"""Interpretation of a job's measured CPU, memory and GPU use.

A job's submission names its tasks and their allocations, with each allocation's slots, pool,
placement, times and exit, so this module reads only the measurements and best-effort context:
the trial's progress, the pool's description, and GPU models. A failed context lookup is
reported and never hides a measurement.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from determined_compute.client import APIError

# Units of the fixed metric names served by the task resources API.
METRICS = {
    "allocation_active": "count",
    "cpu_cores": "cores",
    "memory_working_set_bytes": "bytes",
    "memory_rss_bytes": "bytes",
    "gpu_utilization_percent": "percent",
    "gpu_memory_used_bytes": "bytes",
    "gpu_power_watts": "watts",
    "gpu_temperature_celsius": "celsius",
}
# Server limits: at most seven days, a 15-second step, and 1,440 points per series.
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
_MIN_STEP_SECONDS = 15
_MAX_POINTS = 1440
_MAX_RETURNED_SAMPLES = 2880
_MAX_SUMMARY_METRICS = 100
_SUMMARY_STATISTICS = ("count", "sum", "min", "max", "last", "mean")
# A GPU utilization sample below this percentage counts as idle.
_GPU_IDLE_PERCENT = 10
ADVISORY = (
    "Values are point samples taken every step seconds, so min, max, and mean describe "
    "those samples rather than every moment of the window. A missing value means no "
    "measurement, not zero use. GPU metrics describe each whole assigned device and may "
    "include other processes. allocation_active above zero means the allocation was running. "
    f"idle_fraction is the share of GPU utilization samples below {_GPU_IDLE_PERCENT}%, and "
    "p50 and p95 are nearest-rank percentiles of the available samples. "
    "Coverage depends on the cluster's monitoring retention."
)
_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?")


class UsageError(ValueError):
    """A usage request that cannot be answered. ``code`` is stable."""

    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _unix_seconds(value: Any) -> Optional[int]:
    """Floor an RFC 3339 timestamp to Unix seconds; a missing offset means UTC."""

    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    base, offset = match.groups()
    try:
        parsed = datetime.fromisoformat(base + ("+00:00" if offset in {None, "Z"} else offset))
    except ValueError:
        return None
    return int(parsed.timestamp())


def _iso_seconds(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _percentile(ordered: Sequence[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile of already sorted values."""

    if not ordered:
        return None
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _idle_fraction(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(value < _GPU_IDLE_PERCENT for value in values) / len(values), 6)


def _check_arguments(
    window_seconds: Any,
    allocation_id: Any,
    trial_id: Any,
    metrics: Any,
    include_samples: Any,
) -> Optional[List[str]]:
    def invalid(message: str) -> UsageError:
        return UsageError(message, code="invalid_request")

    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS
    ):
        raise invalid(
            f"window_seconds must be an integer from {MIN_WINDOW_SECONDS} "
            f"to {MAX_WINDOW_SECONDS}"
        )
    if allocation_id is not None and (
        not isinstance(allocation_id, str)
        or not 1 <= len(allocation_id) <= 256
        or not allocation_id.isprintable()
        or allocation_id.strip() != allocation_id
    ):
        raise invalid("allocation_id must be a printable string of 1 to 256 characters")
    if trial_id is not None and (
        isinstance(trial_id, bool) or not isinstance(trial_id, int) or trial_id < 1
    ):
        raise invalid("trial_id must be a positive integer")
    if not isinstance(include_samples, bool):
        raise invalid("include_samples must be a boolean")
    if metrics is None:
        return None
    if (
        isinstance(metrics, str)
        or not isinstance(metrics, (list, tuple))
        or not metrics
        or not all(isinstance(item, str) and item in METRICS for item in metrics)
    ):
        raise invalid(f"metrics must be a non-empty list drawn from: {', '.join(METRICS)}")
    return list(dict.fromkeys(metrics))


