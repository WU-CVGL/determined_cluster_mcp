from __future__ import annotations

import copy
import json
import uuid

import pytest

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    SubmissionUncertainError,
    ValidationError,
)


def marker_of(config):
    return next(
        item.partition("=")[2]
        for item in config["environment"]["environment_variables"]
        if item.startswith("COMPUTE_SUBMISSION_MARKER=")
    )


class FakeClient:
    """Fake Determined master that keeps every launched task with its owner and marker."""

    api_url = "https://det.example.test"

    def __init__(self):
        self.user = {"id": "7", "username": "alice"}
        self.me_calls = 0
        self.launches = []
        self.options = []
        self.gets = []
        self.log_calls = []
        self.cancel_calls = []
        self.controls = []
        self.entities = {}
        self.launch_warnings = []
        self.generic_list = True
        self.probes = 0

    def get_current_user(self):
        self.me_calls += 1
        return dict(self.user)

    def require_generic_task_list(self):
        self.probes += 1
        if not self.generic_list:
            raise APIError("cannot list generic tasks with their owners", code="unsupported")

    def _create(self, kind, config, options=None):
        self.launches.append((kind, copy.deepcopy(config)))
        self.options.append(options)
        number = len(self.launches)
        remote_id = str(number) if kind == "experiment" else str(uuid.UUID(int=number))
        entity = {
            "id": number if kind == "experiment" else remote_id,
            "userId": 7,
            "username": "alice",
            "state": "STATE_ACTIVE" if kind in {"experiment", "generic"} else "QUEUED",
            "resourcePool": config["resources"]["resource_pool"],
            "submissionMarker": marker_of(config),
        }
        for field in ("name", "description"):
            if field in config:
                entity[field] = config[field]
        self.entities[(kind, remote_id)] = entity
        return remote_id, entity

    def launch_task(self, kind, config, options=None):
        remote_id, entity = self._create(kind, config, options)
        if kind == "generic":
            return {"id": remote_id, "warnings": list(self.launch_warnings)}
        result = copy.deepcopy(entity)
        result.pop("submissionMarker")
        return result

    def get_task(self, kind, remote_id):
        self.gets.append((kind, remote_id))
        if (kind, remote_id) in self.entities:
            return copy.deepcopy(self.entities[(kind, remote_id)])
        if any(key[1] == remote_id for key in self.entities):
            raise APIError("Determined task is not a generic task", code="kind_mismatch")
        raise APIError(f"404 {kind} not found", code=404)

    def task_logs(self, kind, remote_id, tail):
        self.log_calls.append((kind, remote_id, tail))
        return [{"message": "ok"}]

    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        if kind in {"experiment", "generic"}:
            return {"id": remote_id, "acknowledged": True}
        return {"id": remote_id, "state": "TERMINATING"}

    def pause_task(self, kind, remote_id):
        self.controls.append(("pause", kind, remote_id))
        return {"id": remote_id, "acknowledged": True}

    def unpause_task(self, kind, remote_id):
        self.controls.append(("unpause", kind, remote_id))
        return {"id": remote_id, "acknowledged": True}

    def list_remote_tasks(self, kind, *, user_id, limit, offset):
        owned = [
            {key: value for key, value in entity.items() if key != "submissionMarker"}
            for (entity_kind, _id), entity in reversed(list(self.entities.items()))
            if entity_kind == kind and str(entity.get("userId")) == user_id
        ]
        return {
            "tasks": owned[offset:offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": len(owned)},
        }

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
                        "slots": {"0": {"id": "0", "enabled": True, "draining": False}},
                    }
                ]
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")


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
    service = ComputeService(client, profile)

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
        "mkdir -p /shared/container/jobs/out && cd /shared/container/jobs/code || exit $?\n"
        "python train.py --name 'space value'",
    ]


