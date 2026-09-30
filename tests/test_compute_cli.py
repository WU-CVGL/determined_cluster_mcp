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


PROFILE_YAML = (
    "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
    "defaults:\n  image: image\n  pool: pool\n"
)


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(compute_cli, "_resolve_runtime", lambda args: (fake, "alice"))
    return fake


def test_plan_reads_json_or_yaml_request_and_prints_envelope(tmp_path, service, capsys):
    request = tmp_path / "request.yaml"
    request.write_text("command:\n  - echo\n  - hello\n", encoding="utf-8")

    assert compute_cli.main(["plan", "--request", '{"command":["echo","hello"]}']) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "result": {
            "kind": "command",
            "config": {"command": ["echo", "hello"]},
        },
    }
    assert compute_cli.main(["plan", "--request-file", str(request)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert service.calls == [("plan", {"command": ["echo", "hello"]})] * 2


def test_launch_discover_and_adopt_bind_owner_outside_request(service, capsys):
    assert compute_cli.main(
        ["launch", "--request", '{"command":"true"}', "--request-id", "req-1"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["result"]["task_id"] == "task-1"

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
        ("launch", {"command": "true"}, "req-1", "alice"),
        ("discover", "shell", "alice", 7, 2),
        ("adopt", "experiment", "remote-9", "alice"),
    ]


def test_usage_binds_owner_and_forwards_options(service, capsys):
    calls = []

    def usage(*args):
        calls.append(args)
        return {"task_id": args[0], "series": []}

    service.usage = usage

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


def test_compute_error_is_structured_json(service, capsys):
    def fail(_request):
        raise APIError("master unavailable", code="transport_error", retryable=True)

    service.plan = fail

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


def test_lazy_client_constructs_on_first_access_and_delegates_api_get():
    clients = []

    class Client:
        cluster_identity = "cluster"

        def _get(self, endpoint, params=None):
            return {"endpoint": endpoint, "params": params}

    lazy = compute_cli._LazyClient(lambda: clients.append(Client()) or clients[-1])
    assert clients == []
    assert lazy.cluster_identity == "cluster"
    assert len(clients) == 1
    # _get must reach the real client rather than being shadowed by the wrapper.
    assert lazy._get("api/v1/resource-pools", params={"limit": 0}) == {
        "endpoint": "api/v1/resource-pools", "params": {"limit": 0},
    }
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


def test_owner_is_required_for_stored_tasks_but_not_for_offline_plan(
    tmp_path, monkeypatch, capsys
):
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE_YAML, encoding="utf-8")
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

    code = compute_cli.main(
        ["--profile", str(profile), "--db", str(tmp_path / "tasks.db"), "list"]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "internal_error"
    assert "OWNER" in payload["error"]["message"]
