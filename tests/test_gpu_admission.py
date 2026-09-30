from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from determined_compute.compute import ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.compute import ValidationError
from determined_compute.compute import gpu_admission


class FakeClient:
    api_url = "https://det.example.test"


WORKDIR = "/shared/container/code"
OUTPUT = "/shared/container/out"
PREFIX = f"mkdir -p {OUTPUT} && cd {WORKDIR} && "


@pytest.fixture
def service():
    profile = ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": "/shared/host", "container_path": "/shared/container"}],
            "defaults": {"image": "registry/image:stable", "pool": "gpu", "slots": 1},
            "cluster_identity": "test-cluster",
        }
    )
    return ComputeService(FakeClient(), SQLiteTaskStore(":memory:"), profile)


def make_request(kind="command", **overrides):
    request = {
        "name": "job",
        "kind": kind,
        "command": "python evaluate.py",
        "workdir": WORKDIR,
        "output_dir": OUTPUT,
        "slots": 2 if kind == "command" else 1,
    }
    if kind == "experiment":
        request["experiment_config"] = {
            "searcher": {"name": "single", "metric": "loss", "max_length": {"batches": 1}},
            "environment": {"environment_variables": ["USER_SETTING=1"]},
        }
    request.update(overrides)
    return request


# --- planning ----------------------------------------------------------------------------

POLICY_VARIABLES = [
    "COMPUTE_GPU_ADMISSION=v1",
    "COMPUTE_GPU_ADMISSION_COUNT=2",
    "COMPUTE_GPU_ADMISSION_NAMES=NVIDIA *|Tesla *",
    "COMPUTE_GPU_ADMISSION_MIN_FREE_MIB=1024",
    "COMPUTE_GPU_ADMISSION_MIN_TOTAL_MIB=",
    "COMPUTE_GPU_ADMISSION_DRIVERS=",
    "COMPUTE_GPU_ADMISSION_RECEIPT=gpu-admission.json",
]


@pytest.mark.parametrize("kind", ["command", "experiment"])
def test_plan_renders_policy_variables_and_preflight(service, kind):
    request = make_request(
        kind, gpu_admission={"names": ["NVIDIA *", "Tesla *"], "min_free_mib": 1024}
    )
    if kind == "experiment":
        request["slots"] = 2
        request["experiment_config"]["resources"] = {"is_single_node": True}

    plan = service.plan(request)

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
    config = plan["config"]
    assert config["environment"]["environment_variables"] == [
        *(["USER_SETTING=1"] if kind == "experiment" else []),
        f"COMPUTE_WORKDIR={WORKDIR}",
        f"COMPUTE_OUTPUT_DIR={OUTPUT}",
        *POLICY_VARIABLES,
    ]
    step = gpu_admission.entrypoint_step()
    assert step.startswith("python3 -c ")
    script = f"{PREFIX}{step} || exit $?\npython evaluate.py"
    assert config["entrypoint"] == (["/bin/bash", "-lc", script] if kind == "command" else script)


@pytest.mark.parametrize("disabled", [None, False])
def test_disabled_admission_renders_byte_identical(service, disabled):
    baseline = service.plan(make_request())
    plan = service.plan(make_request(gpu_admission=disabled))

    assert plan == baseline
    assert plan["config"]["entrypoint"] == ["/bin/bash", "-lc", f"{PREFIX}python evaluate.py"]
    assert service._payload_hash(plan) == service._payload_hash(baseline)
    enabled = service.plan(make_request(gpu_admission={}))
    assert service._payload_hash(enabled) != service._payload_hash(baseline)


def _reserved_override():
    request = make_request("experiment", gpu_admission={})
    request["experiment_config"]["environment"]["environment_variables"] = [
        "COMPUTE_GPU_ADMISSION_COUNT=8"
    ]
    return request