def test_auto_modes_and_shell_advisory(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), profile)
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
    service = ComputeService(FakeClient(), profile)

    command = service.plan(command_request)
    shell_request = dict(command_request)
    shell_request.update({"kind": "shell", "interactive": True})
    shell_request.pop("command")
    shell = service.plan(shell_request)

    assert command["config"]["entrypoint"][:2] == ["/bin/bash", "-lc"]
    assert isinstance(command["config"]["entrypoint"][2], str)
    assert "entrypoint" not in shell["config"]


def test_overnight_command_becomes_experiment(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), profile)
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
    service = ComputeService(FakeClient(), profile)
    request = dict(command_request)
    request.update(change)
    if "experiment_config" in change:
        request.update({"kind": "experiment", "command": None})

    with pytest.raises(ValidationError):
        service.plan(request)


def test_context_named_hyperparameters_are_allowed(tmp_path, profile):
    service = ComputeService(FakeClient(), profile)
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
    service = ComputeService(FakeClient(), profile)

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
    service = ComputeService(FakeClient(), profile)
    request = {
        "command": ["true"],
        "workdir": "/work/code",
        "output_dir": "/work/output",
    }
    request[field] = "/reference/task"

    with pytest.raises(ValidationError, match="read-only"):
        service.plan(request)


def test_read_only_mount_validation():
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

    invalid = {**base, "mounts": [{**base["mounts"][0], "read_only": "true"}]}
    with pytest.raises(ValidationError, match="must be a boolean"):
        ComputeProfile.from_dict(invalid)


def test_submission_marker_environment_name_is_reserved(tmp_path, profile):
    service = ComputeService(FakeClient(), profile)
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


def test_experiment_honors_native_metadata_without_top_level_values(tmp_path, profile):
    service = ComputeService(FakeClient(), profile)
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
    service = ComputeService(FakeClient(), profile)
    with pytest.raises(ValidationError):
        service.plan(dict(command_request, **{field: value}))


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
    service = ComputeService(FakeClient(), profile)
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
    # Other kinds keep their plan shape.
    assert "task_options" not in command
    assert "preemption_timeout" not in command["config"]


def test_generic_plan_without_description_omits_it(tmp_path, profile, generic_request):
    service = ComputeService(FakeClient(), profile)
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
        ({"parent": "not-a-uuid"}, "parent"),
        ({"parent": 7}, "parent"),
        ({"command": None}, "command"),
        ({"interactive": True}, "shell"),
        ({"experiment_config": {"entrypoint": "true"}}, "experiment kind"),
    ],
)
def test_generic_plan_validation(tmp_path, profile, generic_request, change, message):
    service = ComputeService(FakeClient(), profile)
    with pytest.raises(ValidationError, match=message):
        service.plan(dict(generic_request, **change))


def test_profile_rejects_removed_cluster_identity():
    with pytest.raises(ValidationError, match="cluster_identity"):
        ComputeProfile.from_dict(
            {
                "mounts": [{"host_path": "/host", "container_path": "/container"}],
                "defaults": {"image": "image", "pool": "pool"},
                "cluster_identity": "label",
            }
        )


def test_launch_returns_native_command_id_and_its_marker(profile, command_request):
    client = FakeClient()
    service = ComputeService(client, profile)
    request = dict(
        command_request,
        name="pilot waypoint evaluation",
        description="Evaluate the short validation route.",
        allow_queue=True,
    )

    launched = service.launch(request)

    kind, config = client.launches[0]
    assert kind == "command"
    assert config["description"].splitlines() == [
        "pilot waypoint evaluation",
        "Evaluate the short validation route.",
    ]
    assert launched == {
        "kind": "command",
        "id": str(uuid.UUID(int=1)),
        "name": "pilot waypoint evaluation",
        "description": "Evaluate the short validation route.",
        "state": "QUEUED",
        "submission_marker": marker_of(config),
        "advisories": [],
    }
    assert launched["submission_marker"].startswith("determined-compute:")
    assert "determined-compute:" not in config["description"]
    assert "allow_queue" not in config


