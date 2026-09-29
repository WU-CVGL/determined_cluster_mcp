from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest

from determined_compute.compute import APIError
from determined_compute import compute_cli


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, request):
        self.calls.append(("plan", request))
        return {"kind": "command", "config": request}

    def launch(self, request, request_id, owner):
        self.calls.append(("launch", request, request_id, owner))
        return {"task_id": "task-1", "state": "submitted"}

    def status(self, task_id, owner):
        self.calls.append(("status", task_id, owner))
        return {"task_id": task_id, "owner": owner}

    def logs(self, task_id, owner, tail):
        self.calls.append(("logs", task_id, owner, tail))
        return [{"message": "hello"}]

    def cancel(self, task_id, owner):
        self.calls.append(("cancel", task_id, owner))
        return {"task_id": task_id, "state": "cancelling"}

    def reconcile(self, task_id, owner, remote_id):
        self.calls.append(("reconcile", task_id, owner, remote_id))
        return {"task_id": task_id, "remote_id": remote_id}

    def list_tasks(self, owner):
        self.calls.append(("list", owner))
        return [{"task_id": "task-1", "owner": owner}]

    def discover(self, kind, owner, limit=50, offset=0):
        self.calls.append(("discover", kind, owner, limit, offset))
        return {
            "kind": kind,
            "owner": owner,
            "limit": limit,
            "offset": offset,
            "tasks": [],
        }

    def adopt(self, kind, remote_id, owner):
        self.calls.append(("adopt", kind, remote_id, owner))
        return {"task_id": "adopted-1", "kind": kind, "remote_id": remote_id}


def test_plan_outputs_json_and_passes_request(monkeypatch, capsys):
    service = FakeService()
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    code = compute_cli.main(["plan", "--request", '{"command":["echo","hello"]}'])

    assert code == 0
    assert service.calls == [("plan", {"command": ["echo", "hello"]})]
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "result": {
            "kind": "command",
            "config": {"command": ["echo", "hello"]},
        },
    }


def test_launch_binds_owner_outside_request(monkeypatch, capsys):
    service = FakeService()
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    code = compute_cli.main(
        ["launch", "--request", '{"command":"true"}', "--request-id", "req-1"]
    )

    assert code == 0
    assert service.calls == [("launch", {"command": "true"}, "req-1", "alice")]
    assert json.loads(capsys.readouterr().out)["result"]["task_id"] == "task-1"


def test_discover_and_adopt_bind_owner_and_forward_pagination(monkeypatch, capsys):
    service = FakeService()
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    assert compute_cli.main(["discover", "shell", "--limit", "7", "--offset", "2"]) == 0
    discovered = json.loads(capsys.readouterr().out)["result"]
    assert discovered == {
        "kind": "shell",
        "owner": "alice",
        "limit": 7,
        "offset": 2,
        "tasks": [],
    }

    assert compute_cli.main(["adopt", "experiment", "remote-9"]) == 0
    adopted = json.loads(capsys.readouterr().out)["result"]
    assert adopted["task_id"] == "adopted-1"
    assert service.calls == [
        ("discover", "shell", "alice", 7, 2),
        ("adopt", "experiment", "remote-9", "alice"),
    ]


def test_usage_binds_owner_and_forwards_options(monkeypatch, capsys):
    service = FakeService()
    calls = []

    def usage(*args):
        calls.append(args)
        return {"task_id": args[0], "series": []}

    service.usage = usage
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    assert compute_cli.main(["usage", "task-1"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["task_id"] == "task-1"
    assert compute_cli.main([
        "usage", "task-2", "--window-seconds", "900", "--allocation-id", "a.1",
        "--trial-id", "4", "--metric", "cpu_cores", "--metric", "gpu_power_watts",
        "--samples",
    ]) == 0
    assert calls == [
        ("task-1", "alice", 3600, None, None, None, False),
        ("task-2", "alice", 900, "a.1", 4, ["cpu_cores", "gpu_power_watts"], True),
    ]


def test_plan_accepts_yaml_request_file(tmp_path, monkeypatch, capsys):
    service = FakeService()
    request = tmp_path / "request.yaml"
    request.write_text("command:\n  - echo\n  - hello\n", encoding="utf-8")
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    code = compute_cli.main(["plan", "--request-file", str(request)])

    assert code == 0
    assert service.calls == [("plan", {"command": ["echo", "hello"]})]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_compute_error_is_structured_json(monkeypatch, capsys):
    service = FakeService()

    def fail(_request):
        raise APIError("master unavailable", code="transport_error", retryable=True)

    service.plan = fail
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (service, "alice"))

    code = compute_cli.main(["plan", "--request", '{"command":"true"}'])

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": {
            "code": "transport_error",
            "message": "master unavailable",
            "retryable": True,
        },
    }


def test_lazy_client_is_not_constructed_until_attribute_access():
    clients = []

    class Client:
        cluster_identity = "cluster"

    lazy = compute_cli._LazyClient(lambda: clients.append(Client()) or clients[-1])
    assert clients == []
    assert lazy.cluster_identity == "cluster"
    assert len(clients) == 1


def test_lazy_client_constructs_once_under_concurrent_first_access():
    clients = []
    start = threading.Barrier(8)

    class Client:
        cluster_identity = "cluster"

    def factory():
        # Release the GIL long enough to expose an unlocked check/create race.
        time.sleep(0.02)
        client = Client()
        clients.append(client)
        return client

    lazy = compute_cli._LazyClient(factory)

    def access():
        start.wait()
        return lazy.cluster_identity

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: access(), range(8)))

    assert results == ["cluster"] * 8
    assert len(clients) == 1


def test_lazy_client_factory_failure_is_not_cached():
    attempts = 0

    class Client:
        cluster_identity = "recovered"

    def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary login failure")
        return Client()

    lazy = compute_cli._LazyClient(factory)
    with pytest.raises(RuntimeError, match="temporary login failure"):
        lazy._resolve_client()

    recovered = lazy._resolve_client()
    assert recovered.cluster_identity == "recovered"
    assert lazy._resolve_client() is recovered
    assert attempts == 2


def test_missing_explicit_owner_is_an_error(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DETERMINED_COMPUTE_OWNER", raising=False)

    code = compute_cli.main(
        ["--profile", str(profile), "--db", str(tmp_path / "tasks.db"), "list"]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "internal_error"
    assert "OWNER" in payload["error"]["message"]


def test_real_plan_needs_no_owner_database_or_api_client(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DETERMINED_COMPUTE_OWNER", raising=False)
    monkeypatch.delenv("DETERMINED_COMPUTE_DB", raising=False)
    monkeypatch.setattr(
        compute_cli,
        "_client_factory",
        lambda _args: (_ for _ in ()).throw(AssertionError("API client constructed")),
    )

    code = compute_cli.main(
        [
            "--profile",
            str(profile),
            "plan",
            "--request",
            '{"command":"true","workdir":"/shared/work","output_dir":"/shared/out"}',
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["result"]["kind"] == "command"


def test_lazy_client_delegates_api_get_instead_of_shadowing_it():
    class API:
        def _get(self, endpoint, params=None):
            return {'endpoint': endpoint, 'params': params}
    lazy = compute_cli._LazyClient(API)
    assert lazy._get('api/v1/resource-pools', params={'limit': 0}) == {
        'endpoint': 'api/v1/resource-pools', 'params': {'limit': 0},
    }
