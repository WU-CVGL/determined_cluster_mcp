from __future__ import annotations

import copy
import hashlib
import json
import shlex

import pytest

from determined_compute.compute import (
    ComputeProfile,
    ComputeService,
    ConflictError,
    SQLiteTaskStore,
)


class NoRemoteClient:
    api_url = "https://det.example.test"

    def launch_task(self, kind, config):
        raise AssertionError("legacy retries must not submit")


class NoAdmission:
    def __init__(self):
        self.calls = 0

    def require_capacity(self, kind, config):
        self.calls += 1
        raise AssertionError("legacy retries must not run admission")


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "image:stable", "pool": "gpu", "slots": 1},
            "shell_inactivity_seconds": 7200,
            "cluster_identity": "legacy-cluster",
        }
    )


def _base_config(kind, profile, request):
    variables = [
        f"COMPUTE_WORKDIR={request['workdir']}",
        f"COMPUTE_OUTPUT_DIR={request['output_dir']}",
    ]
    if request.get("code_revision") is not None:
        variables.append(f"COMPUTE_CODE_REVISION={request['code_revision']}")
    return {
        "resources": {
            ("slots_per_trial" if kind == "experiment" else "slots"): request.get(
                "slots", profile.default_slots
            ),
            "resource_pool": request.get("pool", profile.default_pool),
        },
        "environment": {
            "image": request.get("image", profile.default_image),
            "environment_variables": variables,
        },
        "bind_mounts": [
            {"host_path": "/shared/host", "container_path": "/shared/container"}
        ],
    }


def _script(request):
    command = request["command"]
    rendered = (
        command
        if isinstance(command, str)
        else " ".join(shlex.quote(part) for part in command)
    )
    return (
        f"mkdir -p {shlex.quote(request['output_dir'])} && "
        f"cd {shlex.quote(request['workdir'])} && {rendered}"
    )


def _old_plan(profile, request):
    raw_kind = request.get("kind", "auto")
    if raw_kind == "auto":
        if request.get("interactive", False):
            kind = "shell"
        elif request.get("overnight", False) or request.get("experiment_config") is not None:
            kind = "experiment"
        else:
            kind = "command"
    else:
        kind = raw_kind
    if kind == "experiment":
        config = copy.deepcopy(request.get("experiment_config") or {})
        resources = dict(config.get("resources", {}))
        resources.update(
            {
                "slots_per_trial": request.get("slots", profile.default_slots),
                "resource_pool": request.get("pool", profile.default_pool),
            }
        )
        config["resources"] = resources
        environment = dict(config.get("environment", {}))
        variables = list(environment.get("environment_variables", []))
        environment["image"] = request.get("image", profile.default_image)
        environment["environment_variables"] = variables + _base_config(
            kind, profile, request
        )["environment"]["environment_variables"]
        config["environment"] = environment
        config["bind_mounts"] = _base_config(kind, profile, request)["bind_mounts"]
        if request.get("command") is not None:
            config["entrypoint"] = _script(request)
        else:
            original = request["experiment_config"]["entrypoint"]
            command_request = dict(request, command=original)
            config["entrypoint"] = _script(command_request)
    else:
        config = _base_config(kind, profile, request)
    if kind == "command":
        config["entrypoint"] = ["/bin/bash", "-lc", _script(request)]
    advisories = []
    if request.get("overnight", False) and kind != "experiment":
        advisories.append(
            {
                "code": "overnight_experiment_recommended",
                "message": "Long or overnight work is more robust as an experiment.",
            }
        )
    if kind == "shell" and profile.shell_inactivity_seconds is not None:
        advisories.append(
            {
                "code": "shell_inactivity_policy",
                "seconds": profile.shell_inactivity_seconds,
                "message": (
                    "The deployment may stop an inactive shell after "
                    f"{profile.shell_inactivity_seconds} seconds."
                ),
            }
        )
    return {
        "kind": kind,
        "config": config,
        "code_revision": request.get("code_revision"),
        "advisories": advisories,
    }


def _old_hash(profile, request):
    cluster_identity = json.dumps(
        {"endpoint": "https://det.example.test", "label": "legacy-cluster"},
        sort_keys=True,
        separators=(",", ":"),
    )
    value = {
        "plan": _old_plan(profile, request),
        "profile_hash": profile.fingerprint,
        "cluster_identity": cluster_identity,
    }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requests():
    common = {
        "workdir": "/shared/container/jobs/code",
        "output_dir": "/shared/container/jobs/out",
        "code_revision": "rev-123",
    }
    return {
        "command": {**common, "kind": "command", "command": ["python", "job.py"]},
        "shell": {**common, "kind": "shell", "interactive": True},
        "experiment": {
            **common,
            "kind": "experiment",
            "experiment_config": {
                "name": "native legacy experiment",
                "description": None,
                "entrypoint": "python train.py",
            },
        },
    }