def select_task(
    submission: Mapping[str, Any],
    trial_id: Optional[int],
    allocation_id: Optional[str] = None,
) -> Tuple[Mapping[str, Any], Optional[Dict[str, Any]]]:
    """Return the task to measure and, for an experiment, what the submission says of its trial.

    Tasks come oldest first, so the last task of a trial is the one a continued trial runs now.
    An ``allocation_id`` selects the task that holds it, whichever trial that is.
    """

    tasks = submission["tasks"]
    holder = None
    if allocation_id is not None:
        holder = next(
            (
                task
                for task in tasks
                if any(item["allocation_id"] == allocation_id for item in task["allocations"])
            ),
            None,
        )
        if holder is None:
            raise UsageError("the allocation does not belong to this job", code="not_found")
    if submission["kind"] != "experiment":
        if trial_id is not None:
            raise UsageError("trial_id applies only to experiments", code="invalid_request")
        if not tasks:
            raise UsageError("the job has no task yet", code="task_not_started")
        return holder or tasks[-1], None
    trials = sorted({task["trial_id"] for task in tasks if task["trial_id"] is not None})
    if holder is not None:
        if trial_id is not None and holder["trial_id"] != trial_id:
            raise UsageError(
                f"the allocation belongs to trial {holder['trial_id']}, not trial {trial_id}",
                code="invalid_request",
            )
        if holder["trial_id"] is None:
            return holder, None
        chosen, selection = holder["trial_id"], "allocation"
    elif trial_id is None:
        if not trials:
            raise UsageError("the experiment has no trials yet", code="task_not_started")
        chosen, selection = trials[-1], "latest"
    elif trial_id in trials:
        chosen, selection = trial_id, "requested"
    else:
        raise UsageError(f"trial {trial_id} does not belong to this job", code="not_found")
    own = [task for task in tasks if task["trial_id"] == chosen]
    trial = {
        "id": chosen,
        "selection": selection,
        "experiment_trial_count": len(trials),
        "task_count": len(own),
    }
    return holder or own[-1], trial


def _window(
    submission: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]],
    allocation_id: Optional[str],
    window_seconds: int,
    now: int,
) -> Dict[str, Any]:
    ended = _unix_seconds(submission["ended_at"])
    # Mirror the WebUI: an ended job shows the window preceding its end.
    end, anchor = (min(ended, now), "job_end") if ended is not None else (now, "now")
    # A paused trial has no end, and an older allocation of a running task has its own; nothing
    # is measured after the last selected allocation ends. An allocation that ended before it
    # started has no end time and no measurements, so only started ones set the end.
    if selected and all(item["state"] == "terminated" for item in selected):
        ends = [_unix_seconds(item["end_time"]) for item in selected if item["start_time"]]
        if ends and all(value is not None for value in ends) and max(ends) < end:
            end, anchor = max(ends), "allocation_end"
    start = end - window_seconds
    floors = [_unix_seconds(submission["submitted_at"])]
    if allocation_id is not None:
        floors.append(_unix_seconds(selected[0]["start_time"]))
    for floor in floors:
        if floor is not None:
            start = max(start, floor)
    start = max(0, min(start, end - 1))
    step = max(_MIN_STEP_SECONDS, math.ceil((end - start) / (_MAX_POINTS - 1)))
    return {
        "start": start,
        "end": end,
        "step": step,
        "start_at": _iso_seconds(start),
        "end_at": _iso_seconds(end),
        "anchor": anchor,
        "expected_points": (end - start) // step + 1,
    }


def summarize(
    client: Any,
    submission: Mapping[str, Any],
    *,
    trial_id: Optional[int] = None,
    allocation_id: Optional[str] = None,
    window_seconds: int = 3600,
    metrics: Optional[Sequence[str]] = None,
    include_samples: bool = False,
) -> Dict[str, Any]:
    """Summarize the measured use of one job, as ``client.get_submission`` returned it.

    ``client`` provides ``task_resources_enabled``, ``get_task_resources``, ``get_trial``,
    ``list_resource_pools`` and ``list_agents``. Without task-resources data the result is
    labelled ``unmeasured`` and still describes the allocations.
    """

    metrics = _check_arguments(window_seconds, allocation_id, trial_id, metrics, include_samples)
    task, trial = select_task(submission, trial_id, allocation_id)
    allocations = [dict(item) for item in task["allocations"]]
    selected = [
        item
        for item in allocations
        if allocation_id is None or item["allocation_id"] == allocation_id
    ]
    if allocation_id is not None and not selected:  # select_task chose the allocation's task
        raise UsageError("the allocation does not belong to the selected task", code="not_found")

    now = int(time.time())
    enabled = client.task_resources_enabled()
    window = None
    if enabled:
        window = _window(submission, selected, allocation_id, window_seconds, now)
    response: Dict[str, Any] = {"series": [], "warnings": []}
    if window is not None:
        response = client.get_task_resources(
            task["task_id"],
            start=window["start"],
            end=window["end"],
            step=window["step"],
            allocation_id=allocation_id,
        )
    returned = response["series"]
    series = [item for item in returned if metrics is None or item["metric"] in metrics]

    unavailable: List[str] = []
    unreachable = False

    def context(name: str, operation: Any, *args: Any) -> Any:
        nonlocal unreachable
        if not unreachable:
            try:
                return operation(*args)
            except APIError as exc:
                # After a timeout, each further lookup would wait the full timeout too.
                unreachable = exc.code == "unavailable"
        if name not in unavailable:
            unavailable.append(name)
        return None

    if trial is not None:
        detail = context("trial", client.get_trial, trial["id"])
        # A trial of another experiment would describe the wrong job.
        if detail is not None and str(detail.get("experimentId")) != submission["entity_id"]:
            detail = None
            unavailable.append("trial")
        trial.update(_trial_progress(detail))
    pool_name = next(
        (item["resource_pool"] for item in reversed(selected) if item["resource_pool"]), None
    )
    resource_pool = None
    if pool_name is not None:
        pools = context("resource_pool", client.list_resource_pools) or []
        described = next((item for item in pools if item["name"] == pool_name), None)
        resource_pool = {
            "name": pool_name,
            "description": described["description"] if described else None,
        }
    gpu_models: Dict[str, str] = {}
    if any(item["labels"]["gpu_uuid"] for item in returned):
        agents = context("gpu_models", client.list_agents) or []
        gpu_models = {
            device["uuid"]: device["brand"]
            for agent in agents
            for device in agent["devices"]
            if device["uuid"] and device["brand"] and device["type"] in {"cuda", "rocm"}
        }

    summaries = [_series(item, gpu_models) for item in series]
    result: Dict[str, Any] = {
        "job_id": submission["job_id"],
        "kind": submission["kind"],
        "task_id": task["task_id"],
        "trial": trial,
        "allocation_id": allocation_id,
        "allocations": allocations,
        "resource_pool": resource_pool,
        "submitted_at": submission["submitted_at"],
        "ended_at": submission["ended_at"],
        "measurement": "measured" if enabled else "unmeasured",
        "window": window,
        "series": summaries,
        "gpus": _gpus(returned, gpu_models, allocations),
        "warnings": response["warnings"],
        "context_unavailable": unavailable,
        "explanation": _explain(enabled, summaries, returned, window, trial),
        "observed_at": _iso_seconds(now),
        "advisory": ADVISORY,
    }
    if include_samples:
        omitted = sum(len(item["samples"]) for item in series) > _MAX_RETURNED_SAMPLES
        result["samples_omitted"] = omitted
        if omitted:
            result["samples_limit"] = _MAX_RETURNED_SAMPLES
        else:
            for summary, item in zip(summaries, series):
                summary["samples"] = item["samples"]
    return result


