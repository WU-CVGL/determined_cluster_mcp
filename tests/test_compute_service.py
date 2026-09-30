from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from determined_compute import compute_cli
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
        self.cancel_response = None

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
        if self.cancel_response is not None:
            return self.cancel_response
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


def claim_unsubmitted(service, request, request_id):
    """Claim a record as a launch would, but stop before contacting the cluster."""
    plan = service.plan(request)
    record, created = service.store.claim(
        request_id=request_id,
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=service.profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=request["workdir"],
        output_dir=request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    assert created
    return record


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


def test_auto_kind_resolution_and_advisories(tmp_path, profile, command_request):
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

    overnight = service.plan(dict(command_request, overnight=True))

    assert overnight["kind"] == "experiment"
    assert overnight["config"]["resources"]["slots_per_trial"] == 1
    assert overnight["config"]["entrypoint"].endswith("python train.py --name 'space value'")


@pytest.mark.parametrize(
    "change",
    [
        {"workdir": "/local/code"},
        {"output_dir": "/shared/container/jobs/../escape"},
        {"workdir": "shared/container/code"},
        {"upload_context": "/tmp/context"},
        {"experiment_config": {"files": [{"path": "secret"}]}},
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


def test_read_only_mount_is_kept_for_reference_data_but_not_for_execution_paths(tmp_path):
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
        "command": ["python", "repair.py", "--registry", "/reference/registry"],
        "workdir": "/work/code",
        "output_dir": "/work/output",
    }

    plan = service.plan(request)

    assert {
        "host_path": "/shared/reference",
        "container_path": "/reference",
        "read_only": True,
    } in plan["config"]["bind_mounts"]
    for field in ("workdir", "output_dir"):
        with pytest.raises(ValidationError, match="read-only"):
            service.plan(dict(request, **{field: "/reference/task"}))


def test_profile_loading_validation_and_fingerprint(tmp_path):
    base = {
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool", "slots": 0},
    }
    json_path = tmp_path / "profile.json"
    yaml_path = tmp_path / "profile.yaml"
    json_path.write_text(json.dumps(base), encoding="utf-8")
    yaml_path.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n  slots: 0\n",
        encoding="utf-8",
    )
    assert ComputeProfile.from_file(json_path) == ComputeProfile.from_file(yaml_path)

    writable = ComputeProfile.from_dict(base)
    read_only = ComputeProfile.from_dict(
        {**base, "mounts": [{**base["mounts"][0], "read_only": True}]}
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


def test_request_id_submits_once_and_rejects_a_changed_payload(
    tmp_path, profile, command_request
):
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

    with pytest.raises(ConflictError) as caught:
        services[0].launch(
            dict(command_request, command="echo changed"), "same-request", "session-a"
        )
    assert caught.value.code == "idempotency_conflict"
    assert len(client.launches) == 1


def test_capacity_failure_creates_no_record_and_existing_request_skips_admission(
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

    inspector.available = False
    duplicate = service.launch(command_request, "capacity-retry", "session-a")
    assert duplicate["task_id"] == launched["task_id"]
    assert len(inspector.calls) == 2
    assert len(client.launches) == 1


@pytest.mark.parametrize(
    ("failure", "allow_queue"),
    [
        (APIError("Could not authenticate", code="transport_error", retryable=True), False),
        (RuntimeError("temporary login failure"), True),
    ],
    ids=["api_error", "other_error_with_allow_queue"],
)
def test_lazy_client_failure_happens_before_claim_and_request_can_retry(
    tmp_path, profile, command_request, failure, allow_queue
):
    client = FakeClient()
    endpoints = []

    def factory(api_url):
        endpoints.append(api_url)
        if len(endpoints) == 1:
            raise failure
        return client

    inspector = ToggleInspector(available=True)
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(
        compute_cli._LazyClient(factory, lambda: client.api_url),
        store,
        profile,
        inspector=inspector,
    )
    request = dict(command_request, allow_queue=allow_queue)

    with pytest.raises(type(failure)) as caught:
        service.launch(request, "lazy-retry", "session-a")

    assert caught.value is failure
    assert store.list_owned("session-a") == []
    assert inspector.calls == []
    assert client.launches == []

    launched = service.launch(request, "lazy-retry", "session-a")

    assert launched["state"] == "submitted"
    assert len(client.launches) == 1
    assert len(inspector.calls) == (0 if allow_queue else 1)
    assert endpoints == [client.api_url, client.api_url]
    assert store.get_owned(launched["task_id"], "session-a").cluster_identity == (
        service._cluster_identity()
    )
    # allow_queue is a local option: it never reaches the cluster configuration.
    assert "allow_queue" not in client.launches[0][1]


def test_uncertain_submission_is_never_retried_and_binds_only_by_its_marker(
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

    task_id = caught.value.details["task_id"]
    assert caught.value.code == "submission_uncertain"
    assert service.status(task_id, "session-a")["state"] == "submission_uncertain"
    duplicate = service.launch(request, "named-command", "session-a")
    assert duplicate["task_id"] == task_id
    assert duplicate["state"] == "submission_uncertain"
    assert len(client.launches) == 1

    # The display name and description are the remote description; the marker is separate.
    description = client.launches[0][1]["description"]
    assert description.splitlines() == [
        "pilot waypoint evaluation",
        "Evaluate the short validation route.",
    ]
    marker = next(
        item.partition("=")[2]
        for item in client.launches[0][1]["environment"]["environment_variables"]
        if item.startswith("COMPUTE_SUBMISSION_MARKER=")
    )
    entity = {
        "id": "remote-9",
        "state": "RUNNING",
        "submissionMarker": "determined-compute:00000000-0000-0000-0000-000000000000",
        # The description carries this record's marker, but a record with display
        # metadata is bound only by the native marker.
        "config": {
            "description": service.store.get_owned(task_id, "session-a").submission_marker
        },
    }
    client.entities[("command", "remote-9")] = entity

    for wrong in (entity["submissionMarker"], f"{marker}-wrong"):
        entity["submissionMarker"] = wrong
        with pytest.raises(ConflictError) as rejected:
            service.reconcile(task_id, "session-a", "remote-9")
        assert rejected.value.code == "identity_mismatch"

    entity.update({"submissionMarker": marker, "config": {"description": description}})
    assert service.reconcile(task_id, "session-a", "remote-9")["remote_id"] == "remote-9"


def test_experiment_top_level_metadata_overrides_native_metadata(tmp_path, profile):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "name": "native experiment name",
            "description": "native description",
            "entrypoint": "python evaluate.py",
        },
    }

    plan = service.plan(request)

    assert plan["name"] == "native experiment name"
    assert plan["description"] == "native description"
    assert plan["config"]["name"] == "native experiment name"
    assert plan["config"]["description"] == "native description"
    assert "generated_task_name" not in {item["code"] for item in plan["advisories"]}

    named = dict(
        request, name="skill retention pass 237", description="Top-level operator description"
    )
    service.launch(named, "named-experiment", "session-a")

    config = service.client.launches[0][1]
    assert config["name"] == "skill retention pass 237"
    assert config["description"] == "Top-level operator description"
    assert any(
        item.startswith("COMPUTE_SUBMISSION_MARKER=determined-compute:")
        for item in config["environment"]["environment_variables"]
    )


def test_generated_and_repeated_display_names_are_not_task_identity(
    tmp_path, profile, command_request
):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    task = service.launch(command_request, "unnamed-command", "session-a")

    assert client.launches[0][1]["description"] == "command: code"
    assert task["name"] == "command: code"
    assert task["description"] is None
    assert service.list_tasks("session-a")[0]["name"] == "command: code"

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


def test_stale_records_become_uncertain_and_bind_only_by_verified_reconcile(
    tmp_path, profile, command_request
):
    client = FakeClient()
    service = ComputeService(
        client, SQLiteTaskStore(tmp_path / "tasks.db"), profile, submission_stale_seconds=0
    )
    pending = claim_unsubmitted(service, command_request, "crash-pending")
    dispatched = claim_unsubmitted(service, command_request, "crash-after-dispatch")
    service.store.mark_submitting(dispatched.task_id)

    for record in (pending, dispatched):
        recovered = service.status(record.task_id, "session-a")
        assert recovered["state"] == "submission_uncertain"
        assert recovered["recovery"]["action"] == "reconcile"
        assert recovered["recovery"]["safe_to_resubmit"] is False
    assert client.gets == []

    # A record without display metadata also accepts a legacy marker on the first line
    # of the remote description.
    assert dispatched.name is None and dispatched.description is None
    client.entities[("command", "remote-after-crash")] = {
        "id": "remote-after-crash",
        "state": "RUNNING",
        "config": {"description": dispatched.submission_marker + "\nold human text"},
    }
    reconciled = service.reconcile(dispatched.task_id, "session-a", "remote-after-crash")

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
    client = FakeClient()
    client.cancel_response = {}
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(command_request, "request-1", "session-a")
    service.status(launched["task_id"], "session-a")

    cancelled = service.cancel(launched["task_id"], "session-a")

    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["remote_state"] == "RUNNING"
    assert cancelled["remote"] == {}


def test_changed_binding_cannot_query_bound_remote(tmp_path, profile, command_request):
    db_path = tmp_path / "tasks.db"
    task = ComputeService(FakeClient(), SQLiteTaskStore(db_path), profile).launch(
        command_request, "request-1", "session-a"
    )
    changed_profile = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "other/image", "pool": "gpu", "slots": 1},
            "cluster_identity": "other-cluster",
        }
    )
    changed_endpoint = FakeClient()
    changed_endpoint.api_url = "https://different-det.example.test"

    for client, bound_profile in (
        (FakeClient(), changed_profile),
        # The same cluster label at a different API URL is a different binding.
        (changed_endpoint, profile),
    ):
        service = ComputeService(client, SQLiteTaskStore(db_path), bound_profile)
        with pytest.raises(ConflictError) as caught:
            service.status(task["task_id"], "session-a")
        assert caught.value.code == "binding_mismatch"
        assert client.gets == []


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


