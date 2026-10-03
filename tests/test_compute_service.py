from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    NotFoundError,
    SQLiteTaskStore,
    SubmissionUncertainError,
    ValidationError,
)


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self, delay=0.0):
        self.delay = delay
        self.launches = []
        self.gets = []
        self.log_calls = []
        self.cancel_calls = []
        self._lock = threading.Lock()
        self.entities = {}

    def launch_task(self, kind, config):
        with self._lock:
            self.launches.append((kind, config))
            remote_id = str(len(self.launches))
        if self.delay:
            time.sleep(self.delay)
        entity = {
            "id": remote_id,
            "state": "RUNNING",
            "config": config,
        }
        self.entities[(kind, remote_id)] = entity
        return entity

    def get_task(self, kind, remote_id):
        self.gets.append((kind, remote_id))
        return self.entities[(kind, remote_id)]

    def task_logs(self, kind, remote_id, tail):
        self.log_calls.append((kind, remote_id, tail))
        return [{"message": "ok"}]

    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        return {"id": remote_id, "state": "TERMINATING", "exitCode": None}

    def _get(self, endpoint, params=None):
        if endpoint == "api/v1/resource-pools":
            return {
                "resourcePools": [
                    {
                        "name": "gpu",
                        "numAgents": 1,
                        "slotsAvailable": 1,
                        "slotsUsed": 0,
                        "slotType": "CUDA",
                        "auxContainerCapacity": 4,
                        "auxContainersRunning": 0,
                    }
                ]
            }
        if endpoint == "api/v1/agents":
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "enabled": True,
                        "draining": False,
                        "resourcePools": ["gpu"],
                        "slots": {
                            "0": {
                                "id": "0",
                                "enabled": True,
                                "draining": False,
                            }
                        },
                    }
                ]
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")


class UncertainClient(FakeClient):
    def launch_task(self, kind, config):
        self.launches.append((kind, config))
        raise SubmissionUncertainError("connection closed after request")


class EmptyCancelClient(FakeClient):
    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        return {}


class ToggleInspector:
    def __init__(self, available=True):
        self.available = available
        self.calls = []

    def require_capacity(self, kind, config):
        self.calls.append((kind, config))
        if not self.available:
            raise APIError(
                "pool cannot fit request",
                code="capacity_unavailable",
                retryable=True,
            )
        return {"admitted": True}


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "registry/image:stable", "pool": "gpu", "slots": 1},
            "shell_inactivity_seconds": 7200,
            "cluster_identity": "test-cluster",
        }
    )


@pytest.fixture
def command_request():
    return {
        "kind": "auto",
        "command": ["python", "train.py", "--name", "space value"],
        "workdir": "/shared/container/jobs/code",
        "output_dir": "/shared/container/jobs/out",
        "code_revision": "git:abc123-dirty=false",
    }


def test_plan_is_offline_and_builds_shared_storage_command(tmp_path, profile, command_request):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    plan = service.plan(command_request)

    assert client.launches == []
    assert client.gets == []
    assert plan["kind"] == "command"
    assert plan["name"] == "command: code"
    assert plan["description"] is None
    assert plan["allow_queue"] is False
    assert "generated_task_name" in {item["code"] for item in plan["advisories"]}
    assert plan["code_revision"] == "git:abc123-dirty=false"
    assert plan["config"]["resources"] == {"slots": 1, "resource_pool": "gpu"}
    assert plan["config"]["bind_mounts"] == [
        {"host_path": "/shared/host", "container_path": "/shared/container"}
    ]
    assert plan["config"]["entrypoint"] == [
        "/bin/bash",
        "-lc",
        "mkdir -p /shared/container/jobs/out && cd /shared/container/jobs/code && "
        "python train.py --name 'space value'",
    ]


