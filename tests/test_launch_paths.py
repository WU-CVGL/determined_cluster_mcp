from __future__ import annotations

import json
import os
import shlex
import shutil
import threading
import time
import uuid

import pytest

from determined_compute import compute_cli
from determined_compute.compute import (
    ComputeProfile,
    ComputeService,
    ConflictError,
    SQLiteTaskStore,
    ValidationError,
)
from determined_compute.storage import (
    PathInspector,
    SSHConfig,
    StorageAccessConfig,
    StorageError,
    StorageService,
)
from determined_compute.storage import paths as paths_module


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self):
        self.launches = []

    def launch_task(self, kind, config):
        self.launches.append((kind, config))
        return {"id": str(len(self.launches)), "state": "QUEUED"}


class Admission:
    def __init__(self):
        self.calls = []

    def require_capacity(self, kind, config):
        self.calls.append(kind)
        return {"admitted": True}


def make_profile(host_root):
    return ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": str(host_root), "container_path": "/shared"}],
            "defaults": {"image": "image", "pool": "pool", "slots": 1},
            "cluster_identity": "test-cluster",
        }
    )


def make_service(tmp_path, host_root, access=None, inspector=True, timeout=10.0):
    profile = make_profile(host_root)
    paths = None
    if inspector:
        storage = StorageService(profile, access or StorageAccessConfig(), tmp_path / "none.env")
        paths = PathInspector(storage, timeout_seconds=timeout)
    client = FakeClient()
    admission = Admission()
    service = ComputeService(
        client,
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=admission,
        path_inspector=paths,
    )
    return service, client, admission


def experiment(host_root, **overrides):
    request = {
        "name": "train",
        "kind": "experiment",
        "command": "python train.py",
        "workdir": "/shared/code",
        "output_dir": "/shared/out",
        "experiment_config": {
            "searcher": {"name": "single", "metric": "loss", "max_length": {"batches": 1}},
            "checkpoint_storage": {
                "type": "shared_fs",
                "host_path": str(host_root / "checkpoints"),
            },
        },
    }
    request.update(overrides)
    return request


