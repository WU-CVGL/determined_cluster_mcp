from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.compute import ValidationError
from determined_compute.compute import gpu_admission


class FakeClient:
    api_url = "https://det.example.test"


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "registry/image:stable", "pool": "gpu", "slots": 1},
            "cluster_identity": "test-cluster",
        }
    )


@pytest.fixture
def service(profile):
    return ComputeService(FakeClient(), SQLiteTaskStore(":memory:"), profile)


def command_request(**overrides):
    request = {
        "name": "evaluate",
        "command": ["python", "evaluate.py"],
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "slots": 2,
    }
    request.update(overrides)
    return request


def experiment_request(**overrides):
    request = {
        "name": "train",
        "kind": "experiment",
        "command": "python train.py",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "searcher": {"name": "single", "metric": "loss", "max_length": {"batches": 1}},
            "environment": {"environment_variables": ["USER_SETTING=1"]},
        },
    }
    request.update(overrides)
    return request


POLICY_VARIABLES = [
    "COMPUTE_GPU_ADMISSION=v1",
    "COMPUTE_GPU_ADMISSION_COUNT=2",
    "COMPUTE_GPU_ADMISSION_NAMES=NVIDIA *|Tesla *",
    "COMPUTE_GPU_ADMISSION_MIN_FREE_MIB=1024",
    "COMPUTE_GPU_ADMISSION_MIN_TOTAL_MIB=",
    "COMPUTE_GPU_ADMISSION_DRIVERS=",
    "COMPUTE_GPU_ADMISSION_RECEIPT=gpu-admission.json",
]


def test_command_plan_renders_policy_variables_and_preflight(service):
    plan = service.plan(
        command_request(gpu_admission={"names": ["NVIDIA *", "Tesla *"], "min_free_mib": 1024})
    )

    assert plan["gpu_admission"] == {
        "version": "v1",
        "count": 2,
        "names": ["NVIDIA *", "Tesla *"],
        "min_free_mib": 1024,
        "min_total_mib": None,
        "driver_versions": [],
        "receipt": "gpu-admission.json",
        "failure_exit_code": 86,
    }
    assert plan["config"]["environment"]["environment_variables"] == [
        "COMPUTE_WORKDIR=/shared/container/code",
        "COMPUTE_OUTPUT_DIR=/shared/container/out",
        *POLICY_VARIABLES,
    ]
    step = gpu_admission.entrypoint_step()
    assert step.startswith("/bin/bash -c '") and step.endswith("' determined-compute-gpu-admission")
    assert plan["config"]["entrypoint"] == [
        "/bin/bash",
        "-lc",
        "mkdir -p /shared/container/out && cd /shared/container/code && "
        f"{step} && python evaluate.py",
    ]
    assert plan["advisories"] == []


def test_experiment_plan_appends_policy_after_managed_variables(service):
    plan = service.plan(
        experiment_request(
            slots=2,
            gpu_admission={"names": ["NVIDIA *", "Tesla *"], "min_free_mib": 1024},
        )
    )

    assert plan["config"]["environment"]["environment_variables"] == [
        "USER_SETTING=1",
        "COMPUTE_WORKDIR=/shared/container/code",
        "COMPUTE_OUTPUT_DIR=/shared/container/out",
        *POLICY_VARIABLES,
    ]
    assert plan["config"]["entrypoint"] == (
        "mkdir -p /shared/container/out && cd /shared/container/code && "
        f"{gpu_admission.entrypoint_step()} && python train.py"
    )


@pytest.mark.parametrize("disabled", [None, False])
def test_disabled_admission_leaves_plan_and_hash_unchanged(service, disabled):
    baseline = service.plan(command_request())
    plan = service.plan(command_request(gpu_admission=disabled))

    assert "gpu_admission" not in plan
    assert plan == baseline
    assert service._payload_hash(plan) == service._payload_hash(baseline)
    enabled = service.plan(command_request(gpu_admission={}))
    assert service._payload_hash(enabled) != service._payload_hash(baseline)