def test_auto_modes_and_shell_advisory(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    shell = dict(command_request)
    shell.pop("command")
    shell.update({"interactive": True, "overnight": True})

    plan = service.plan(shell)

    assert plan["kind"] == "shell"
    assert "entrypoint" not in plan["config"]
    assert plan["config"]["resources"]["slots"] == 1
    assert {item["code"] for item in plan["advisories"]} == {
        "generated_task_name",
        "overnight_experiment_recommended",
        "shell_inactivity_policy",
    }


def test_command_and_shell_use_native_entrypoint_shapes(
    tmp_path, profile, command_request
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    command = service.plan(command_request)
    shell_request = dict(command_request)
    shell_request.update({"kind": "shell", "interactive": True})
    shell_request.pop("command")
    shell = service.plan(shell_request)

    assert command["config"]["entrypoint"][:2] == ["/bin/bash", "-lc"]
    assert isinstance(command["config"]["entrypoint"][2], str)
    assert "entrypoint" not in shell["config"]


def test_overnight_command_becomes_experiment(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(command_request, overnight=True)

    plan = service.plan(request)

    assert plan["kind"] == "experiment"
    assert plan["config"]["resources"]["slots_per_trial"] == 1
    assert plan["config"]["entrypoint"].endswith("python train.py --name 'space value'")


@pytest.mark.parametrize(
    "change",
    [
        {"workdir": "/local/code"},
        {"output_dir": "/shared/container/jobs/../escape"},
        {"workdir": "shared/container/code"},
        {"upload_context": "/tmp/context"},
        {"experiment_config": {"files": [{"path": "secret"}]}},
        {"experiment_config": {"context_path": "/tmp/upload"}},
        {"experiment_config": {"bind_mounts": []}},
    ],
)
def test_rejects_unmapped_paths_and_upload_or_mount_fields(
    tmp_path, profile, command_request, change
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(command_request)
    request.update(change)
    if "experiment_config" in change:
        request.update({"kind": "experiment", "command": None})

    with pytest.raises(ValidationError):
        service.plan(request)


def test_context_named_hyperparameters_are_allowed(tmp_path, profile):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "entrypoint": "python train.py",
            "hyperparameters": {"context_length": 8192, "model_context_window": 16384},
        },
    }

    plan = service.plan(request)

    assert plan["config"]["hyperparameters"]["context_length"] == 8192


def test_read_only_mount_is_preserved_for_reference_data(tmp_path):
    profile = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/work", "container_path": "/work"},
                {
                    "host_path": "/shared/graphics",
                    "container_path": "/graphics",
                    "read_only": True,
                },
            ],
            "defaults": {"image": "image", "pool": "pool", "slots": 0},
        }
    )
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    plan = service.plan(
        {
            "command": ["python", "repair.py", "--registry", "/graphics/registry"],
            "workdir": "/work/code",
            "output_dir": "/work/output",
        }
    )

    assert {
        "host_path": "/shared/graphics",
        "container_path": "/graphics",
        "read_only": True,
    } in plan["config"]["bind_mounts"]


@pytest.mark.parametrize("field", ["workdir", "output_dir"])
def test_execution_paths_cannot_use_read_only_mount(tmp_path, field):
    profile = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/work", "container_path": "/work"},
                {
                    "host_path": "/shared/reference",
                    "container_path": "/reference",
                    "read_only": True,
                },
            ],
            "defaults": {"image": "image", "pool": "pool", "slots": 0},
        }
    )
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "command": ["true"],
        "workdir": "/work/code",
        "output_dir": "/work/output",
    }
    request[field] = "/reference/task"

    with pytest.raises(ValidationError, match="read-only"):
        service.plan(request)


def test_read_only_mount_validation_and_fingerprint():
    base = {
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool", "slots": 0},
    }
    writable = ComputeProfile.from_dict(base)
    read_only = ComputeProfile.from_dict(
        {
            **base,
            "mounts": [
                {
                    "host_path": "/shared",
                    "container_path": "/shared",
                    "read_only": True,
                }
            ],
        }
    )

    assert writable.mounts[0].as_config() == {
        "host_path": "/shared",
        "container_path": "/shared",
    }
    assert read_only.mounts[0].as_config()["read_only"] is True
    assert writable.fingerprint != read_only.fingerprint

    invalid = {**base, "mounts": [{**base["mounts"][0], "read_only": "true"}]}
    with pytest.raises(ValidationError, match="must be a boolean"):
        ComputeProfile.from_dict(invalid)


def test_submission_marker_environment_name_is_reserved(tmp_path, profile):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "entrypoint": "python train.py",
            "environment": {
                "environment_variables": [
                    "COMPUTE_SUBMISSION_MARKER=user-controlled"
                ]
            },
        },
    }

    with pytest.raises(ValidationError, match="cannot be overridden"):
        service.plan(request)