def treat_as_mount_point(monkeypatch, *roots):
    """Make local directories look like mounted shared roots to the implicit host-root view."""
    original = os.path.ismount
    mounted = {os.path.realpath(root) for root in roots}
    monkeypatch.setattr(
        os.path, "ismount", lambda path: os.path.realpath(path) in mounted or original(path)
    )


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A profile host root that is mounted on this machine, as on a cluster login node."""
    root = tmp_path / "host"
    (root / "code").mkdir(parents=True)
    treat_as_mount_point(monkeypatch, root)
    return root


def checks_by_field(plan):
    return {item["field"]: item for item in plan["path_checks"]}


def test_undecidable_roots_are_unverified_and_plan_is_otherwise_unchanged(tmp_path):
    root = tmp_path / f"nonexistent-root-{uuid.uuid4().hex}"
    service, _client, _admission = make_service(tmp_path, root)
    bare, _client, _admission = make_service(tmp_path, root, inspector=False)
    request = experiment(root)

    plan = service.plan(request)

    assert {item["status"] for item in plan["path_checks"]} == {"unverified"}
    assert {item["reason"] for item in plan["path_checks"]} == {"not_locally_visible"}
    assert {key: value for key, value in plan.items() if key != "path_checks"} == bare.plan(request)
    assert plan["advisories"] == []
    assert list(checks_by_field(plan)) == [
        "bind_mounts[0].host_path",
        "workdir",
        "experiment_config.checkpoint_storage.host_path",
        "output_dir",
    ]


def test_present_paths_are_reported_with_output_dir_optional(tmp_path, host):
    (host / "checkpoints").mkdir()
    service, _client, _admission = make_service(tmp_path, host)

    checks = checks_by_field(service.plan(experiment(host)))

    assert checks["bind_mounts[0].host_path"] == {
        "field": "bind_mounts[0].host_path",
        "host_path": str(host),
        "container_path": "/shared",
        "required": True,
        "status": "present",
        "reason": None,
    }
    assert checks["workdir"]["host_path"] == str(host / "code")
    assert checks["workdir"]["container_path"] == "/shared/code"
    assert checks["experiment_config.checkpoint_storage.host_path"]["container_path"] is None
    assert checks["output_dir"]["required"] is False
    assert checks["output_dir"]["status"] == "missing"


def test_missing_checkpoint_path_fails_plan_and_launch_before_any_claim(tmp_path, host):
    service, client, admission = make_service(tmp_path, host)
    request = experiment(host)
    expected = [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(host / "checkpoints"),
            "status": "missing",
        }
    ]

    with pytest.raises(ValidationError) as planned:
        service.plan(request)
    with pytest.raises(ValidationError) as launched:
        service.launch(request, "request-1", "session-a")

    for caught in (planned, launched):
        assert caught.value.code == "path_not_found"
        assert caught.value.details == {"missing_paths": expected}
        assert "experiment_config.checkpoint_storage.host_path" in str(caught.value)
    assert service.store.list_owned("session-a") == []
    assert client.launches == []
    assert admission.calls == []


def test_missing_workdir_and_file_in_place_of_directory_fail(tmp_path, host):
    (host / "checkpoints").write_text("not a directory", encoding="utf-8")
    service, _client, _admission = make_service(tmp_path, host)

    with pytest.raises(ValidationError) as caught:
        service.plan(experiment(host, workdir="/shared/missing"))

    assert caught.value.details["missing_paths"] == [
        {"field": "workdir", "host_path": str(host / "missing"), "status": "missing"},
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(host / "checkpoints"),
            "status": "not_directory",
        },
    ]


def test_blocked_filesystem_probe_times_out_as_unverified(tmp_path, host, monkeypatch):
    release = threading.Event()
    original = paths_module._stat_directory

    def blocked(path):
        release.wait(5)
        return original(path)

    monkeypatch.setattr(paths_module, "_stat_directory", blocked)
    service, _client, _admission = make_service(tmp_path, host, timeout=0.2)
    try:
        plan = service.plan(experiment(host))
    finally:
        release.set()

    assert {(item["status"], item["reason"]) for item in plan["path_checks"]} == {
        ("unverified", "timeout")
    }


def test_ssh_only_and_unavailable_storage_are_unverified(tmp_path, host):
    ssh_only = StorageAccessConfig(mode="ssh", ssh=SSHConfig(host="login"))
    service, _client, _admission = make_service(tmp_path, host, access=ssh_only)
    assert {item["reason"] for item in service.plan(experiment(host))["path_checks"]} == {
        "ssh_only_access"
    }

    unavailable = ComputeService(
        FakeClient(),
        SQLiteTaskStore(":memory:"),
        make_profile(host),
        path_inspector=PathInspector(None),
    )
    assert {item["reason"] for item in unavailable.plan(experiment(host))["path_checks"]} == {
        "storage_config_unavailable"
    }


def test_explicit_local_mount_decides_a_root_invisible_on_this_machine(tmp_path):
    local = tmp_path / "local-view"
    (local / "code").mkdir(parents=True)
    # An explicit entry is trusted as configured, even though it is not a mount point.
    assert not os.path.ismount(local)
    profile = ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": "/cluster-only/data", "container_path": "/shared"}],
            "defaults": {"image": "image", "pool": "pool"},
        }
    )
    access = StorageAccessConfig.from_dict(
        {"local_mounts": [{"host_path": "/cluster-only/data", "local_path": str(local)}]}
    )
    service = ComputeService(
        FakeClient(),
        SQLiteTaskStore(":memory:"),
        profile,
        path_inspector=PathInspector(StorageService(profile, access, tmp_path / "none.env")),
    )
    request = experiment(tmp_path)
    request["experiment_config"]["checkpoint_storage"]["host_path"] = "/cluster-only/data/ckpt"

    with pytest.raises(ValidationError) as caught:
        service.plan(request)

    assert caught.value.details["missing_paths"] == [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": "/cluster-only/data/ckpt",
            "status": "missing",
        }
    ]


def test_create_directories_plans_nothing_and_launch_creates_before_submitting(tmp_path, host):
    service, client, admission = make_service(tmp_path, host)
    request = experiment(host, create_directories=["output_dir", "checkpoint_storage"])

    plan = service.plan(request)

    checks = checks_by_field(plan)
    assert plan["create_directories"] == ["checkpoint_storage", "output_dir"]
    assert checks["experiment_config.checkpoint_storage.host_path"]["status"] == "will_create"
    assert checks["output_dir"]["status"] == "will_create"
    assert checks["output_dir"]["required"] is True
    assert not (host / "checkpoints").exists() and not (host / "out").exists()

    launched = service.launch(request, "request-1", "session-a")

    assert (host / "checkpoints").is_dir() and (host / "out").is_dir()
    assert launched["prepared_directories"] == [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(host / "checkpoints"),
            "created": True,
        },
        {"field": "output_dir", "host_path": str(host / "out"), "created": True},
    ]
    assert len(client.launches) == 1
    assert admission.calls == ["experiment"]
    retried = service.launch(request, "request-1", "session-a")
    assert "prepared_directories" not in retried
    assert retried["task_id"] == launched["task_id"]
    assert len(client.launches) == 1


def test_client_failure_creates_no_directories_and_no_record(tmp_path, host):
    profile = make_profile(host)
    client = FakeClient()
    attempts = []

    def factory(api_url):
        attempts.append(api_url)
        if len(attempts) == 1:
            raise RuntimeError("temporary login failure")
        return client

    storage = StorageService(profile, StorageAccessConfig(), tmp_path / "none.env")
    service = ComputeService(
        compute_cli._LazyClient(factory, lambda: client.api_url),
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=Admission(),
        path_inspector=PathInspector(storage),
    )
    request = experiment(host, create_directories=["output_dir", "checkpoint_storage"])

    with pytest.raises(RuntimeError, match="temporary login failure"):
        service.launch(request, "request-1", "session-a")

    assert not (host / "checkpoints").exists() and not (host / "out").exists()
    assert service.store.list_owned("session-a") == []
    launched = service.launch(request, "request-1", "session-a")
    assert launched["state"] == "submitted"
    assert [item["created"] for item in launched["prepared_directories"]] == [True, True]
    assert len(client.launches) == 1


def test_create_directories_needs_storage_access_before_claim(tmp_path, host):
    request = experiment(host, create_directories=["checkpoint_storage"])
    service = ComputeService(
        FakeClient(), SQLiteTaskStore(tmp_path / "a.db"), make_profile(host),
        inspector=Admission(), path_inspector=PathInspector(None),
    )
    assert checks_by_field(service.plan(request))[
        "experiment_config.checkpoint_storage.host_path"
    ]["status"] == "will_create"
    with pytest.raises(StorageError) as caught:
        service.launch(request, "request-1", "session-a")
    assert caught.value.code == "configuration_required"
    assert service.store.list_owned("session-a") == []

    library = ComputeService(
        FakeClient(), SQLiteTaskStore(tmp_path / "b.db"), make_profile(host), inspector=Admission()
    )
    assert "path_checks" not in library.plan(request)
    with pytest.raises(ConflictError) as missing:
        library.launch(request, "request-1", "session-a")
    assert missing.value.code == "configuration_required"
    assert library.store.list_owned("session-a") == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["checkpoint_storage"], "requires an experiment"),
        (["output_dir", "output_dir"], "repeat"),
        (["workdir"], "unknown"),
        ("output_dir", "list of strings"),
    ],
)
def test_create_directories_validation(tmp_path, host, value, message):
    service, _client, _admission = make_service(tmp_path, host, inspector=False)
    request = {
        "name": "run",
        "command": "true",
        "workdir": "/shared/code",
        "output_dir": "/shared/out",
        "create_directories": value,
    }
    with pytest.raises(ValidationError, match=message):
        service.plan(request)


def test_empty_create_directories_keeps_the_legacy_hash(tmp_path, host):
    service, _client, _admission = make_service(tmp_path, host, inspector=False)
    request = experiment(host)
    baseline = service.plan(request)

    assert service.plan({**request, "create_directories": []}) == baseline
    assert service._payload_hash(
        service.plan({**request, "create_directories": ["output_dir"]})
    ) != service._payload_hash(baseline)


def test_existing_request_is_returned_without_path_checks(tmp_path, host):
    (host / "checkpoints").mkdir()
    service, client, _admission = make_service(tmp_path, host)
    request = experiment(host)
    first = service.launch(request, "request-1", "session-a")
    (host / "checkpoints").rmdir()

    again = service.launch(request, "request-1", "session-a")

    assert again == first
    assert len(client.launches) == 1


def test_cli_plan_degrades_when_storage_config_is_invalid(tmp_path, host, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"mounts:\n  - host_path: {host}\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    storage = tmp_path / "storage.yaml"
    storage.write_text("unknown_setting: true\n", encoding="utf-8")
    monkeypatch.delenv("DETERMINED_COMPUTE_STORAGE", raising=False)

    code = compute_cli.main([
        "--profile", str(profile), "--storage-config", str(storage), "plan",
        "--request", json.dumps(experiment(host)),
    ])

    assert code == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert {(item["status"], item["reason"]) for item in result["path_checks"]} == {
        ("unverified", "storage_config_unavailable")
    }


def test_cli_reports_missing_paths_in_error_details(tmp_path, host, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"mounts:\n  - host_path: {host}\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DETERMINED_COMPUTE_STORAGE", raising=False)

    code = compute_cli.main(
        ["--profile", str(profile), "plan", "--request", json.dumps(experiment(host))]
    )

    assert code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "path_not_found"
    assert error["retryable"] is False
    assert error["details"] == {
        "missing_paths": [
            {
                "field": "experiment_config.checkpoint_storage.host_path",
                "host_path": str(host / "checkpoints"),
                "status": "missing",
            }
        ]
    }


def test_safe_error_details_keep_only_allowed_keys():
    error = ValidationError("missing", code="path_not_found")
    error.details = {"missing_paths": [{"field": "workdir"}], "secret": "value"}

    assert compute_cli.safe_error_details(error) == {"missing_paths": [{"field": "workdir"}]}


def remote_profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/cluster/shared", "container_path": "/work"},
                {"host_path": "/cluster/reference", "container_path": "/ref", "read_only": True},
            ],
            "defaults": {"image": "image", "pool": "pool"},
        }
    )


@pytest.mark.parametrize(
    ("output", "created"), [("created\n", True), ("banner\nexisted\n", False)]
)
def test_ssh_directory_creation_uses_a_fixed_quoted_script(monkeypatch, output, created):
    config = StorageAccessConfig.from_dict(
        {"mode": "ssh", "ssh": {"host": "storage.example", "user": "alice", "auth": "openssh"}}
    )
    service = StorageService(remote_profile(), config)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"output": output, "truncated": False}

    monkeypatch.setattr(service, "_run", run)
    host_path = "/cluster/shared/a path/with'quote;$(x)"

    result = service.ensure_directories([{"field": "output_dir", "host_path": host_path}])

    assert result == [{"field": "output_dir", "host_path": host_path, "created": created}]
    argv, kwargs = calls[0]
    assert argv[0] == "ssh" and argv[-2] == "storage.example"
    assert shlex.split(argv[-1]) == [
        "sh",
        "-c",
        'if [ -d "$1" ]; then echo existed; else mkdir -p -- "$1" && echo created; fi',
        "sh",
        host_path,
    ]
    assert set(kwargs["env_overrides"]) <= {"SSH_AUTH_SOCK"}


@pytest.mark.parametrize("output", ["", "maybe\n", "created\nextra\n"])
def test_ssh_directory_creation_rejects_unexpected_responses(monkeypatch, output):
    config = StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example"}})
    service = StorageService(remote_profile(), config)
    monkeypatch.setattr(service, "_run", lambda argv, **kwargs: {"output": output})

    with pytest.raises(StorageError) as caught:
        service.ensure_directories([{"field": "output_dir", "host_path": "/cluster/shared/x"}])

    assert caught.value.code == "storage_operation_failed"


def test_directory_creation_refuses_read_only_roots_and_escaping_parents(tmp_path):
    local_root = tmp_path / "client-mount"
    outside = tmp_path / "outside"
    local_root.mkdir()
    outside.mkdir()
    (local_root / "escape").symlink_to(outside, target_is_directory=True)
    config = StorageAccessConfig.from_dict(
        {"local_mounts": [{"host_path": "/cluster/shared", "local_path": str(local_root)}]}
    )
    service = StorageService(remote_profile(), config)

    cases = {
        "/cluster/reference/new": "read_only_storage",
        "/cluster/shared/escape/new": "invalid_storage_path",
    }
    for host_path, code in cases.items():
        with pytest.raises(StorageError) as caught:
            service.ensure_directories([{"field": "output_dir", "host_path": host_path}])
        assert caught.value.code == code, host_path

    assert list(outside.iterdir()) == []
    assert service.ensure_directories(
        [{"field": "output_dir", "host_path": "/cluster/shared/runs/one"}]
    ) == [{"field": "output_dir", "host_path": "/cluster/shared/runs/one", "created": True}]
    assert (local_root / "runs" / "one").is_dir()
    # An existing mount root satisfies the request without being created.
    assert service.ensure_directories(
        [{"field": "output_dir", "host_path": "/cluster/shared"}]
    ) == [{"field": "output_dir", "host_path": "/cluster/shared", "created": False}]


def test_same_named_local_directory_that_is_not_a_mount_is_unverified(tmp_path):
    root = tmp_path / "SSD"
    (root / "code").mkdir(parents=True)
    assert not os.path.ismount(root)
    service, client, admission = make_service(tmp_path, root)
    request = experiment(root, create_directories=["output_dir", "checkpoint_storage"])

    plain = service.plan(experiment(root))
    planned = service.plan(request)

    assert {(item["status"], item["reason"]) for item in plain["path_checks"]} == {
        ("unverified", "local_view_unconfirmed")
    }
    assert {item["status"] for item in planned["path_checks"]} == {"unverified", "will_create"}
    with pytest.raises(StorageError) as caught:
        service.launch(request, "request-1", "session-a")
    assert caught.value.code == "configuration_required"
    assert "not detected as a mount point" in str(caught.value)
    assert sorted(path.name for path in root.iterdir()) == ["code"]
    assert service.store.list_owned("session-a") == []
    assert client.launches == []


@pytest.mark.parametrize("mode", ["auto", "local"])
def test_unavailable_local_mount_entry_does_not_fall_back_to_the_host_root(
    tmp_path, host, mode
):
    dead = {"host_path": str(host), "local_path": str(tmp_path / "gone")}
    access = StorageAccessConfig.from_dict({"mode": mode, "local_mounts": [dead]})
    service, client, _admission = make_service(tmp_path, host, access=access)
    request = experiment(host, create_directories=["checkpoint_storage"])

    plan = service.plan(experiment(host))

    assert {(item["status"], item["reason"]) for item in plan["path_checks"]} == {
        ("unverified", "local_mount_unavailable")
    }
    with pytest.raises(StorageError) as caught:
        service.launch(request, "request-1", "session-a")
    assert caught.value.code == "configuration_required"
    assert "local_mounts entry" in str(caught.value)
    assert not (host / "checkpoints").exists()
    assert service.store.list_owned("session-a") == []
    assert client.launches == []


def test_create_directories_uses_ssh_when_the_local_view_is_unconfirmed(tmp_path, monkeypatch):
    root = tmp_path / "SSD"
    (root / "code").mkdir(parents=True)
    access = StorageAccessConfig.from_dict({"ssh": {"host": "storage.example"}})
    service, client, _admission = make_service(tmp_path, root, access=access)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return {"output": "created\n", "truncated": False}

    monkeypatch.setattr(service.path_inspector.storage, "_run", run)

    launched = service.launch(
        experiment(root, create_directories=["checkpoint_storage"]), "request-1", "session-a"
    )

    assert launched["prepared_directories"] == [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(root / "checkpoints"),
            "created": True,
        }
    ]
    assert shlex.split(calls[0][-1])[-1] == str(root / "checkpoints")
    assert not (root / "checkpoints").exists()
    assert len(client.launches) == 1


def test_launch_does_not_create_after_a_timed_out_check(tmp_path, host, monkeypatch):
    release = threading.Event()
    original = paths_module._stat_directory

    def blocked(path):
        release.wait(5)
        return original(path)

    monkeypatch.setattr(paths_module, "_stat_directory", blocked)
    service, client, admission = make_service(tmp_path, host, timeout=0.2)
    request = experiment(host, create_directories=["output_dir", "checkpoint_storage"])
    try:
        with pytest.raises(StorageError) as caught:
            service.launch(request, "request-1", "session-a")
        assert not (host / "checkpoints").exists() and not (host / "out").exists()
    finally:
        release.set()

    assert caught.value.code == "storage_timeout"
    assert caught.value.retryable is True
    assert "experiment_config.checkpoint_storage.host_path, output_dir" in str(caught.value)
    assert compute_cli._error_payload(caught.value)["error"]["retryable"] is True
    assert admission.calls == []
    assert service.store.list_owned("session-a") == []
    assert client.launches == []


def test_local_directory_creation_is_bounded_by_the_inspector_deadline(
    tmp_path, host, monkeypatch
):
    release = threading.Event()
    original = StorageService._mkdir_beneath_root

    def stalled(path, root):
        release.wait(5)
        original(path, root)

    monkeypatch.setattr(StorageService, "_mkdir_beneath_root", staticmethod(stalled))
    service, client, _admission = make_service(tmp_path, host, timeout=0.3)
    request = {
        "name": "run",
        "command": "true",
        "workdir": "/shared/code",
        "output_dir": "/shared/out",
        "create_directories": ["output_dir"],
    }
    started = time.monotonic()
    try:
        with pytest.raises(StorageError) as caught:
            service.launch(request, "request-1", "session-a")
        elapsed = time.monotonic() - started
        assert not (host / "out").exists()
    finally:
        release.set()

    assert caught.value.code == "storage_timeout" and caught.value.retryable is True
    assert elapsed < 3
    assert service.store.list_owned("session-a") == []
    assert client.launches == []
    # The late creation is harmless: a retry finds the directory and submits once.
    for _ in range(100):
        if (host / "out").is_dir():
            break
        time.sleep(0.02)
    retried = service.launch(request, "request-1", "session-a")
    assert retried["prepared_directories"] == [
        {"field": "output_dir", "host_path": str(host / "out"), "created": False}
    ]
    assert len(client.launches) == 1


def test_existing_mount_root_can_be_named_in_create_directories(tmp_path, host):
    service, client, _admission = make_service(tmp_path, host)
    request = experiment(
        host, output_dir="/shared", create_directories=["checkpoint_storage", "output_dir"]
    )
    request["experiment_config"]["checkpoint_storage"]["host_path"] = str(host)

    checks = checks_by_field(service.plan(request))
    launched = service.launch(request, "request-1", "session-a")

    assert checks["output_dir"]["status"] == "present"
    assert checks["experiment_config.checkpoint_storage.host_path"]["status"] == "present"
    assert launched["state"] == "submitted"
    assert launched["prepared_directories"] == [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(host),
            "created": False,
        },
        {"field": "output_dir", "host_path": str(host), "created": False},
    ]
    assert len(client.launches) == 1


@pytest.mark.parametrize(("output", "created"), [("existed\n", False), ("missing\n", None)])
def test_ssh_never_creates_a_mount_root(monkeypatch, output, created):
    config = StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example"}})
    service = StorageService(remote_profile(), config)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return {"output": output, "truncated": False}

    monkeypatch.setattr(service, "_run", run)
    entry = {"field": "output_dir", "host_path": "/cluster/shared"}

    if created is None:
        with pytest.raises(StorageError) as caught:
            service.ensure_directories([entry])
        assert caught.value.code == "invalid_storage_path"
    else:
        assert service.ensure_directories([entry]) == [{**entry, "created": created}]
    assert "mkdir" not in shlex.split(calls[0][-1])[2]


def install_fake_ssh(tmp_path, monkeypatch, body):
    """Put an ``ssh`` stand-in first on PATH; it never contacts a network."""
    tools = tmp_path / "fake-tools"
    tools.mkdir(exist_ok=True)
    script = tools / "ssh"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))


@pytest.mark.skipif(
    os.name != "posix" or not os.path.exists("/bin/sh") or shutil.which("sleep") is None,
    reason="the fake ssh needs a POSIX shell and sleep",
)
def test_ssh_directory_creation_timeout_is_retryable(tmp_path, monkeypatch):
    root = tmp_path / "SSD"
    access = StorageAccessConfig.from_dict(
        {"mode": "ssh", "timeout_seconds": 1, "ssh": {"host": "storage.example"}}
    )
    service, client, _admission = make_service(tmp_path, root, access=access)
    request = experiment(root, create_directories=["checkpoint_storage"])
    install_fake_ssh(tmp_path, monkeypatch, "exec sleep 30")

    started = time.monotonic()
    with pytest.raises(StorageError) as caught:
        service.launch(request, "request-1", "session-a")
    elapsed = time.monotonic() - started

    assert caught.value.code == "storage_timeout"
    assert caught.value.retryable is True
    assert compute_cli._error_payload(caught.value)["error"]["retryable"] is True
    assert elapsed < 10
    assert service.store.list_owned("session-a") == []
    assert client.launches == []
    assert not root.exists()

    # Only directory creation is marked retryable; other SSH operations such as a check
    # keep the non-retryable timeout.
    with pytest.raises(StorageError) as check_timeout:
        service.path_inspector.storage.check("/shared/code")
    assert check_timeout.value.code == "storage_timeout"
    assert check_timeout.value.retryable is False

    # Nothing was claimed, so a retry with the same request_id submits exactly once.
    install_fake_ssh(tmp_path, monkeypatch, "echo created")
    retried = service.launch(request, "request-1", "session-a")
    assert retried["state"] == "submitted"
    assert retried["prepared_directories"] == [
        {
            "field": "experiment_config.checkpoint_storage.host_path",
            "host_path": str(root / "checkpoints"),
            "created": True,
        }
    ]
    assert len(client.launches) == 1