SHELL = {
    "name": "s",
    "kind": "shell",
    "interactive": True,
    "workdir": WORKDIR,
    "output_dir": OUTPUT,
}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({**SHELL, "gpu_admission": {}}, "shell"),
        (make_request(slots=0, gpu_admission={}), "at least one slot"),
        (make_request(gpu_admission="yes"), "object"),
        (make_request(gpu_admission={"unexpected": 1}), "unknown"),
        (make_request(gpu_admission={"receipt": "../escape.json"}), "receipt"),
        (make_request(gpu_admission={"receipt": "nested/receipt.json"}), "receipt"),
        (make_request(gpu_admission={"names": ["a|b"]}), "names"),
        (make_request(gpu_admission={"names": "NVIDIA *"}), "names"),
        (make_request(gpu_admission={"count": 0}), "count"),
        (make_request(gpu_admission={"count": True}), "count"),
        (make_request(gpu_admission={"min_free_mib": -1}), "min_free_mib"),
        (_reserved_override(), "cannot be overridden"),
        (
            make_request(
                "experiment",
                gpu_admission={},
                experiment_config={"resources": {"is_single_node": False}},
            ),
            "single-node",
        ),
        # Determined's default may split a multi-slot trial across agents.
        (make_request("experiment", slots=2, gpu_admission={}), "is_single_node: true"),
    ],
)
def test_invalid_admission_requests_are_rejected(service, payload, message):
    with pytest.raises(ValidationError, match=message) as caught:
        service.plan(payload)
    assert caught.value.code == "invalid_request"


def _entrypoint_request(command):
    request = make_request("experiment", gpu_admission={})
    del request["command"]
    request["experiment_config"]["entrypoint"] = command
    return request


# With admission the command runs as its own line, where a blank one would pass as a no-op,
# so a blank command is rejected with or without admission.
@pytest.mark.parametrize(
    "payload",
    [
        make_request(command="   ", gpu_admission={}),
        make_request("experiment", command="\n\t\n"),
        _entrypoint_request("   "),
    ],
    ids=["command_admitted", "experiment_plain", "experiment_entrypoint_admitted"],
)
def test_blank_command_is_rejected(service, payload):
    with pytest.raises(ValidationError, match="command must not be empty"):
        service.plan(payload)


def test_script_is_python36_and_survives_the_yaml_submission(service):
    script = gpu_admission.SCRIPT_V1
    ast.parse(script, feature_version=(3, 6))
    assert "'" not in script
    plan = service.plan(make_request("experiment", gpu_admission={"names": ["A 'q' \"d\" \\"]}))

    # DeterminedAPIClient.launch_task submits experiment configs with this call.
    encoded = yaml.safe_dump(plan["config"], sort_keys=False)
    assert yaml.safe_load(encoded) == plan["config"]
    assert f"python3 -c '{script}' determined-compute-gpu-admission" in plan["config"]["entrypoint"]


# --- the preflight script against a fake pynvml ------------------------------------------

FAKE_PYNVML = """\
import time

CONFIG = %r


class NVMLError(Exception):
    pass


def _out(value):
    return value.encode() if CONFIG["bytes"] and isinstance(value, str) else value


def nvmlInit():
    if CONFIG["hang"]:
        time.sleep(60)
    if CONFIG["init_error"]:
        raise NVMLError(CONFIG["init_error"])


def nvmlShutdown():
    pass


def nvmlSystemGetDriverVersion():
    return _out("555.10")


def nvmlDeviceGetCount():
    return len(CONFIG["gpus"])


def nvmlDeviceGetHandleByIndex(index):
    return CONFIG["gpus"][index]


def nvmlDeviceGetName(handle):
    return _out(handle["name"])


def nvmlDeviceGetUUID(handle):
    return _out(handle["uuid"])


class _Memory:
    def __init__(self, total, free):
        # Bytes, with a partial MiB that the preflight must round down.
        self.total, self.free = (total << 20) + 4095, (free << 20) + 4095


def nvmlDeviceGetMemoryInfo(handle):
    return _Memory(handle["total"], handle["free"])
"""

GPUS = [
    {"uuid": "GPU-00000000", "name": "NVIDIA Example 24GB", "total": 24564, "free": 24000},
    {"uuid": "GPU-11111111", "name": "NVIDIA Example 24GB", "total": 24564, "free": 20000},
]


def _fake_pynvml(
    tmp_path, gpus=GPUS, *, as_bytes=False, init_error=None, hang=False, missing=False
):
    library = tmp_path / "pylib"
    library.mkdir(exist_ok=True)
    config = {"gpus": gpus, "bytes": as_bytes, "init_error": init_error, "hang": hang}
    source = 'raise ImportError("No module named pynvml")\n' if missing else FAKE_PYNVML % config
    (library / "pynvml.py").write_text(source, encoding="utf-8")
    return library