def _insert_legacy(service, profile, request, request_id, *, current_metadata=False):
    old_plan = _old_plan(profile, request)
    record, created = service.store.claim(
        request_id=request_id,
        owner="session-a",
        payload_hash=_old_hash(profile, request),
        profile_hash=profile.fingerprint,
        kind=old_plan["kind"],
        code_revision=old_plan["code_revision"],
        workdir=request["workdir"],
        output_dir=request["output_dir"],
        cluster_identity=service._cluster_identity(),
        name="current task" if current_metadata else None,
        description=None,
    )
    assert created
    return service.store.mark_uncertain(record.task_id)


@pytest.mark.parametrize("kind", ["command", "shell", "experiment"])
def test_exact_legacy_retry_returns_existing_without_admission_or_submit(
    tmp_path, profile, kind
):
    inspector = NoAdmission()
    service = ComputeService(
        NoRemoteClient(),
        SQLiteTaskStore(tmp_path / "tasks.db"),
        profile,
        inspector=inspector,
    )
    request = _requests()[kind]
    record = _insert_legacy(service, profile, request, f"legacy-{kind}")

    result = service.launch(request, f"legacy-{kind}", "session-a")

    assert result["task_id"] == record.task_id
    assert result["state"] == "submission_uncertain"
    assert inspector.calls == 0


def test_legacy_retry_rejects_changed_payload_and_native_experiment_metadata(
    tmp_path, profile
):
    service = ComputeService(
        NoRemoteClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile, inspector=NoAdmission()
    )
    command = _requests()["command"]
    _insert_legacy(service, profile, command, "legacy-command")
    with pytest.raises(ConflictError, match="different request"):
        service.launch(
            dict(command, command=["python", "changed.py"]),
            "legacy-command",
            "session-a",
        )

    experiment = _requests()["experiment"]
    _insert_legacy(service, profile, experiment, "legacy-experiment")
    changed = copy.deepcopy(experiment)
    changed["experiment_config"]["name"] = "changed native name"
    with pytest.raises(ConflictError, match="different request"):
        service.launch(changed, "legacy-experiment", "session-a")


@pytest.mark.parametrize(
    "new_field", [{"name": "new"}, {"description": "new"}, {"allow_queue": False}]
)
def test_legacy_retry_rejects_explicit_new_request_fields(tmp_path, profile, new_field):
    service = ComputeService(
        NoRemoteClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile, inspector=NoAdmission()
    )
    request = _requests()["command"]
    _insert_legacy(service, profile, request, "legacy-command")

    with pytest.raises(ConflictError, match="different request"):
        service.launch(dict(request, **new_field), "legacy-command", "session-a")


def test_current_metadata_record_never_uses_legacy_hash_fallback(tmp_path, profile):
    service = ComputeService(
        NoRemoteClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile, inspector=NoAdmission()
    )
    request = _requests()["command"]
    _insert_legacy(
        service, profile, request, "current-record", current_metadata=True
    )

    with pytest.raises(ConflictError, match="different request"):
        service.launch(request, "current-record", "session-a")


@pytest.mark.parametrize("kind", ["command", "experiment"])
def test_retry_of_a_row_claimed_with_the_and_joined_entrypoint_returns_it(
    tmp_path, profile, kind
):
    # Releases before "|| exit $?" joined the prelude and the command with "&&".
    inspector = NoAdmission()
    service = ComputeService(
        NoRemoteClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile, inspector=inspector
    )
    request = dict(_requests()[kind], name="named task")
    plan = service.plan(request)
    previous = copy.deepcopy(plan)
    config = previous["config"]
    holder, key = (config["entrypoint"], 2) if kind == "command" else (config, "entrypoint")
    holder[key] = holder[key].replace(" || exit $?\n", " && ", 1)
    record, created = service.store.claim(
        request_id="before-fix",
        owner="session-a",
        payload_hash=service._payload_hash(previous),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        name=plan["name"],
        description=plan["description"],
        workdir=request["workdir"],
        output_dir=request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    assert created
    service.store.mark_uncertain(record.task_id)

    result = service.launch(request, "before-fix", "session-a")

    assert result["task_id"] == record.task_id
    assert result["state"] == "submission_uncertain"
    assert inspector.calls == 0
    changed = copy.deepcopy(request)
    if kind == "command":
        changed["command"] = ["python", "changed.py"]
    else:
        changed["experiment_config"]["entrypoint"] = "python changed.py"
    with pytest.raises(ConflictError, match="different request"):
        service.launch(changed, "before-fix", "session-a")