@pytest.mark.parametrize(
    ("request_factory", "policy", "message"),
    [
        (lambda: command_request(kind="shell", command=None, interactive=True), {}, "shell"),
        (lambda: command_request(slots=0), {}, "at least one slot"),
        (lambda: command_request(), {"receipt": "../escape.json"}, "receipt"),
        (lambda: command_request(), {"receipt": "nested/receipt.json"}, "receipt"),
        (lambda: command_request(), {"receipt": "receipt.txt"}, "receipt"),
        (lambda: command_request(), {"receipt": ""}, "receipt"),
        (lambda: command_request(), {"names": ["a|b"]}, "names"),
        (lambda: command_request(), {"names": ["line\nbreak"]}, "names"),
        (lambda: command_request(), {"names": ["x"] * 9}, "names"),
        (lambda: command_request(), {"names": "NVIDIA *"}, "names"),
        (lambda: command_request(), {"driver_versions": [""]}, "driver_versions"),
        (lambda: command_request(), {"count": 0}, "count"),
        (lambda: command_request(), {"count": True}, "count"),
        (lambda: command_request(), {"min_free_mib": -1}, "min_free_mib"),
        (lambda: command_request(), {"min_total_mib": 1.5}, "min_total_mib"),
        (lambda: command_request(), {"unexpected": 1}, "unknown"),
        (
            lambda: experiment_request(
                experiment_config={"resources": {"is_single_node": False}}
            ),
            {},
            "single-node",
        ),
    ],
)
def test_invalid_admission_requests_are_rejected(service, request_factory, policy, message):
    request = {key: value for key, value in request_factory().items() if value is not None}
    request["gpu_admission"] = policy

    with pytest.raises(ValidationError, match=message) as caught:
        service.plan(request)
    assert caught.value.code == "invalid_request"


def test_non_object_admission_and_reserved_variables_are_rejected(service):
    with pytest.raises(ValidationError, match="object"):
        service.plan(command_request(gpu_admission="yes"))
    request = experiment_request()
    request["experiment_config"]["environment"]["environment_variables"] = [
        "COMPUTE_GPU_ADMISSION_COUNT=8"
    ]
    with pytest.raises(ValidationError, match="cannot be overridden"):
        service.plan(request)


BASH = shutil.which("bash")
pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash is required")