def _explain(
    enabled: bool,
    summaries: Sequence[Mapping[str, Any]],
    returned: Sequence[Mapping[str, Any]],
    window: Optional[Mapping[str, Any]],
    trial: Optional[Mapping[str, Any]],
) -> str:
    if not enabled:
        explanation = (
            "Unmeasured: task resource monitoring is not enabled on this Determined master, so "
            "only the allocations are described"
        )
    elif summaries:
        explanation = f"{len(summaries)} measurement series over the selected window"
    elif returned:
        others = sorted({item["metric"] for item in returned})
        explanation = (
            "None of the requested metrics were measured in this window; Determined "
            f"returned {len(returned)} other series ({', '.join(others)})"
        )
    else:
        explanation = (
            "No measurements were returned; the task may not have run in this "
            "window, or monitoring retained no data for it"
        )
    if window is not None and window["anchor"] == "allocation_end":
        explanation += (
            "; the window ends when the last selected allocation ended because no "
            "selected allocation is running"
        )
    if trial is not None and trial["experiment_trial_count"] > 1:
        explanation += (
            f"; the experiment has {trial['experiment_trial_count']} trials and this "
            f"reports trial {trial['id']}, so pass trial_id to inspect another"
        )
    if trial is not None and trial.get("total_batches_processed") == 0:
        explanation += (
            "; the trial reports no batches, which is expected when the workload does "
            "not report training progress through Determined's Core API or has not "
            "reported yet"
        )
    return explanation


def _trial_progress(trial: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Progress fields of a trial response; all None when it was unavailable."""

    trial = trial or {}
    state = trial.get("state")
    batches, restarts = (
        value if not isinstance(value, bool) and isinstance(value, int) and value >= 0 else None
        for value in (trial.get("totalBatchesProcessed"), trial.get("restarts"))
    )
    wall_clock = trial.get("wallClockTime")
    wall_clock = float(wall_clock) if _finite(wall_clock) and wall_clock >= 0 else None
    summary_metrics, truncated = _summary_metrics(trial.get("summaryMetrics"))
    return {
        "state": _trial_state(state),
        "total_batches_processed": batches,
        "wall_clock_seconds": wall_clock,
        "restarts": restarts,
        # Wall-clock time spans every allocation, including image pulls, startup, and
        # restarts, so this is a floor on the training rate.
        "batches_per_second_lower_bound": (
            round(batches / wall_clock, 6) if batches is not None and wall_clock else None
        ),
        "summary_metrics": summary_metrics,
        "summary_metrics_truncated": truncated,
    }


def _trial_state(value: Any) -> Optional[str]:
    """``STATE_NAME`` as ``name``, like a job's states; unspecified or unknown is None.

    The trial is best-effort context, so a malformed state is dropped rather than refused.
    """

    if not isinstance(value, str) or not value.startswith("STATE_"):
        return None
    name = value[len("STATE_"):].lower()
    return None if name == "unspecified" else name


def _summary_metrics(value: Any) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Keep numeric per-metric statistics from a trial's summary metrics."""

    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, Mapping):
        return result, False
    kept = 0
    # Determined's built-in groups come first so a large training or custom group cannot
    # crowd validation metrics out of the cap.
    builtin = ("validation_metrics", "avg_metrics")
    groups = [key for key in builtin if key in value] + sorted(
        key for key in value if isinstance(key, str) and key not in builtin
    )
    for group in groups:
        metrics = value[group]
        if not isinstance(metrics, Mapping):
            continue
        for name in sorted(key for key in metrics if isinstance(key, str)):
            stats = metrics[name]
            if not isinstance(stats, Mapping):
                continue
            if kept == _MAX_SUMMARY_METRICS:
                return result, True
            entry: Dict[str, Any] = (
                {"type": stats["type"]} if isinstance(stats.get("type"), str) else {}
            )
            entry.update(
                (key, stats[key]) for key in _SUMMARY_STATISTICS if _finite(stats.get(key))
            )
            result.setdefault(group, {})[name] = entry
            kept += 1
    return result, False