def _run(tmp_path, script=gpu_admission.SCRIPT_V1, **policy):
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    environment = {
        "PYTHONPATH": str(tmp_path / "pylib"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "COMPUTE_OUTPUT_DIR": str(output),
        "COMPUTE_GPU_ADMISSION": "v1",
        "DET_TASK_ID": "task-1",
        "DET_SECRET_VALUE": "must-not-appear",
    }
    environment.update({f"COMPUTE_GPU_ADMISSION_{k.upper()}": v for k, v in policy.items()})
    completed = subprocess.run(
        [sys.executable, "-c", script, "determined-compute-gpu-admission"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed, output


def _records(output, receipt="gpu-admission.json"):
    history = (output / receipt).with_suffix(".jsonl").read_text().splitlines()
    return json.loads((output / receipt).read_text()), [json.loads(line) for line in history]


@pytest.mark.parametrize(
    ("policy", "failure"),
    [
        (
            {
                "count": "2",
                "names": "Other*|NVIDIA Example*",
                "drivers": "555.*",
                "min_free_mib": "20000",
                "min_total_mib": "24564",
            },
            None,
        ),
        ({"count": "1"}, "NVML reports 2 GPUs but the policy requires 1"),
        ({"names": "nvidia example*|Other*"}, "GPU 0 name NVIDIA Example 24GB is not allowed"),
        ({"drivers": "560.*"}, "GPU 0 driver 555.10 is not allowed"),
        ({"min_free_mib": "22000"}, "GPU 1 has 20000 MiB free, below 22000 MiB"),
        ({"min_total_mib": "30000"}, "GPU 0 has 24564 MiB total, below 30000 MiB"),
    ],
    ids=["pass", "count", "name", "driver", "free", "total"],
)
def test_script_evaluates_policy_and_writes_receipt(tmp_path, policy, failure):
    _fake_pynvml(tmp_path)

    completed, output = _run(tmp_path, **policy)

    status = "failed" if failure else "passed"
    receipt, history = _records(output)
    assert completed.returncode == (86 if failure else 0)
    assert completed.stdout.startswith(f"determined-compute gpu_admission: {status}: ")
    assert history == [receipt]
    assert receipt["schema_version"] == gpu_admission.GPU_ADMISSION_RECEIPT_SCHEMA
    assert receipt["status"] == status
    assert (failure in receipt["failures"]) if failure else receipt["failures"] == []
    assert receipt["devices"][1] == {
        "index": 1,
        "uuid": "GPU-11111111",
        "name": "NVIDIA Example 24GB",
        "driver_version": "555.10",
        "memory_total_mib": 24564,
        "memory_free_mib": 20000,
    }
    assert receipt["determined"] == {"task_id": "task-1", "allocation_id": None, "trial_id": None}
    assert receipt["observed_at"].endswith("Z")
    assert "must-not-appear" not in (output / "gpu-admission.json").read_text()
    assert sorted(path.name for path in output.iterdir()) == [
        "gpu-admission.json",
        "gpu-admission.jsonl",
    ]


def test_script_decodes_bytes_from_older_pynvml(tmp_path):
    gpus = [{"uuid": "GPU-2", "name": b"NVIDIA \xffExample", "total": 1024, "free": 512}]
    _fake_pynvml(tmp_path, gpus, as_bytes=True)

    completed, output = _run(tmp_path, count="1", names="NVIDIA ?Example", drivers="555.10")

    assert completed.returncode == 0, completed.stdout
    device = _records(output)[0]["devices"][0]
    assert (device["uuid"], device["name"], device["driver_version"]) == (
        "GPU-2",
        "NVIDIA �Example",
        "555.10",
    )


def test_script_fails_closed_without_nvml_and_keeps_history(tmp_path):
    _fake_pynvml(tmp_path, missing=True)
    first, output = _run(tmp_path, count="1", receipt="probe.json")
    _fake_pynvml(tmp_path, init_error="Driver Not Loaded")
    second, _output = _run(tmp_path, count="1", receipt="probe.json")

    assert (first.returncode, second.returncode) == (86, 86)
    receipt, history = _records(output, "probe.json")
    assert [(item["devices"], item["failures"]) for item in history] == [
        ([], ["nvidia-ml-py (pynvml) is not installed in the task image"]),
        ([], ["NVML initialisation failed: NVMLError: Driver Not Loaded"]),
    ]
    assert receipt == history[-1]
    assert sorted(path.name for path in output.iterdir()) == ["probe.json", "probe.jsonl"]


def test_script_fails_closed_when_nvml_hangs(tmp_path):
    _fake_pynvml(tmp_path, hang=True)
    script = gpu_admission.SCRIPT_V1.replace("t.join(60)", "t.join(1)")
    assert script != gpu_admission.SCRIPT_V1

    completed, output = _run(tmp_path, script=script, count="2")

    assert completed.returncode == 86
    assert _records(output)[0]["failures"] == ["NVML did not answer within 60 seconds"]


# --- the rendered entrypoint: nothing of the workload runs before admission passes --------

BASH = shutil.which("bash")
DASH = shutil.which("dash")
# Determined starts an experiment's string entrypoint with `sh -c`; use dash when present.
POSIX_SH = [DASH] if DASH else [BASH or "bash", "--posix"]
_OUT = '"$COMPUTE_OUTPUT_DIR"'
# Each workload: the command, its exit code once admitted, and the files it leaves.
WORKLOADS = {
    "semicolon": (f"echo x > {_OUT}/a; echo x > {_OUT}/b; exit 7", 7, {"a", "b"}),
    "newline": (f"echo x > {_OUT}/a\necho x > {_OUT}/b\nexit 7", 7, {"a", "b"}),
    "or": (f"false || {{ echo x > {_OUT}/b; exit 7; }}", 7, {"b"}),
    "heredoc": (f"cat > {_OUT}/a <<EOF\nx=1\nEOF\necho x > {_OUT}/b", 0, {"a", "b"}),
    "set_e": (f"set -e; echo x > {_OUT}/a; false; echo x > {_OUT}/b", 1, {"a"}),
    "background": (f"echo x > {_OUT}/a & echo x > {_OUT}/b; wait; exit 7", 7, {"a", "b"}),
}


@pytest.mark.skipif(BASH is None, reason="bash is required")
@pytest.mark.parametrize("workload", sorted(WORKLOADS))
def test_workload_runs_only_after_admission(tmp_path, workload):
    command, exit_code, markers = WORKLOADS[workload]
    shared = tmp_path / "shared"
    (shared / "code").mkdir(parents=True)
    profile = ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": str(shared), "container_path": str(shared)}],
            "defaults": {"image": "image", "pool": "pool", "slots": 1},
        }
    )
    service = ComputeService(FakeClient(), SQLiteTaskStore(":memory:"), profile)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, found in [("mkdir", shutil.which("mkdir")), ("cat", shutil.which("cat"))]:
        (tools / name).symlink_to(found)
    (tools / "python3").symlink_to(sys.executable)
    library = _fake_pynvml(tmp_path)

    for kind in ("command", "experiment"):
        for admitted in (True, False):
            output = shared / f"{kind}-{admitted}"
            request = {
                "name": "admitted",
                "kind": kind,
                "command": command,
                "workdir": str(shared / "code"),
                "output_dir": str(output),
                "slots": 2,
                "gpu_admission": {"min_free_mib": 1000 if admitted else 99999},
            }
            if kind == "experiment":
                request["experiment_config"] = {"resources": {"is_single_node": True}}
            config = service.plan(request)["config"]
            environment = {"PATH": str(tools), "HOME": str(tmp_path), "PYTHONPATH": str(library)}
            environment.update(
                item.split("=", 1) for item in config["environment"]["environment_variables"]
            )
            if kind == "command":
                assert config["entrypoint"][:2] == ["/bin/bash", "-lc"]
                # Skip login profiles so the host's PATH setup cannot shadow the tools.
                argv = [BASH, "--noprofile", "-lc", config["entrypoint"][2]]
            else:
                argv = [*POSIX_SH, "-c", config["entrypoint"]]

            completed = subprocess.run(
                argv, env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=30
            )

            receipt = json.loads((output / "gpu-admission.json").read_text())
            left = {path.name for path in output.iterdir()} - {
                "gpu-admission.json",
                "gpu-admission.jsonl",
            }
            observed = (completed.returncode, left, receipt["status"])
            expected = (exit_code, markers, "passed") if admitted else (86, set(), "failed")
            assert observed == expected, (kind, admitted, completed.stdout, completed.stderr)
            # The fake pynvml answered, not a real one.
            assert [device["uuid"] for device in receipt["devices"]] == [
                "GPU-00000000",
                "GPU-11111111",
            ]
    assert not any((shared / "code").iterdir())