def test_concurrent_duplicate_launch_calls_remote_once(tmp_path, profile, command_request):
    client = FakeClient(delay=0.15)
    db_path = tmp_path / "tasks.db"
    services = [
        ComputeService(client, SQLiteTaskStore(db_path), profile) for _ in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                service.launch, command_request, "same-request", "session-a"
            )
            for service in services
        ]
        results = [future.result() for future in futures]

    assert len(client.launches) == 1
    assert len({result["task_id"] for result in results}) == 1
    assert {result["state"] for result in results}.issubset({"pending", "submitting", "submitted"})
    assert services[0].list_tasks("session-a")[0]["state"] == "submitted"


def test_request_id_payload_conflict(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    service.launch(command_request, "request-1", "session-a")

    with pytest.raises(ConflictError) as caught:
        service.launch(dict(command_request, command="echo changed"), "request-1", "session-a")

    assert caught.value.code == "idempotency_conflict"


def test_capacity_failure_creates_no_record_and_same_request_can_retry(
    tmp_path, profile, command_request
):
    inspector = ToggleInspector(available=False)
    client = FakeClient()
    service = ComputeService(
        client,
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=inspector,
    )

    with pytest.raises(APIError) as caught:
        service.launch(command_request, "capacity-retry", "session-a")

    assert caught.value.code == "capacity_unavailable"
    assert service.list_tasks("session-a") == []
    assert client.launches == []

    inspector.available = True
    launched = service.launch(command_request, "capacity-retry", "session-a")
    assert launched["state"] == "submitted"
    assert len(client.launches) == 1


def test_existing_idempotent_request_skips_new_capacity_check(
    tmp_path, profile, command_request
):
    inspector = ToggleInspector(available=True)
    service = ComputeService(
        FakeClient(),
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=inspector,
    )
    first = service.launch(command_request, "existing-request", "session-a")
    inspector.available = False

    duplicate = service.launch(command_request, "existing-request", "session-a")

    assert duplicate["task_id"] == first["task_id"]
    assert len(inspector.calls) == 1


def test_allow_queue_bypasses_admission_without_leaking_into_config(
    tmp_path, profile, command_request
):
    inspector = ToggleInspector(available=False)
    client = FakeClient()
    service = ComputeService(
        client,
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=inspector,
    )
    request = dict(command_request, allow_queue=True)

    launched = service.launch(request, "queue-ok", "session-a")

    assert launched["state"] == "submitted"
    assert inspector.calls == []
    assert "allow_queue" not in client.launches[0][1]


def test_named_command_uses_display_name_and_reconciles(
    tmp_path, profile, command_request
):
    client = UncertainClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(
        command_request,
        name="pilot waypoint evaluation",
        description="Evaluate the short validation route.",
    )

    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(request, "named-command", "session-a")

    description = client.launches[0][1]["description"]
    assert description.splitlines() == [
        "pilot waypoint evaluation",
        "Evaluate the short validation route.",
    ]
    variables = client.launches[0][1]["environment"]["environment_variables"]
    marker = next(
        item.partition("=")[2]
        for item in variables
        if item.startswith("COMPUTE_SUBMISSION_MARKER=")
    )
    task_id = caught.value.details["task_id"]
    client.entities[("command", "remote-named")] = {
        "id": "remote-named",
        "state": "RUNNING",
        "submissionMarker": marker,
        "config": {"description": description},
    }

    assert (
        service.reconcile(task_id, "session-a", "remote-named")["remote_id"]
        == "remote-named"
    )


def test_named_experiment_top_level_metadata_overrides_native_metadata(
    tmp_path, profile
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "kind": "experiment",
        "name": "skill retention pass 237",
        "description": "Top-level operator description",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "name": "old generated experiment name",
            "description": "Five-clip deterministic decoder evaluation",
            "entrypoint": "python evaluate.py",
        },
    }

    service.launch(request, "named-experiment", "session-a")
    config = service.client.launches[0][1]
    assert config["name"] == "skill retention pass 237"
    assert config["description"] == "Top-level operator description"
    assert "determined-compute:" not in config["description"]
    assert any(
        item.startswith("COMPUTE_SUBMISSION_MARKER=determined-compute:")
        for item in config["environment"]["environment_variables"]
    )