def _toolbox(tmp_path: Path) -> Path:
    """A PATH directory with coreutils but no nvidia-smi."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("tr", "date", "mv", "rm", "hostname", "cat", "timeout", "mkdir"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    return tools


def _fake_smi(tools: Path, body: str) -> None:
    script = tools / "nvidia-smi"
    script.write_text("#!" + BASH + "\n" + body, encoding="utf-8")
    script.chmod(0o755)


GPUS = (
    "echo '0, GPU-00000000-0000-0000-0000-000000000000, NVIDIA Example 24GB, "
    "555.10, 24564, 24000'\n"
    "echo '1, GPU-11111111-1111-1111-1111-111111111111, NVIDIA Example 24GB, "
    "555.10, 24564, 20000'\n"
)


def _run_script(tmp_path, tools, **policy):
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    environment = {
        "PATH": str(tools),
        "COMPUTE_OUTPUT_DIR": str(output),
        "COMPUTE_GPU_ADMISSION": "v1",
        "DET_TASK_ID": "task-1",
        "DET_SECRET_VALUE": "must-not-appear",
        "COMPUTE_SUBMISSION_MARKER": "determined-compute:must-not-appear",
    }
    environment.update(
        {f"COMPUTE_GPU_ADMISSION_{key.upper()}": value for key, value in policy.items()}
    )
    completed = subprocess.run(
        [BASH, "-c", gpu_admission.SCRIPT_V1, "determined-compute-gpu-admission"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed, output


@pytestmark_bash
@pytest.mark.parametrize(
    ("policy", "status", "failure"),
    [
        ({"count": "2", "names": "NVIDIA Example*", "min_free_mib": "16000"}, "passed", None),
        ({"count": "1"}, "failed", "2 GPUs are visible but the policy requires 1"),
        (
            {"count": "2", "names": "Other*|Another*"},
            "failed",
            "name NVIDIA Example 24GB is not allowed",
        ),
        (
            {"count": "2", "min_free_mib": "22000"},
            "failed",
            "GPU 1 has 20000 MiB free, below 22000 MiB",
        ),
        ({"count": "2", "min_total_mib": "30000"}, "failed", "below 30000 MiB"),
        ({"count": "2", "drivers": "560.*"}, "failed", "driver 555.10 is not allowed"),
    ],
)
def test_script_evaluates_policy_and_writes_receipt(tmp_path, policy, status, failure):
    tools = _toolbox(tmp_path)
    _fake_smi(tools, GPUS)

    completed, output = _run_script(tmp_path, tools, **policy)

    assert completed.returncode == (0 if status == "passed" else 86)
    assert completed.stdout.startswith(f"determined-compute gpu_admission: {status}")
    assert len(completed.stdout.splitlines()) == 1
    receipt = json.loads((output / "gpu-admission.json").read_text())
    assert receipt["schema_version"] == gpu_admission.GPU_ADMISSION_RECEIPT_SCHEMA
    assert receipt["status"] == status
    assert receipt["observed_at"].endswith("Z")
    assert [device["memory_free_mib"] for device in receipt["devices"]] == [24000, 20000]
    assert receipt["devices"][0]["uuid"] == "GPU-00000000-0000-0000-0000-000000000000"
    assert receipt["determined"] == {"task_id": "task-1", "allocation_id": None, "trial_id": None}
    if failure is None:
        assert receipt["failures"] == []
    else:
        assert any(failure in item for item in receipt["failures"])
    text = (output / "gpu-admission.json").read_text()
    assert "must-not-appear" not in text
    history = (output / "gpu-admission.jsonl").read_text().splitlines()
    assert [json.loads(line)["status"] for line in history] == [status]


@pytestmark_bash
def test_script_appends_history_and_fails_without_nvidia_smi(tmp_path):
    tools = _toolbox(tmp_path)

    first, output = _run_script(tmp_path, tools, count="1", receipt="probe.json")
    _fake_smi(tools, "exit 9\n")
    second, _output = _run_script(tmp_path, tools, count="1", receipt="probe.json")

    assert first.returncode == 86 and second.returncode == 86
    receipt = json.loads((output / "probe.json").read_text())
    assert receipt["failures"] == ["nvidia-smi failed with exit code 9"]
    history = [json.loads(line) for line in (output / "probe.jsonl").read_text().splitlines()]
    assert [item["failures"] for item in history] == [
        ["nvidia-smi is unavailable"],
        ["nvidia-smi failed with exit code 9"],
    ]
    assert not list(output.glob(".*.tmp"))


@pytestmark_bash
@pytest.mark.parametrize(("min_free", "exit_code", "ran"), [(1000, 0, True), (99999, 86, False)])
def test_rendered_entrypoint_runs_workload_only_after_admission(
    tmp_path, min_free, exit_code, ran
):
    host = tmp_path / "shared"
    (host / "code").mkdir(parents=True)
    profile = ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": str(host), "container_path": str(host)}],
            "defaults": {"image": "image", "pool": "pool", "slots": 1},
        }
    )
    service = ComputeService(FakeClient(), SQLiteTaskStore(":memory:"), profile)
    plan = service.plan(
        {
            "name": "admitted",
            "command": 'echo ran > "$COMPUTE_OUTPUT_DIR/workload.txt"',
            "workdir": str(host / "code"),
            "output_dir": str(host / "out"),
            "slots": 2,
            "gpu_admission": {"min_free_mib": min_free},
        }
    )
    tools = _toolbox(tmp_path)
    _fake_smi(tools, GPUS)
    environment = {"PATH": str(tools)}
    environment.update(
        item.split("=", 1) for item in plan["config"]["environment"]["environment_variables"]
    )
    shell, _login, script = plan["config"]["entrypoint"]
    assert shell == "/bin/bash"

    # Run without a login profile so the host's PATH setup cannot shadow the fake tools.
    completed = subprocess.run(
        [BASH, "-c", script], env=environment, capture_output=True, text=True, timeout=30
    )

    assert completed.returncode == exit_code, completed.stderr
    assert (host / "out" / "workload.txt").exists() is ran
    assert json.loads((host / "out" / "gpu-admission.json").read_text())["policy"]["count"] == 2


def test_experiment_entrypoint_survives_the_yaml_submission_encoding(service):
    import yaml

    plan = service.plan(experiment_request(slots=1, gpu_admission={"names": ["A 'q' \"d\" \\"]}))

    # DeterminedAPIClient.launch_task submits experiment configs with this call.
    encoded = yaml.safe_dump(plan["config"], sort_keys=False)
    assert yaml.safe_load(encoded) == plan["config"]
    assert gpu_admission.SCRIPT_V1 in plan["config"]["entrypoint"].replace("'\"'\"'", "'")
