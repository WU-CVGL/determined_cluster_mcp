"""Optional in-container GPU admission preflight for command and experiment tasks.

The preflight is a versioned Python script that runs before the workload inside the
task container. It needs ``python3`` (3.6 or newer) and the ``nvidia-ml-py`` package,
whose ``pynvml`` module binds NVIDIA's NVML library. The script is part of the
rendered, hashed task config, so any change to it requires a new version.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Mapping, Optional

from .models import ValidationError

GPU_ADMISSION_VERSION = "v1"
GPU_ADMISSION_EXIT_CODE = 86
GPU_ADMISSION_RECEIPT_SCHEMA = "determined-compute-gpu-admission-v1"
DEFAULT_RECEIPT = "gpu-admission.json"
ENVIRONMENT_NAMES = (
    "COMPUTE_GPU_ADMISSION",
    "COMPUTE_GPU_ADMISSION_COUNT",
    "COMPUTE_GPU_ADMISSION_NAMES",
    "COMPUTE_GPU_ADMISSION_MIN_FREE_MIB",
    "COMPUTE_GPU_ADMISSION_MIN_TOTAL_MIB",
    "COMPUTE_GPU_ADMISSION_DRIVERS",
    "COMPUTE_GPU_ADMISSION_RECEIPT",
)
_POLICY_FIELDS = {"count", "names", "min_free_mib", "min_total_mib", "driver_versions", "receipt"}
_MAX_PATTERNS = 8
_MAX_PATTERN_LENGTH = 128
_RECEIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,122}\.json")

# Version v1. Reads the policy from COMPUTE_GPU_ADMISSION_* variables, queries NVML
# through pynvml with a 60 second limit, writes an atomically replaced JSON receipt plus
# one JSON line of history under COMPUTE_OUTPUT_DIR, prints one summary line, and exits
# 86 when the policy fails or NVML is unusable. It drops the working directory from
# sys.path so workload files cannot shadow the modules it imports. It is passed as one
# shell-quoted argument, so it stays small, contains no single quote, and runs on
# Python 3.6; pynvml may return bytes or str.
SCRIPT_V1 = r"""import fnmatch, json, os, socket, sys, threading, time
sys.path[:] = [p for p in sys.path if p]
E = os.environ
F, D, Q = [], [], []
def env(k):
    return E.get("COMPUTE_GPU_ADMISSION_" + k, "")
def num(k):
    v = env(k)
    if v and not v.strip("0123456789"):
        return int(v)
    if v:
        F.append("COMPUTE_GPU_ADMISSION_%s is not a non-negative integer" % k)
def text(v):
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
def allowed(v, patterns):
    return not patterns or any(fnmatch.fnmatchcase(v, p) for p in patterns)
def opt(k):
    return E.get(k) or None
def say(f, m):
    m = "determined-compute gpu_admission: %s\n" % m
    f.buffer.write(m.encode("utf-8", "surrogateescape"))
    f.flush()
def query():
    step = "import"
    try:
        import pynvml as N
        step = "initialisation"
        N.nvmlInit()
        step = "query"
        driver = text(N.nvmlSystemGetDriverVersion())
        for i in range(N.nvmlDeviceGetCount()):
            h = N.nvmlDeviceGetHandleByIndex(i)
            m = N.nvmlDeviceGetMemoryInfo(h)
            D.append({"index": i, "uuid": text(N.nvmlDeviceGetUUID(h)),
                      "name": text(N.nvmlDeviceGetName(h)), "driver_version": driver,
                      "memory_total_mib": m.total >> 20, "memory_free_mib": m.free >> 20})
        step = None
        N.nvmlShutdown()
    except Exception as e:  # pynvml.NVMLError or anything unexpected fails closed
        if step == "import" and isinstance(e, ImportError):
            Q.append("nvidia-ml-py (pynvml) is not installed in the task image")
        elif step:
            Q.append("NVML %s failed: %s: %s" % (step, type(e).__name__, e))
count, free, total = num("COUNT"), num("MIN_FREE_MIB"), num("MIN_TOTAL_MIB")
names, drivers = [[p for p in env(k).split("|") if p] for k in ("NAMES", "DRIVERS")]
t = threading.Thread(target=query)
t.daemon = True
t.start()
t.join(60)
hung = t.is_alive()
devices = [] if hung else list(D)
F.extend(["NVML did not answer within 60 seconds"] if hung else Q)
if not (hung or Q) and count is not None and len(devices) != count:
    F.append("NVML reports %d GPUs but the policy requires %d" % (len(devices), count))
for d in devices:
    g = "GPU %d" % d["index"]
    if not allowed(d["name"], names):
        F.append("%s name %s is not allowed" % (g, d["name"]))
    if not allowed(d["driver_version"], drivers):
        F.append("%s driver %s is not allowed" % (g, d["driver_version"]))
    if free is not None and d["memory_free_mib"] < free:
        F.append("%s has %d MiB free, below %d MiB" % (g, d["memory_free_mib"], free))
    if total is not None and d["memory_total_mib"] < total:
        F.append("%s has %d MiB total, below %d MiB" % (g, d["memory_total_mib"], total))