def test_launch_returns_integer_experiment_id(profile, command_request):
    service = ComputeService(FakeClient(), profile)

    launched = service.launch(dict(command_request, kind="experiment", allow_queue=True))

    assert launched["kind"] == "experiment"
    assert launched["id"] == 1
    assert service.status("experiment", launched["id"])["id"] == 1


def test_every_launch_is_a_new_submission(profile, command_request):
    client = FakeClient()
    service = ComputeService(client, profile)
    request = dict(command_request, name="repeated human name", allow_queue=True)

    first = service.launch(request)
    second = service.launch(request)

    assert len(client.launches) == 2
    assert first["name"] == second["name"] == "repeated human name"
    assert first["id"] != second["id"]
    assert first["submission_marker"] != second["submission_marker"]


def test_unnamed_submission_generates_human_name(profile, command_request):
    client = FakeClient()
    service = ComputeService(client, profile)

    task = service.launch(dict(command_request, allow_queue=True))

    assert client.launches[0][1]["description"] == "command: code"
    assert task["name"] == "command: code"
    assert task["description"] is None
    assert "generated_task_name" in {item["code"] for item in task["advisories"]}


def test_named_experiment_top_level_metadata_overrides_native_metadata(profile):
    client = FakeClient()
    service = ComputeService(client, profile)
    request = {
        "kind": "experiment",
        "name": "skill retention pass 237",
        "description": "Top-level operator description",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "allow_queue": True,
        "experiment_config": {
            "name": "old generated experiment name",
            "description": "Five-clip deterministic decoder evaluation",
            "entrypoint": "python evaluate.py",
        },
    }

    service.launch(request)

    config = client.launches[0][1]
    assert config["name"] == "skill retention pass 237"
    assert config["description"] == "Top-level operator description"
    assert marker_of(config).startswith("determined-compute:")


def test_capacity_failure_submits_nothing(profile, command_request):
    inspector = ToggleInspector(available=False)
    client = FakeClient()
    service = ComputeService(client, profile, inspector=inspector)

    with pytest.raises(APIError) as caught:
        service.launch(command_request)

    assert caught.value.code == "capacity_unavailable"
    assert client.launches == []

    inspector.available = True
    service.launch(command_request)
    assert len(client.launches) == 1


def test_allow_queue_bypasses_admission(profile, command_request):
    inspector = ToggleInspector(available=False)
    client = FakeClient()
    service = ComputeService(client, profile, inspector=inspector)

    service.launch(dict(command_request, allow_queue=True))

    assert inspector.calls == []
    assert len(client.launches) == 1


class _MissingId(FakeClient):
    def launch_task(self, kind, config, options=None):
        self._create(kind, config, options)
        return {"warnings": []}


class _Disconnected(FakeClient):
    def launch_task(self, kind, config, options=None):
        self._create(kind, config, options)
        raise SubmissionUncertainError("Determined mutation outcome is unknown")


class _Crashed(FakeClient):
    def launch_task(self, kind, config, options=None):
        self._create(kind, config, options)
        raise RuntimeError("worker died")


@pytest.mark.parametrize("client_type", [_Disconnected, _Crashed, _MissingId])
@pytest.mark.parametrize("kind", ["command", "generic", "experiment"])
def test_uncertain_launch_returns_the_marker_and_is_not_retried(
    profile, command_request, client_type, kind
):
    client = client_type()
    service = ComputeService(client, profile)

    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(dict(command_request, kind=kind, allow_queue=True))

    assert len(client.launches) == 1
    marker = marker_of(client.launches[0][1])
    error = caught.value
    assert error.code == "submission_uncertain"
    assert error.retryable is False
    assert error.details == {"kind": kind, "submission_marker": marker}
    assert "unconfirmed" in str(error)
    assert f"compute_list(kind={kind!r}, marker={marker!r})" in str(error)
    assert "does not prove that the submission failed" in str(error)

    # The marker finds the task the master did create, without launching again.
    found = service.list_tasks(kind, marker=marker)
    assert [task["submission_marker"] for task in found["tasks"]] == [marker]
    assert len(client.launches) == 1