def test_experiment_honors_native_metadata_without_top_level_values(tmp_path, profile):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    plan = service.plan(
        {
            "kind": "experiment",
            "workdir": "/shared/container/code",
            "output_dir": "/shared/container/out",
            "experiment_config": {
                "name": "native experiment name",
                "description": "native description",
                "entrypoint": "python evaluate.py",
            },
        }
    )

    assert plan["name"] == "native experiment name"
    assert plan["description"] == "native description"
    assert plan["config"]["name"] == "native experiment name"
    assert plan["config"]["description"] == "native description"
    assert "generated_task_name" not in {item["code"] for item in plan["advisories"]}


def test_unnamed_submission_generates_human_name_and_persists_it(
    tmp_path, profile, command_request
):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    task = service.launch(command_request, "unnamed-command", "session-a")

    description = client.launches[0][1]["description"]
    assert description == "command: code"
    assert "determined-compute:" not in description
    assert task["name"] == "command: code"
    assert task["description"] is None
    assert service.list_tasks("session-a")[0]["name"] == "command: code"


def test_display_names_can_collide_without_colliding_task_identity(
    tmp_path, profile, command_request
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(command_request, name="repeated human name")

    first = service.launch(request, "request-a", "session-a")
    second = service.launch(request, "request-b", "session-a")

    assert first["name"] == second["name"] == "repeated human name"
    assert first["task_id"] != second["task_id"]
    assert first["remote_id"] != second["remote_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "x" * 129),
        ("name", "bad\nname"),
        ("description", "x" * 2049),
        ("description", "bad\x00description"),
    ],
)
def test_display_metadata_validation(tmp_path, profile, command_request, field, value):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(ValidationError):
        service.plan(dict(command_request, **{field: value}))


def test_uncertain_submission_is_recorded_and_never_retried(
    tmp_path, profile, command_request
):
    client = UncertainClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(command_request, "uncertain-1", "session-a")

    task_id = caught.value.details["task_id"]
    assert caught.value.code == "submission_uncertain"
    assert service.status(task_id, "session-a")["state"] == "submission_uncertain"
    duplicate = service.launch(command_request, "uncertain-1", "session-a")
    assert duplicate["task_id"] == task_id
    assert duplicate["state"] == "submission_uncertain"
    assert len(client.launches) == 1