record = json.dumps({
    "schema_version": "determined-compute-gpu-admission-v1",
    "status": "failed" if F else "passed",
    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "policy": {"count": count, "names": names, "min_free_mib": free,
               "min_total_mib": total, "driver_versions": drivers},
    "devices": devices, "failures": F,
    "cuda_visible_devices": opt("CUDA_VISIBLE_DEVICES"),
    "nvidia_visible_devices": opt("NVIDIA_VISIBLE_DEVICES"),
    "hostname": socket.gethostname() or None,
    "determined": {"task_id": opt("DET_TASK_ID"), "allocation_id": opt("DET_ALLOCATION_ID"),
                   "trial_id": opt("DET_TRIAL_ID")},
}, separators=(",", ":")) + "\n"
out, name = E.get("COMPUTE_OUTPUT_DIR") or ".", env("RECEIPT") or "gpu-admission.json"
path = os.path.join(out, name)
tmp = os.path.join(out, ".%s.%d.tmp" % (name, os.getpid()))
try:
    with open(tmp, "w") as f:
        f.write(record)
    os.replace(tmp, path)
except OSError:
    try:
        os.remove(tmp)
    except OSError:
        pass
    say(sys.stderr, "cannot write " + path)
history = (path[:-5] if path.endswith(".json") else path) + ".jsonl"
try:
    with open(history, "a") as f:
        f.write(record)
except OSError:
    say(sys.stderr, "cannot append " + history)
if F:
    say(sys.stdout, "failed: %s; receipt %s" % ("; ".join(F), path))
else:
    say(sys.stdout, "passed: %d GPUs; receipt %s" % (len(devices), path))
os._exit(86 if F else 0)  # a hung NVML thread must not delay the exit
"""


def _patterns(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_PATTERNS:
        raise ValidationError(f"{field} must be a list of at most {_MAX_PATTERNS} patterns")
    result = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > _MAX_PATTERN_LENGTH
            or not item.isprintable()
            or "|" in item
        ):
            raise ValidationError(
                f"{field} entries must be printable patterns of 1 to {_MAX_PATTERN_LENGTH} "
                "characters without '|'"
            )
        result.append(item)
    return result


def _non_negative(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer or null")
    return value


def normalize_policy(
    value: Any,
    *,
    kind: str,
    slots: int,
    experiment_config: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Validate a request's ``gpu_admission`` value; ``None`` means disabled."""

    if value is None or value is False:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("gpu_admission must be an object, false, or null")
    unknown = set(value) - _POLICY_FIELDS
    if unknown:
        raise ValidationError(f"gpu_admission has unknown fields: {sorted(unknown)}")
    if kind not in {"command", "experiment"}:
        raise ValidationError(
            "gpu_admission applies only to command and experiment tasks; "
            "a shell has no managed entrypoint"
        )
    if slots < 1:
        raise ValidationError("gpu_admission requires at least one slot")
    if kind == "experiment":
        resources = (experiment_config or {}).get("resources")
        single_node = resources.get("is_single_node") if isinstance(resources, Mapping) else None
        if single_node is False:
            raise ValidationError("gpu_admission supports single-node experiments only")
        # Determined's default (null) may split a multi-slot trial across agents, and each
        # container would then see only its own agent's GPUs.
        if slots > 1 and single_node is not True:
            raise ValidationError(
                "gpu_admission for an experiment with more than one slot requires "
                "experiment_config.resources.is_single_node: true"
            )
    count = value.get("count", slots)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValidationError("gpu_admission.count must be a positive integer")
    receipt = value.get("receipt", DEFAULT_RECEIPT)
    if not isinstance(receipt, str) or not _RECEIPT.fullmatch(receipt) or ".." in receipt:
        raise ValidationError(
            "gpu_admission.receipt must be a plain file name ending in .json, "
            "without '/' or '..'"
        )
    return {
        "version": GPU_ADMISSION_VERSION,
        "count": count,
        "names": _patterns(value.get("names"), "gpu_admission.names"),
        "min_free_mib": _non_negative(value.get("min_free_mib"), "gpu_admission.min_free_mib"),
        "min_total_mib": _non_negative(value.get("min_total_mib"), "gpu_admission.min_total_mib"),
        "driver_versions": _patterns(
            value.get("driver_versions"), "gpu_admission.driver_versions"
        ),
        "receipt": receipt,
        "failure_exit_code": GPU_ADMISSION_EXIT_CODE,
    }


def environment_variables(policy: Mapping[str, Any]) -> List[str]:
    """Render the managed policy variables read by the preflight script."""

    def optional(value: Optional[int]) -> str:
        return "" if value is None else str(value)

    return [
        f"COMPUTE_GPU_ADMISSION={GPU_ADMISSION_VERSION}",
        f"COMPUTE_GPU_ADMISSION_COUNT={policy['count']}",
        f"COMPUTE_GPU_ADMISSION_NAMES={'|'.join(policy['names'])}",
        f"COMPUTE_GPU_ADMISSION_MIN_FREE_MIB={optional(policy['min_free_mib'])}",
        f"COMPUTE_GPU_ADMISSION_MIN_TOTAL_MIB={optional(policy['min_total_mib'])}",
        f"COMPUTE_GPU_ADMISSION_DRIVERS={'|'.join(policy['driver_versions'])}",
        f"COMPUTE_GPU_ADMISSION_RECEIPT={policy['receipt']}",
    ]


def entrypoint_step() -> str:
    """Return the shell step inserted before the workload command."""
    return f"python3 -c {shlex.quote(SCRIPT_V1)} determined-compute-gpu-admission"


__all__ = [
    "DEFAULT_RECEIPT",
    "ENVIRONMENT_NAMES",
    "GPU_ADMISSION_EXIT_CODE",
    "GPU_ADMISSION_RECEIPT_SCHEMA",
    "GPU_ADMISSION_VERSION",
    "SCRIPT_V1",
    "entrypoint_step",
    "environment_variables",
    "normalize_policy",
]