def _series(series: Mapping[str, Any], gpu_models: Mapping[str, str]) -> Dict[str, Any]:
    points = sorted((stamp, value) for stamp, value in series["samples"] if value is not None)
    values = [value for _stamp, value in points]
    ordered = sorted(values)
    gpu_uuid = series["labels"]["gpu_uuid"]
    summary = {
        "metric": series["metric"],
        "unit": METRICS.get(series["metric"]),
        **series["labels"],
        "gpu_model": gpu_models.get(gpu_uuid) if gpu_uuid else None,
        "points": len(series["samples"]),
        "available_points": len(values),
        "first_at": _iso_seconds(points[0][0]) if points else None,
        "last_at": _iso_seconds(points[-1][0]) if points else None,
        "last": values[-1] if values else None,
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
        "mean": round(sum(values) / len(values), 6) if values else None,
        "p50": _percentile(ordered, 0.5),
        "p95": _percentile(ordered, 0.95),
    }
    if series["metric"] == "gpu_utilization_percent":
        summary["idle_fraction"] = _idle_fraction(values)
    return summary


def _gpus(
    series: Sequence[Mapping[str, Any]],
    gpu_models: Mapping[str, str],
    allocations: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Compare the GPUs of each allocation using every returned GPU series."""

    slots = {item["allocation_id"]: item["slots"] for item in allocations}
    groups: Dict[Optional[str], Dict[str, Dict[str, List[float]]]] = {}
    for item in series:
        gpu_uuid = item["labels"]["gpu_uuid"]
        if not gpu_uuid or item["metric"] not in {
            "gpu_utilization_percent",
            "gpu_memory_used_bytes",
        }:
            continue
        values = [value for _stamp, value in item["samples"] if value is not None]
        devices = groups.setdefault(item["labels"]["allocation_id"], {})
        devices.setdefault(gpu_uuid, {}).setdefault(item["metric"], []).extend(values)
    result: List[Dict[str, Any]] = []
    for allocation, devices in sorted(groups.items(), key=lambda pair: pair[0] or ""):
        means = {
            gpu_uuid: sum(values) / len(values)
            for gpu_uuid, metrics in devices.items()
            if (values := metrics.get("gpu_utilization_percent"))
        }
        utilization = [
            value
            for metrics in devices.values()
            for value in metrics.get("gpu_utilization_percent", [])
        ]
        memory = [
            value
            for metrics in devices.values()
            for value in metrics.get("gpu_memory_used_bytes", [])
        ]
        lowest = min(sorted(means), key=means.__getitem__) if means else None
        result.append(
            {
                "allocation_id": allocation,
                "gpu_count": len(devices),
                "requested_slots": slots.get(allocation) if allocation is not None else None,
                "gpu_models": sorted({gpu_models[key] for key in devices if key in gpu_models}),
                # Each GPU counts equally, whatever its number of available samples.
                "mean_utilization_percent": (
                    round(sum(means.values()) / len(means), 6) if means else None
                ),
                "min_gpu_mean_utilization_percent": round(means[lowest], 6) if means else None,
                "max_gpu_mean_utilization_percent": (
                    round(max(means.values()), 6) if means else None
                ),
                "utilization_spread_percent": (
                    round(max(means.values()) - means[lowest], 6) if means else None
                ),
                "least_utilized_gpu_uuid": lowest,
                "idle_fraction": _idle_fraction(utilization),
                "idle_threshold_percent": _GPU_IDLE_PERCENT,
                "max_memory_used_bytes": max(memory) if memory else None,
            }
        )
    return result


__all__ = ["ADVISORY", "METRICS", "UsageError", "summarize"]