def test_a_task_that_appears_after_an_empty_search_is_found_and_never_relaunched(
    profile, command_request
):
    class Slow(FakeClient):
        """The master stores the task only after the client gave up waiting."""

        def launch_task(self, kind, config, options=None):
            self.pending = (kind, config, options)
            self.launches.append((kind, copy.deepcopy(config)))
            raise SubmissionUncertainError("Determined mutation outcome is unknown")

    client = Slow()
    service = ComputeService(client, profile)

    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(dict(command_request, allow_queue=True))

    message = str(caught.value)
    assert "does not prove that the submission failed" in message
    assert "Do not launch again automatically" in message
    assert "safe" not in message and "not created" not in message
    marker = caught.value.details["submission_marker"]
    assert service.list_tasks("command", marker=marker)["tasks"] == []

    kind, config, options = client.pending
    client.launches.pop()
    remote_id, _entity = client._create(kind, config, options)
    found = service.list_tasks("command", marker=marker)

    assert [task["id"] for task in found["tasks"]] == [remote_id]
    # The service never submitted the request a second time.
    assert len(client.launches) == 1


def test_a_fresh_service_continues_by_native_id_without_local_files(
    tmp_path, monkeypatch, profile, command_request
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    master = FakeClient()
    launched = ComputeService(master, profile).launch(dict(command_request, allow_queue=True))

    # A new process: a new client and service that share nothing but the master's state.
    client = FakeClient()
    client.entities = master.entities
    fresh = ComputeService(client, profile)

    assert fresh.status("command", launched["id"])["state"] == "QUEUED"
    assert fresh.logs("command", launched["id"], 3) == [{"message": "ok"}]
    assert fresh.cancel("command", launched["id"])["cancellation_acknowledged"] is True
    assert client.cancel_calls == [("command", launched["id"])]
    assert list(tmp_path.iterdir()) == []


def test_definite_rejection_is_not_reported_as_uncertain(profile, command_request):
    class Rejecting(FakeClient):
        def launch_task(self, kind, config, options=None):
            self.launches.append((kind, config))
            raise APIError("400 invalid config", code=400)

    client = Rejecting()
    service = ComputeService(client, profile)

    with pytest.raises(APIError) as caught:
        service.launch(dict(command_request, allow_queue=True))

    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.code == 400
    assert len(client.launches) == 1


@pytest.fixture
def generic_launch(command_request):
    return dict(
        command_request,
        kind="generic",
        name="eval-shards",
        description="Evaluate every shard; skips finished shards on resume.",
        allow_queue=True,
    )


def test_generic_launch_admits_capacity_and_surfaces_warnings(profile, generic_launch):
    warning = {"code": "launch_warning", "message": "LAUNCH_WARNING_CURRENT_SLOTS_EXCEEDED"}
    client = FakeClient()
    client.launch_warnings = [warning]
    inspector = ToggleInspector()
    service = ComputeService(client, profile, inspector=inspector)

    launched = service.launch(dict(generic_launch, allow_queue=False))

    assert launched["kind"] == "generic"
    assert launched["id"] == str(uuid.UUID(int=1))
    assert launched["name"] == "eval-shards"
    assert launched["warnings"] == [warning]
    assert client.options == [{"noPause": True}]
    assert [kind for kind, _config in inspector.calls] == ["generic"]
    assert client.probes == 1


def test_generic_launch_needs_a_master_that_lists_generic_tasks(profile, generic_launch):
    client = FakeClient()
    client.generic_list = False
    inspector = ToggleInspector()
    service = ComputeService(client, profile, inspector=inspector)
    parent = str(uuid.UUID(int=5))

    with pytest.raises(APIError) as caught:
        service.launch(dict(generic_launch, parent=parent, allow_queue=False))

    assert caught.value.code == "unsupported"
    # Refused before the parent, the capacity or the master's create route was touched.
    assert client.launches == [] and client.gets == [] and inspector.calls == []


def test_generic_parent_is_an_owned_remote_generic_task(profile, generic_launch):
    client = FakeClient()
    service = ComputeService(client, profile)
    parent = service.launch(generic_launch)

    service.launch(
        dict(generic_launch, name="child", parent=parent["id"].upper(),
             inherit_context=True, pausable=True)
    )

    assert client.options[-1] == {
        "parentId": parent["id"], "inheritContext": True, "noPause": False,
    }
    assert ("generic", parent["id"]) in client.gets


@pytest.mark.parametrize(
    ("setup", "error", "code"),
    [
        ("other_user", ConflictError, "ownership_mismatch"),
        ("ownerless", APIError, "ownership_unavailable"),
        ("command", APIError, "kind_mismatch"),
        ("missing", APIError, 404),
    ],
)
def test_generic_parent_must_be_an_owned_generic_task(
    profile, generic_launch, command_request, setup, error, code
):
    client = FakeClient()
    service = ComputeService(client, profile)
    if setup == "command":
        parent_id = service.launch(dict(command_request, allow_queue=True))["id"]
    elif setup == "missing":
        parent_id = str(uuid.UUID(int=99))
    else:
        parent_id = service.launch(generic_launch)["id"]
        entity = client.entities[("generic", parent_id)]
        if setup == "other_user":
            entity["userId"] = 8
        else:
            del entity["userId"]  # a master without the generic task list reports no owner
    launches = len(client.launches)

    with pytest.raises(error) as caught:
        service.launch(dict(generic_launch, parent=parent_id))

    assert caught.value.code == code
    assert len(client.launches) == launches


def test_generic_status_logs_cancel_pause_and_resume(profile, generic_launch):
    client = FakeClient()
    service = ComputeService(client, profile)
    task_id = service.launch(dict(generic_launch, pausable=True))["id"]

    paused = service.pause("generic", task_id)
    assert paused["pause_acknowledged"] is True
    assert paused["id"] == task_id and paused["name"] == "eval-shards"
    client.entities[("generic", task_id)]["state"] = "STATE_PAUSED"
    status = service.status("generic", task_id)
    assert status["state"] == "STATE_PAUSED"
    assert status["submission_marker"].startswith("determined-compute:")
    assert status["remote"]["id"] == task_id

    assert service.resume("generic", task_id)["resume_acknowledged"] is True
    assert client.controls == [("pause", "generic", task_id), ("unpause", "generic", task_id)]

    assert service.logs("generic", task_id, 5) == [{"message": "ok"}]
    assert client.log_calls == [("generic", task_id, 5)]
    cancelled = service.cancel("generic", task_id)
    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["state"] == "STATE_PAUSED"
    assert client.cancel_calls == [("generic", task_id)]


def test_command_cancel_reports_the_remote_state(profile, command_request):
    client = FakeClient()
    service = ComputeService(client, profile)
    task_id = service.launch(dict(command_request, allow_queue=True))["id"]

    cancelled = service.cancel("command", task_id)

    assert cancelled["state"] == "TERMINATING"
    assert cancelled["remote"] == {"id": task_id, "state": "TERMINATING"}
    assert client.cancel_calls == [("command", task_id)]


def test_experiment_pause_and_resume(profile, command_request):
    client = FakeClient()
    service = ComputeService(client, profile)
    experiment_id = service.launch(dict(command_request, kind="experiment", allow_queue=True))["id"]

    assert service.pause("experiment", experiment_id)["pause_acknowledged"] is True
    assert service.resume("experiment", str(experiment_id))["resume_acknowledged"] is True
    assert client.controls == [("pause", "experiment", "1"), ("unpause", "experiment", "1")]


@pytest.mark.parametrize("kind", ["command", "shell"])
def test_pause_and_resume_reject_commands_and_shells_before_network(profile, kind):
    client = FakeClient()
    service = ComputeService(client, profile)

    for action in (service.pause, service.resume):
        with pytest.raises(ValidationError) as caught:
            action(kind, str(uuid.UUID(int=1)))
        assert caught.value.code == "unsupported_kind"
    assert client.me_calls == 0 and client.gets == [] and client.controls == []