# Payload hashes of requests rendered by release 0.5.0. Idempotent retries of existing
# request IDs depend on these values, so a change here needs a migration plan.
GOLDEN_PAYLOAD_HASHES = {
    "command": "9c8ff0c3cd4b8fef968d7cba4de97058d99e94a4cd774fb3afc768c44a4c2483",
    "named": "bc6a23b3a0c9a21c647f33bf9c47443331161b6c46f2507f48f781203f666c94",
    "shell": "18d6875cae55e4b72bbf18a60f283b9d294fc4c55d6e050ac6f82025b16ce8ac",
    "experiment": "be87af468ac7b97290596b1d92ecd83d0a95f13a72046535bac4dc2865bc67ec",
}


def test_existing_request_payload_hashes_and_profile_fingerprint_are_stable(
    profile, command_request
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(":memory:"), profile)
    requests = {
        "command": command_request,
        "named": {
            "name": "n",
            "description": "d",
            "command": "true",
            "workdir": "/shared/container/a",
            "output_dir": "/shared/container/b",
            "slots": 2,
        },
        "shell": {
            "interactive": True,
            "workdir": "/shared/container/a",
            "output_dir": "/shared/container/b",
        },
        "experiment": {
            "name": "exp",
            "kind": "experiment",
            "command": ["python", "t.py"],
            "workdir": "/shared/container/a",
            "output_dir": "/shared/container/b",
            "experiment_config": {
                "searcher": {"name": "single", "metric": "m", "max_length": {"batches": 1}},
                "checkpoint_storage": {"type": "shared_fs", "host_path": "/shared/host/ckpt"},
                "environment": {"environment_variables": ["A=1"]},
            },
        },
    }

    assert profile.fingerprint == (
        "5b58b5378262430e4c35235eefca45e5b65d089c2c2316767ea7ca4eee19915d"
    )
    for key, request in requests.items():
        rendered = service._render(request)
        assert service._payload_hash(rendered) == GOLDEN_PAYLOAD_HASHES[key], key
        # Without a path inspector, planning returns exactly the hashed render.
        assert service.plan(request) == rendered