@pytest.mark.parametrize("initial_state", ["pending", "submitting"])
def test_stale_crash_record_transitions_to_uncertain_without_resubmit(
    tmp_path, profile, command_request, initial_state
):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile, submission_stale_seconds=0)
    plan = service.plan(command_request)
    record, created = store.claim(
        request_id=f"crash-{initial_state}",
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=command_request["workdir"],
        output_dir=command_request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    assert created
    if initial_state == "submitting":
        store.mark_submitting(record.task_id)

    recovered = service.status(record.task_id, "session-a")

    assert recovered["state"] == "submission_uncertain"
    assert recovered["recovery"]["action"] == "reconcile"
    assert recovered["recovery"]["safe_to_resubmit"] is False
    assert client.launches == []
    assert client.gets == []


def test_stale_after_dispatch_can_only_bind_by_verified_reconcile(
    tmp_path, profile, command_request
):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile, submission_stale_seconds=0)
    plan = service.plan(command_request)
    record, _ = store.claim(
        request_id="crash-after-dispatch",
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=command_request["workdir"],
        output_dir=command_request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    store.mark_submitting(record.task_id)
    client.entities[("command", "remote-after-crash")] = {
        "id": "remote-after-crash",
        "state": "RUNNING",
        "config": {"description": record.submission_marker},
    }

    assert service.status(record.task_id, "session-a")["state"] == "submission_uncertain"
    reconciled = service.reconcile(record.task_id, "session-a", "remote-after-crash")

    assert reconciled["remote_id"] == "remote-after-crash"
    assert reconciled["remote_state"] == "RUNNING"
    assert client.launches == []


def test_restart_preserves_task_and_owner_scope(tmp_path, profile, command_request):
    db_path = tmp_path / "tasks.db"
    first_store = SQLiteTaskStore(db_path)
    task = ComputeService(FakeClient(), first_store, profile).launch(
        command_request, "request-1", "session-a"
    )
    first_store.close()

    second_client = FakeClient()
    service = ComputeService(second_client, SQLiteTaskStore(db_path), profile)
    assert service.list_tasks("session-a")[0]["task_id"] == task["task_id"]
    assert service.list_tasks("session-b") == []
    with pytest.raises(NotFoundError):
        service.status(task["task_id"], "session-b")
    assert second_client.gets == []


def test_status_logs_cancel_preserve_remote_evidence(tmp_path, profile, command_request):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(command_request, "request-1", "session-a")
    remote_id = launched["remote_id"]
    client.entities[("command", remote_id)] = {
        "id": remote_id,
        "state": "TERMINATED",
        "exitStatus": 23,
        "failureReason": "process failed",
    }

    status = service.status(launched["task_id"], "session-a")

    assert status["remote_state"] == "TERMINATED"
    assert status["remote"]["exitStatus"] == 23
    assert status["remote"]["failureReason"] == "process failed"
    assert service.logs(launched["task_id"], "session-a", 10) == [{"message": "ok"}]
    cancelled = service.cancel(launched["task_id"], "session-a")
    assert cancelled["remote"]["state"] == "TERMINATING"


def test_empty_cancel_ack_preserves_last_observed_remote_state(
    tmp_path, profile, command_request
):
    client = EmptyCancelClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(command_request, "request-1", "session-a")
    remote_id = launched["remote_id"]
    client.entities[("command", remote_id)] = {"id": remote_id, "state": "RUNNING"}
    service.status(launched["task_id"], "session-a")

    cancelled = service.cancel(launched["task_id"], "session-a")

    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["remote_state"] == "RUNNING"
    assert cancelled["remote"] == {}


def test_changed_profile_or_cluster_cannot_query_bound_remote(
    tmp_path, profile, command_request
):
    client = FakeClient()
    db_path = tmp_path / "tasks.db"
    task = ComputeService(client, SQLiteTaskStore(db_path), profile).launch(
        command_request, "request-1", "session-a"
    )
    changed = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "other/image", "pool": "gpu", "slots": 1},
            "cluster_identity": "other-cluster",
        }
    )
    changed_client = FakeClient()
    service = ComputeService(changed_client, SQLiteTaskStore(db_path), changed)

    with pytest.raises(ConflictError) as caught:
        service.status(task["task_id"], "session-a")

    assert caught.value.code == "binding_mismatch"
    assert changed_client.gets == []


def test_same_cluster_label_with_changed_api_url_cannot_query_bound_remote(
    tmp_path, profile, command_request
):
    db_path = tmp_path / "tasks.db"
    first_client = FakeClient()
    task = ComputeService(first_client, SQLiteTaskStore(db_path), profile).launch(
        command_request, "request-1", "session-a"
    )
    changed_client = FakeClient()
    changed_client.api_url = "https://different-det.example.test"
    service = ComputeService(changed_client, SQLiteTaskStore(db_path), profile)

    with pytest.raises(ConflictError) as caught:
        service.status(task["task_id"], "session-a")

    assert caught.value.code == "binding_mismatch"
    assert changed_client.gets == []


def test_reconcile_requires_remote_identity_marker(tmp_path, profile, command_request):
    client = UncertainClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(command_request, "request-1", "session-a")
    task_id = caught.value.details["task_id"]
    marker = next(
        item.partition("=")[2]
        for item in client.launches[0][1]["environment"]["environment_variables"]
        if item.startswith("COMPUTE_SUBMISSION_MARKER=")
    )
    client.entities[("command", "remote-9")] = {
        "id": "remote-9",
        "state": "RUNNING",
        "submissionMarker": "determined-compute:00000000-0000-0000-0000-000000000000",
        "config": {
            "description": service.store.get_owned(
                task_id, "session-a"
            ).submission_marker
        },
    }

    with pytest.raises(ConflictError):
        service.reconcile(task_id, "session-a", "remote-9")

    client.entities[("command", "remote-9")]["submissionMarker"] = f"{marker}-wrong"
    with pytest.raises(ConflictError):
        service.reconcile(task_id, "session-a", "remote-9")

    client.entities[("command", "remote-9")]["submissionMarker"] = marker
    reconciled = service.reconcile(task_id, "session-a", "remote-9")
    assert reconciled["remote_id"] == "remote-9"


