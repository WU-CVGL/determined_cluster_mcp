"""Optional in-container GPU admission preflight for command and experiment tasks.

The preflight is a versioned bash script that runs before the workload inside the
task container. It needs only bash, coreutils, and ``nvidia-smi``. The script is part
of the rendered, hashed task config, so any change to it requires a new version.
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

# Version v1. Reads the policy from COMPUTE_GPU_ADMISSION_* variables, writes an
# atomically replaced JSON receipt plus one JSON line of history under
# COMPUTE_OUTPUT_DIR, prints one summary line, and exits 86 when the policy fails.
SCRIPT_V1 = r'''out=${COMPUTE_OUTPUT_DIR:-.}
receipt=${COMPUTE_GPU_ADMISSION_RECEIPT:-gpu-admission.json}
count=${COMPUTE_GPU_ADMISSION_COUNT:-}
names=${COMPUTE_GPU_ADMISSION_NAMES:-}
drivers=${COMPUTE_GPU_ADMISSION_DRIVERS:-}
min_free=${COMPUTE_GPU_ADMISSION_MIN_FREE_MIB:-}
min_total=${COMPUTE_GPU_ADMISSION_MIN_TOTAL_MIB:-}
js() { local s; s=$(printf '%s' "$1" | tr -d '\000-\037\177'); s=${s//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }
jn() { case $1 in ''|*[!0-9]*) printf null ;; *) printf '%s' "$((10#$1))" ;; esac; }
jo() { if [ -n "$1" ]; then js "$1"; else printf null; fi; }
jl() { local IFS='|' first=1 item items; printf '['; if [ -n "$1" ]; then read -r -a items <<<"$1"; for item in "${items[@]}"; do [ "$first" = 1 ] || printf ','; first=0; js "$item"; done; fi; printf ']'; }
trim() { local v=$1; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}; printf '%s' "$v"; }
matches() { local IFS='|' pat pats; read -r -a pats <<<"$2"; for pat in "${pats[@]}"; do case $1 in $pat) return 0 ;; esac; done; return 1; }
atleast() { case $1 in ''|*[!0-9]*) return 1 ;; esac; [ "$((10#$1))" -ge "$2" ]; }
failures=()
devices=
unparsed=
n=0
query=(nvidia-smi --query-gpu=index,uuid,name,driver_version,memory.total,memory.free --format=csv,noheader,nounits)
if command -v nvidia-smi >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then raw=$(timeout 60 "${query[@]}" 2>/dev/null); rc=$?; else raw=$("${query[@]}" 2>/dev/null); rc=$?; fi
  if [ "$rc" != 0 ]; then failures+=("nvidia-smi failed with exit code $rc"); raw=; fi
else
  rc=127; raw=; failures+=("nvidia-smi is unavailable")
fi
if [ "$rc" = 0 ]; then
  while IFS= read -r line; do
    [ -n "$(trim "$line")" ] || continue
    IFS=, read -r index uuid name driver total free extra <<<"$line"
    index=$(trim "$index"); uuid=$(trim "$uuid"); name=$(trim "$name")
    driver=$(trim "$driver"); total=$(trim "$total"); free=$(trim "$free")
    case $index in ''|*[!0-9]*) row= ;; *) row=1 ;; esac
    if [ -z "$row" ] || [ -n "$extra" ] || [ -z "$free" ]; then unparsed="$unparsed${unparsed:+,}$(js "$(printf '%s' "$line" | LC_ALL=C tr -cd '\040-\176')")"; continue; fi
    n=$((n + 1))
    devices="$devices${devices:+,}{\"index\":$(jn "$index"),\"uuid\":$(jo "$uuid"),\"name\":$(jo "$name"),\"driver_version\":$(jo "$driver"),\"memory_total_mib\":$(jn "$total"),\"memory_free_mib\":$(jn "$free")}"
    if [ -n "$names" ] && ! matches "$name" "$names"; then failures+=("GPU $index name $name is not allowed"); fi
    if [ -n "$drivers" ] && ! matches "$driver" "$drivers"; then failures+=("GPU $index driver $driver is not allowed"); fi
    if [ -n "$min_free" ] && ! atleast "$free" "$min_free"; then failures+=("GPU $index has ${free:-unknown} MiB free, below $min_free MiB"); fi
    if [ -n "$min_total" ] && ! atleast "$total" "$min_total"; then failures+=("GPU $index has ${total:-unknown} MiB total, below $min_total MiB"); fi
  done <<<"$raw"
  if [ -n "$count" ] && [ "$n" != "$count" ]; then failures+=("nvidia-smi reports $n GPUs but the policy requires $count"); fi
fi
status=passed
[ "${#failures[@]}" = 0 ] || status=failed
fl=
for f in "${failures[@]}"; do fl="$fl${fl:+,}$(js "$f")"; done
host=$(hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null)
record="{\"schema_version\":\"determined-compute-gpu-admission-v1\",\"status\":\"$status\",\"observed_at\":$(jo "$(date -u +%FT%TZ 2>/dev/null)"),\"policy\":{\"count\":$(jn "$count"),\"names\":$(jl "$names"),\"min_free_mib\":$(jn "$min_free"),\"min_total_mib\":$(jn "$min_total"),\"driver_versions\":$(jl "$drivers")},\"devices\":[$devices],\"unparsed_lines\":[$unparsed],\"failures\":[$fl],\"cuda_visible_devices\":$(jo "${CUDA_VISIBLE_DEVICES:-}"),\"nvidia_visible_devices\":$(jo "${NVIDIA_VISIBLE_DEVICES:-}"),\"hostname\":$(jo "$host"),\"determined\":{\"task_id\":$(jo "${DET_TASK_ID:-}"),\"allocation_id\":$(jo "${DET_ALLOCATION_ID:-}"),\"trial_id\":$(jo "${DET_TRIAL_ID:-}")}}"
tmp="$out/.$receipt.$$.tmp"
if printf '%s\n' "$record" >"$tmp" 2>/dev/null && mv -f "$tmp" "$out/$receipt" 2>/dev/null; then :; else rm -f "$tmp" 2>/dev/null; echo "determined-compute gpu_admission: cannot write $out/$receipt" >&2; fi
printf '%s\n' "$record" >>"$out/${receipt%.json}.jsonl" 2>/dev/null || echo "determined-compute gpu_admission: cannot append $out/${receipt%.json}.jsonl" >&2
if [ "$status" = passed ]; then
  echo "determined-compute gpu_admission: passed: $n GPUs; receipt $out/$receipt"
  exit 0
fi
joined=$(printf '%s; ' "${failures[@]}")
echo "determined-compute gpu_admission: failed: ${joined%; }; receipt $out/$receipt"
exit 86
'''


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
    return f"/bin/bash -c {shlex.quote(SCRIPT_V1)} determined-compute-gpu-admission"


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