def test_reconcile_supports_legacy_first_line_description_marker(
    tmp_path, profile, command_request
):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile)
    plan = service.plan(command_request)
    record, _ = store.claim(
        request_id="legacy-request",
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=command_request["workdir"],
        output_dir=command_request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    store.mark_uncertain(record.task_id)
    assert record.name is None
    assert record.description is None
    client.entities[("command", "legacy-remote")] = {
        "id": "legacy-remote",
        "state": "RUNNING",
        "config": {"description": record.submission_marker + "\nold human text"},
    }

    reconciled = service.reconcile(record.task_id, "session-a", "legacy-remote")

    assert reconciled["remote_id"] == "legacy-remote"


def test_secret_request_values_are_not_persisted(tmp_path, profile):
    db_path = tmp_path / "tasks.db"
    secret = "SUPER_SECRET_VALUE_6d0b"
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "entrypoint": "python train.py",
            "environment": {"environment_variables": [f"TOKEN={secret}"]},
        },
    }
    service = ComputeService(FakeClient(), SQLiteTaskStore(db_path), profile)

    service.launch(request, "request-1", "session-a")

    assert secret.encode() not in db_path.read_bytes()


def test_profile_json_and_yaml(tmp_path):
    value = {
        "mounts": [{"host_path": "/host", "container_path": "/container"}],
        "defaults": {"image": "image", "pool": "pool", "slots": 0},
    }
    json_path = tmp_path / "profile.json"
    yaml_path = tmp_path / "profile.yaml"
    json_path.write_text(json.dumps(value), encoding="utf-8")
    yaml_path.write_text(
        "mounts:\n  - host_path: /host\n    container_path: /container\n"
        "defaults:\n  image: image\n  pool: pool\n  slots: 0\n",
        encoding="utf-8",
    )

    assert ComputeProfile.from_file(json_path) == ComputeProfile.from_file(yaml_path)


def test_store_additively_migrates_legacy_task_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE compute_tasks (
            task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, owner TEXT NOT NULL,
            payload_hash TEXT NOT NULL, profile_hash TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL, remote_id TEXT, remote_state TEXT, code_revision TEXT,
            workdir TEXT NOT NULL, output_dir TEXT NOT NULL, cluster_identity TEXT,
            submission_marker TEXT NOT NULL, error_code TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(owner, request_id)
        );
        INSERT INTO compute_tasks VALUES (
            'legacy-task', 'legacy-request', 'session-a', 'hash', 'profile', 'command',
            'submission_uncertain', NULL, NULL, 'rev', '/shared/code', '/shared/out',
            'cluster', 'determined-compute:00000000-0000-0000-0000-000000000001',
            'submission_uncertain', '2026-01-01T00:00:00.000Z',
            '2026-01-01T00:00:00.000Z'
        );
        """
    )
    connection.close()

    store = SQLiteTaskStore(db_path)
    record = store.get_owned("legacy-task", "session-a")

    assert record.name is None
    assert record.description is None
    columns = {
        row[1]
        for row in sqlite3.connect(db_path).execute("PRAGMA table_info(compute_tasks)")
    }
    assert {"name", "description"}.issubset(columns)


class GenericClient(FakeClient):
    """Fake client that also serves generic-task launch options and control routes."""

    def __init__(self, warnings=None):
        super().__init__()
        self.options = []
        self.controls = []
        self.warnings = warnings or []

    def launch_task(self, kind, config, options=None):
        self.options.append(options)
        entity = super().launch_task(kind, config)
        marker = next(
            item.partition("=")[2]
            for item in config["environment"]["environment_variables"]
            if item.startswith("COMPUTE_SUBMISSION_MARKER=")
        )
        self.entities[(kind, entity["id"])] = {
            "id": entity["id"], "state": "STATE_ACTIVE", "submissionMarker": marker,
        }
        return {"id": entity["id"], "warnings": list(self.warnings)}

    def pause_task(self, kind, remote_id):
        self.controls.append(("pause", kind, remote_id))
        return {"id": remote_id, "acknowledged": True}

    def unpause_task(self, kind, remote_id):
        self.controls.append(("unpause", kind, remote_id))
        return {"id": remote_id, "acknowledged": True}

    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        return {"id": remote_id, "acknowledged": True}


@pytest.fixture
def generic_request(command_request):
    return dict(
        command_request,
        kind="generic",
        name="eval-shards",
        description="Evaluate every shard; skips finished shards on resume.",
    )


def test_generic_plan_uses_native_metadata_and_command_entrypoint(
    tmp_path, profile, generic_request
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    command = service.plan(dict(generic_request, kind="command"))

    plan = service.plan(dict(generic_request, preemption_timeout=120, pausable=True))

    config = plan["config"]
    assert plan["kind"] == "generic"
    assert config["name"] == "eval-shards"
    assert config["description"] == "Evaluate every shard; skips finished shards on resume."
    assert config["entrypoint"] == command["config"]["entrypoint"]
    assert config["resources"] == {"slots": 1, "resource_pool": "gpu"}
    assert config["environment"] == command["config"]["environment"]
    assert config["bind_mounts"] == command["config"]["bind_mounts"]
    assert config["preemption_timeout"] == 120
    assert plan["task_options"] == {"parent": None, "inherit_context": False, "pausable": True}
    assert "generic_restart_safety" in {item["code"] for item in plan["advisories"]}
    # A task that cannot be paused is never rerun, so it needs no restart-safety advisory.
    default = service.plan(generic_request)
    assert default["task_options"]["pausable"] is False
    assert "generic_restart_safety" not in {item["code"] for item in default["advisories"]}
    # Other kinds keep their plan shape, so their idempotency hashes are unchanged.
    assert "task_options" not in command
    assert "preemption_timeout" not in command["config"]


def test_generic_plan_without_description_omits_it(tmp_path, profile, generic_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(generic_request)
    del request["description"]
    assert "description" not in service.plan(request)["config"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "command", "pausable": True}, "require generic kind"),
        ({"kind": "experiment", "preemption_timeout": 5}, "require generic kind"),
        ({"kind": "auto", "parent": "task"}, "require generic kind"),
        ({"inherit_context": True}, "inherit_context requires parent"),
        ({"preemption_timeout": -1}, "preemption_timeout"),
        ({"preemption_timeout": True}, "preemption_timeout"),
        ({"pausable": "yes"}, "booleans"),
        ({"parent": ""}, "parent"),
        ({"command": None}, "command"),
        ({"interactive": True}, "shell"),
        ({"experiment_config": {"entrypoint": "true"}}, "experiment kind"),
    ],
)
def test_generic_plan_validation(tmp_path, profile, generic_request, change, message):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(ValidationError, match=message):
        service.plan(dict(generic_request, **change))


def test_generic_launch_admits_capacity_and_surfaces_warnings(
    tmp_path, profile, generic_request
):
    warning = {"code": "generic_task_metadata_unsupported", "message": "kept locally"}
    client = GenericClient(warnings=[warning])
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    launched = service.launch(generic_request, "generic-1", "session-a")

    assert launched["kind"] == "generic"
    assert launched["state"] == "submitted"
    assert launched["name"] == "eval-shards"
    assert launched["description"].startswith("Evaluate every shard")
    assert launched["warnings"] == [warning]
    assert client.options == [{"noPause": True}]
    kind, config = client.launches[0]
    assert kind == "generic"
    assert config["name"] == "eval-shards"
    assert any(
        item.startswith("COMPUTE_SUBMISSION_MARKER=")
        for item in config["environment"]["environment_variables"]
    )
    # An idempotent retry returns the stored record without submitting again.
    again = service.launch(generic_request, "generic-1", "session-a")
    assert again["task_id"] == launched["task_id"]
    assert len(client.launches) == 1


def test_generic_parent_resolves_to_owned_remote_task(tmp_path, profile, generic_request):
    client = GenericClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    parent = service.launch(generic_request, "parent", "session-a")
    child_request = dict(
        generic_request, name="child", parent=parent["task_id"], inherit_context=True,
        pausable=True,
    )

    service.launch(child_request, "child", "session-a")

    assert client.options[-1] == {
        "parentId": parent["remote_id"], "inheritContext": True, "noPause": False,
    }


def test_generic_parent_must_be_an_owned_bound_generic_task(
    tmp_path, profile, generic_request, command_request
):
    client = GenericClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    parent = service.launch(generic_request, "parent", "session-a")
    command = service.launch(command_request, "command", "session-a")

    with pytest.raises(NotFoundError):
        service.launch(dict(generic_request, parent=parent["task_id"]), "x", "session-b")
    with pytest.raises(ValidationError, match="generic"):
        service.launch(dict(generic_request, parent=command["task_id"]), "y", "session-a")

    assert {item["request_id"] for item in service.list_tasks("session-a")} == {
        "command", "parent",
    }
    assert service.list_tasks("session-b") == []
    assert len(client.launches) == 2


def test_generic_status_logs_cancel_pause_and_resume(tmp_path, profile, generic_request):
    client = GenericClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(generic_request, "generic-1", "session-a")
    task_id, remote_id = launched["task_id"], launched["remote_id"]

    paused = service.pause(task_id, "session-a")
    assert paused["pause_acknowledged"] is True
    client.entities[("generic", remote_id)]["state"] = "STATE_PAUSED"
    status = service.status(task_id, "session-a")
    assert status["remote_state"] == "STATE_PAUSED"

    resumed = service.resume(task_id, "session-a")
    assert resumed["resume_acknowledged"] is True
    assert client.controls == [("pause", "generic", remote_id), ("unpause", "generic", remote_id)]

    assert service.logs(task_id, "session-a", 5) == [{"message": "ok"}]
    assert client.log_calls == [("generic", remote_id, 5)]
    cancelled = service.cancel(task_id, "session-a")
    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["remote_state"] == "STATE_PAUSED"
    assert client.cancel_calls == [("generic", remote_id)]


def test_experiment_pause_and_resume(tmp_path, profile, command_request):
    client = GenericClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(dict(command_request, kind="experiment"), "exp-1", "session-a")
    task_id, remote_id = launched["task_id"], launched["remote_id"]

    assert service.pause(task_id, "session-a")["pause_acknowledged"] is True
    assert service.resume(task_id, "session-a")["resume_acknowledged"] is True
    assert client.controls == [
        ("pause", "experiment", remote_id), ("unpause", "experiment", remote_id),
    ]


def test_pause_and_resume_apply_only_to_owned_bound_pausable_tasks(
    tmp_path, profile, generic_request, command_request
):
    client = GenericClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile)
    command = service.launch(command_request, "command", "session-a")
    generic = service.launch(generic_request, "generic", "session-a")

    for action in (service.pause, service.resume):
        with pytest.raises(ValidationError) as caught:
            action(command["task_id"], "session-a")
        assert caught.value.code == "unsupported_kind"
        with pytest.raises(NotFoundError):
            action(generic["task_id"], "session-b")

    record, _ = store.claim(
        request_id="unbound", owner="session-a", payload_hash="hash",
        profile_hash=profile.fingerprint, kind="generic", code_revision=None,
        workdir="/shared/container/jobs/code", output_dir="/shared/container/jobs/out",
        cluster_identity=service._cluster_identity(),
    )
    with pytest.raises(ConflictError) as caught:
        service.pause(record.task_id, "session-a")
    assert caught.value.code == "remote_id_unknown"
    assert client.controls == []


def test_uncertain_generic_submission_reconciles_by_marker(
    tmp_path, profile, generic_request
):
    class UncertainGeneric(GenericClient):
        def launch_task(self, kind, config, options=None):
            super().launch_task(kind, config, options)
            raise SubmissionUncertainError("connection closed after request")

    client = UncertainGeneric()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(generic_request, "generic-1", "session-a")
    task_id = caught.value.details["task_id"]
    remote = client.entities[("generic", "1")]

    reconciled = service.reconcile(task_id, "session-a", "1")

    assert reconciled["remote_id"] == "1"
    assert reconciled["remote_state"] == remote["state"]
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(dict(generic_request, name="other"), "generic-2", "session-a")
    other = caught.value.details["task_id"]
    # Another task's marker never binds this submission.
    client.entities[("generic", "2")]["submissionMarker"] = remote["submissionMarker"]
    with pytest.raises(ConflictError) as caught:
        service.reconcile(other, "session-a", "2")
    assert caught.value.code == "identity_mismatch"


def test_generic_adoption_rejects_invalid_ids_before_network(tmp_path, profile):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(ValidationError, match="must be a UUID"):
        service.adopt("generic", "not-a-uuid", "session-a")
    assert client.gets == []
