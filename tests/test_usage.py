"""Usage interpretation over a parsed submission and a fake client."""

from __future__ import annotations

import copy

import pytest

from determined_compute import usage as usage_module
from determined_compute.client import APIError, parse_submission
from determined_compute.usage import UsageError, summarize

NOW = 1_800_000_000  # 2027-01-15T08:00:00Z
SUBMITTED = "2027-01-11T04:00:00Z"  # NOW - 100 hours, well before the default window
JOB = "job-1"
TASK = "task-1"


def allocation(number=1, task=TASK, state="STATE_RUNNING", start=None, end=None, **fields):
    value = {
        "taskId": task,
        "allocationId": f"{task}.{number}",
        "state": state,
        "slots": 1,
        "exitClass": "EXIT_CLASS_UNSPECIFIED",
        "exitDetail": None,
        "resourcePool": "gpu",
        "placements": [],
    }
    if start is not None:
        value["startTime"] = start
    if end is not None:
        value["endTime"] = end
    value.update(fields)
    return value


def job(kind="command", tasks=None, ended=None, submitted=SUBMITTED):
    """A submission in the client's parsed form, built from the wire shape."""
    if tasks is None:
        tasks = [{"taskId": TASK, "allocations": [allocation(start=SUBMITTED)]}]
    return parse_submission({
        "jobId": JOB,
        "kind": f"SUBMISSION_KIND_{kind.upper()}",
        "entityId": "9" if kind == "experiment" else TASK,
        "ownerId": 1,
        "owner": "alice",
        "workspaceId": 1,
        "name": "usage probe",
        "admission": "ADMISSION_QUEUE",
        "submittedAt": submitted,
        "endedAt": ended,
        "state": "SUBMISSION_STATE_RUNNING",
        "exitClass": "EXIT_CLASS_UNSPECIFIED",
        "exitReason": "",
        "tasks": tasks,
    })


def trial_task(trial, task, *allocations):
    return {"taskId": task, "trialId": trial,
            "allocations": list(allocations) or [allocation(task=task, start=SUBMITTED)]}


def series(metric, samples, allocation=f"{TASK}.1", node="node-a", gpu=None):
    return {
        "metric": metric,
        "labels": {"allocation_id": allocation, "node": node, "gpu_uuid": gpu},
        "samples": samples,
    }


class UsageClient:
    def __init__(self) -> None:
        self.calls = []
        self.enabled = True
        self.series = []
        self.warnings = []
        self.pools = [{"name": "gpu", "description": "Shared GPU agents"}]
        self.agents = []
        self.trials = {}
        self.failing = set()

    def task_resources_enabled(self):
        self.calls.append(("capability",))
        return self.enabled

    def get_task_resources(self, task_id, *, start, end, step, allocation_id=None):
        self.calls.append(("resources", task_id, start, end, step, allocation_id))
        return {"enabled": True, "series": copy.deepcopy(self.series),
                "warnings": copy.deepcopy(self.warnings)}

    def _context(self, call, value):
        self.calls.append(call)
        if call[0] in self.failing:
            raise APIError("context unavailable", code="internal")
        return copy.deepcopy(value)

    def get_trial(self, trial_id):
        default = {"id": trial_id, "experimentId": 9}
        return self._context(("trial", trial_id), self.trials.get(trial_id, default))

    def list_resource_pools(self):
        return self._context(("pools",), self.pools)

    def list_agents(self):
        return self._context(("agents",), self.agents)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(usage_module.time, "time", lambda: NOW + 0.75)


@pytest.fixture
def client():
    return UsageClient()


def resources_call(client):
    (call,) = [item for item in client.calls if item[0] == "resources"]
    return call


def test_running_command_uses_trailing_window_and_summarizes_non_null_samples(client):
    client.series = [
        series("cpu_cores", [[NOW - 30, 1.5], [NOW - 15, None], [NOW, 0.5]]),
        series("gpu_utilization_percent", [[NOW, None]], gpu="GPU-1"),
    ]
    client.warnings = [{"code": "gpu_full_device", "message": "whole device"}]
    client.agents = [{"id": "a", "devices": [
        {"type": "cuda", "brand": "Model X", "uuid": "GPU-1"},
        {"type": "cpu", "brand": "CPU", "uuid": "cpu-0"},
    ]}]

    result = summarize(client, job())

    assert client.calls == [
        ("capability",),
        ("resources", TASK, NOW - 3600, NOW, 15, None),
        ("pools",),
        ("agents",),
    ]
    assert (result["job_id"], result["kind"], result["task_id"]) == (JOB, "command", TASK)
    assert result["measurement"] == "measured"
    assert result["trial"] is None
    assert result["resource_pool"] == {"name": "gpu", "description": "Shared GPU agents"}
    assert result["window"] == {
        "start": NOW - 3600,
        "end": NOW,
        "step": 15,
        "start_at": "2027-01-15T07:00:00+00:00",
        "end_at": "2027-01-15T08:00:00+00:00",
        "anchor": "now",
        "expected_points": 241,
    }
    assert result["allocations"][0]["allocation_id"] == f"{TASK}.1"
    cpu, gpu = result["series"]
    assert cpu == {
        "metric": "cpu_cores",
        "unit": "cores",
        "allocation_id": f"{TASK}.1",
        "node": "node-a",
        "gpu_uuid": None,
        "gpu_model": None,
        "points": 3,
        "available_points": 2,
        "first_at": "2027-01-15T07:59:30+00:00",
        "last_at": "2027-01-15T08:00:00+00:00",
        "last": 0.5,
        "min": 0.5,
        "max": 1.5,
        "mean": 1.0,
        "p50": 0.5,
        "p95": 1.5,
    }
    assert gpu["gpu_model"] == "Model X"
    assert gpu["available_points"] == 0 and gpu["idle_fraction"] is None
    assert result["warnings"] == client.warnings
    assert result["observed_at"] == "2027-01-15T08:00:00+00:00"
    assert "not zero use" in result["advisory"]
    assert "samples" not in cpu and "samples_omitted" not in result


def test_ended_job_uses_the_window_before_its_end_and_never_before_submission(client):
    ended = job(submitted="2027-01-15T06:58:00.9Z", ended="2027-01-15T07:00:00.999999Z",
                tasks=[{"taskId": TASK, "allocations": []}])

    window = summarize(client, ended, window_seconds=604800)["window"]

    assert (window["start"], window["end"]) == (NOW - 3600 - 120, NOW - 3600)
    assert (window["step"], window["anchor"]) == (15, "job_end")


def test_week_window_step_respects_the_point_limit(client):
    window = summarize(client, job(submitted="2020-01-01T00:00:00Z"),
                       window_seconds=604800)["window"]

    assert window["end"] - window["start"] == 604800
    assert window["step"] == 421
    assert window["expected_points"] <= 1440


def test_without_monitoring_the_allocations_are_described_as_unmeasured(client):
    client.enabled = False

    result = summarize(client, job())

    assert client.calls == [("capability",), ("pools",)]
    assert result["measurement"] == "unmeasured"
    assert result["window"] is None and result["series"] == [] and result["gpus"] == []
    assert result["allocations"][0]["slots"] == 1
    assert result["explanation"].startswith("Unmeasured")


def test_allocation_must_belong_to_the_job(client):
    with pytest.raises(UsageError) as caught:
        summarize(client, job(), allocation_id="other.1")
    assert caught.value.code == "not_found"
    assert client.calls == []

    summarize(client, job(), allocation_id=f"{TASK}.1")
    assert resources_call(client)[-1] == f"{TASK}.1"


def test_an_allocation_selects_its_own_trial_and_task(client):
    experiment = job("experiment", tasks=[
        trial_task(1, "trial-1"),
        trial_task(2, "trial-2a", allocation(task="trial-2a", start=SUBMITTED)),
        trial_task(2, "trial-2b", allocation(task="trial-2b", start=SUBMITTED)),
    ])

    earlier_trial = summarize(client, experiment, allocation_id="trial-1.1")
    assert resources_call(client)[1:2] == ("trial-1",)
    assert earlier_trial["task_id"] == "trial-1"
    assert (earlier_trial["trial"]["id"], earlier_trial["trial"]["selection"]) == (1, "allocation")

    client.calls.clear()
    # An earlier task of a continued trial, which the latest-task default would pass over.
    earlier_task = summarize(client, experiment, trial_id=2, allocation_id="trial-2a.1")
    assert resources_call(client)[1] == "trial-2a"
    assert (earlier_task["task_id"], earlier_task["trial"]["task_count"]) == ("trial-2a", 2)

    client.calls.clear()
    with pytest.raises(UsageError, match="belongs to trial 1, not trial 2") as caught:
        summarize(client, experiment, trial_id=2, allocation_id="trial-1.1")
    assert caught.value.code == "invalid_request"
    assert client.calls == []


def test_metric_filter_and_bounded_samples(client, monkeypatch):
    client.series = [
        series("cpu_cores", [[NOW, 1.0]]),
        series("memory_rss_bytes", [[NOW, 0], [NOW - 15, 0]]),
    ]

    filtered = summarize(client, job(), metrics=["memory_rss_bytes"], include_samples=True)
    assert [item["metric"] for item in filtered["series"]] == ["memory_rss_bytes"]
    assert filtered["samples_omitted"] is False
    assert filtered["series"][0]["samples"] == [[NOW, 0], [NOW - 15, 0]]
    assert filtered["series"][0]["last"] == 0

    monkeypatch.setattr(usage_module, "_MAX_RETURNED_SAMPLES", 2)
    bounded = summarize(client, job(), include_samples=True)
    assert bounded["samples_omitted"] is True and bounded["samples_limit"] == 2
    assert all("samples" not in item for item in bounded["series"])


def test_filter_that_removes_every_series_names_the_returned_metrics(client):
    client.series = [series("allocation_active", [[NOW, 1.0]]), series("cpu_cores", [[NOW, 0.5]])]

    result = summarize(client, job(), metrics=["gpu_utilization_percent"])

    assert result["series"] == []
    assert "allocation_active, cpu_cores" in result["explanation"]


def test_experiment_reports_the_latest_trial_and_its_newest_task(client):
    experiment = job("experiment", tasks=[
        trial_task(3, "9.a"), trial_task(17, "9.b"), trial_task(17, "9.b-1"), trial_task(5, "9.c"),
    ])

    result = summarize(client, experiment)

    assert resources_call(client)[1] == "9.b-1"
    assert ("trial", 17) in client.calls
    assert result["task_id"] == "9.b-1"
    assert result["trial"] == {
        "id": 17,
        "selection": "latest",
        "experiment_trial_count": 3,
        "task_count": 2,
        "state": None,
        "total_batches_processed": None,
        "wall_clock_seconds": None,
        "restarts": None,
        "batches_per_second_lower_bound": None,
        "summary_metrics": {},
        "summary_metrics_truncated": False,
    }
    assert "has 3 trials" in result["explanation"]
    assert "reports trial 17" in result["explanation"]


def test_requested_trial_must_belong_to_the_job(client):
    experiment = job("experiment", tasks=[trial_task(4, "9.a")])

    result = summarize(client, experiment, trial_id=4)
    assert result["trial"]["selection"] == "requested"
    assert "pass trial_id" not in result["explanation"]

    client.calls.clear()
    with pytest.raises(UsageError) as caught:
        summarize(client, experiment, trial_id=5)
    assert caught.value.code == "not_found"
    assert client.calls == []


def test_a_trial_of_another_experiment_is_not_reported(client):
    client.trials[4] = {"id": 4, "experimentId": 10, "totalBatchesProcessed": 5}

    result = summarize(client, job("experiment", tasks=[trial_task(4, "9.a")]))

    assert result["context_unavailable"] == ["trial"]
    assert result["trial"]["total_batches_processed"] is None


def test_jobs_without_tasks_or_trials_are_not_started(client):
    for submission in (job("experiment", tasks=[]), job(tasks=[])):
        with pytest.raises(UsageError) as caught:
            summarize(client, submission)
        assert caught.value.code == "task_not_started"


def test_trial_id_requires_an_experiment(client):
    with pytest.raises(UsageError, match="experiment") as caught:
        summarize(client, job(), trial_id=1)
    assert caught.value.code == "invalid_request"
    assert client.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"window_seconds": 59},
        {"window_seconds": 604801},
        {"window_seconds": True},
        {"allocation_id": ""},
        {"allocation_id": " x"},
        {"trial_id": 0},
        {"trial_id": "1"},
        {"metrics": []},
        {"metrics": "cpu_cores"},
        {"metrics": ["disk_bytes"]},
        {"include_samples": "yes"},
    ],
)
def test_invalid_arguments_fail_before_any_access(client, arguments):
    with pytest.raises(UsageError) as caught:
        summarize(client, job("experiment", tasks=[trial_task(1, "9.a")]), **arguments)
    assert caught.value.code == "invalid_request"
    assert client.calls == []


def test_paused_trial_window_ends_at_the_last_allocation_end(client):
    # Pausing leaves the job without an end while every allocation has ended; the one that
    # ended before it started has no times and does not count.
    experiment = job("experiment", tasks=[trial_task(
        3, "9.p",
        allocation(1, "9.p", "STATE_TERMINATED", "2027-01-15T01:00:00.5Z", "2027-01-15T02:00:00Z"),
        allocation(2, "9.p", "STATE_TERMINATED", "2027-01-15T02:30:00Z", "2027-01-15T05:00:00.1Z"),
        allocation(3, "9.p", "STATE_TERMINATED"),
    )])

    result = summarize(client, experiment)

    end = NOW - 3 * 3600
    assert (result["window"]["start"], result["window"]["end"]) == (end - 3600, end)
    assert result["window"]["anchor"] == "allocation_end"
    assert "no selected allocation is running" in result["explanation"]


def test_cancelled_job_uses_the_allocation_end_before_the_job_end(client):
    cancelled = job(ended="2027-01-15T07:00:00Z", tasks=[{"taskId": TASK, "allocations": [
        allocation(1, state="STATE_TERMINATED", start="2027-01-15T03:00:00Z",
                   end="2027-01-15T04:00:00Z"),
    ]}])

    window = summarize(client, cancelled)["window"]

    assert (window["start"], window["end"]) == (NOW - 5 * 3600, NOW - 4 * 3600)
    assert window["anchor"] == "allocation_end"


def test_a_job_cancelled_while_queued_keeps_the_job_end(client):
    cancelled = job(ended="2027-01-15T07:00:00Z", tasks=[{"taskId": TASK, "allocations": [
        allocation(1, state="STATE_TERMINATED"),
    ]}])

    window = summarize(client, cancelled)["window"]

    assert (window["end"], window["anchor"]) == (NOW - 3600, "job_end")


def test_requested_finished_allocation_of_a_running_task_uses_its_lifetime(client):
    running = job(tasks=[{"taskId": TASK, "allocations": [
        allocation(1, state="STATE_TERMINATED", start="2027-01-15T02:30:00Z",
                   end="2027-01-15T03:00:00Z"),
        allocation(2, start="2027-01-15T03:00:05Z"),
    ]}])

    first = summarize(client, running, window_seconds=86400, allocation_id=f"{TASK}.1")["window"]
    assert (first["start"], first["end"]) == (NOW - 5 * 3600 - 1800, NOW - 5 * 3600)
    assert first["anchor"] == "allocation_end"

    whole = summarize(client, running)["window"]
    assert (whole["end"], whole["anchor"]) == (NOW, "now")


def test_unparsable_allocation_times_fall_back_to_now(client):
    odd = job(tasks=[{"taskId": TASK, "allocations": [
        allocation(1, state="STATE_TERMINATED", start="later", end="soon"),
    ]}])

    result = summarize(client, odd)

    assert (result["window"]["end"], result["window"]["anchor"]) == (NOW, "now")
    assert result["allocations"][0]["end_time"] == "soon"


@pytest.mark.parametrize(
    ("submitted", "ended", "expected_end"),
    [
        ("2027-01-15T08:00:05Z", None, NOW),  # master clock ahead of the client
        ("2027-01-15T07:00:00Z", "2027-01-15T07:00:00.5Z", NOW - 3600),  # same second
    ],
)
def test_window_is_never_empty_or_inverted(client, submitted, ended, expected_end):
    window = summarize(client, job(submitted=submitted, ended=ended,
                                   tasks=[{"taskId": TASK, "allocations": []}]))["window"]

    assert (window["start"], window["end"]) == (expected_end - 1, expected_end)
    assert window["step"] == 15 and window["expected_points"] == 1


def test_gpu_comparison_uses_every_returned_gpu_series(client):
    client.series = [
        series("cpu_cores", [[NOW, 2.0]]),
        series("gpu_utilization_percent", [[NOW - 15, 100], [NOW, 80]], gpu="GPU-A"),
        series("gpu_utilization_percent", [[NOW - 30, 0], [NOW - 15, 20], [NOW, 10]],
               gpu="GPU-B"),
        series("gpu_memory_used_bytes", [[NOW, 4e9], [NOW - 15, None]], gpu="GPU-A"),
        series("gpu_memory_used_bytes", [[NOW, 6e9]], gpu="GPU-B"),
        series("gpu_utilization_percent", [[NOW, 50]], allocation=f"{TASK}.0", gpu="GPU-C"),
    ]
    client.agents = [{"id": "a", "devices": [
        {"type": "cuda", "brand": "Model X", "uuid": "GPU-A"},
        {"type": "cuda", "brand": "Model Z", "uuid": "GPU-C"},
        {"type": "cuda", "brand": "Model Y", "uuid": None},
    ]}]
    two_slots = job(tasks=[{"taskId": TASK, "allocations": [allocation(slots=2, start=SUBMITTED)]}])

    result = summarize(client, two_slots, metrics=["cpu_cores"])

    assert [item["metric"] for item in result["series"]] == ["cpu_cores"]
    assert result["gpus"] == [
        {
            "allocation_id": f"{TASK}.0",
            "gpu_count": 1,
            "requested_slots": None,
            "gpu_models": ["Model Z"],
            "mean_utilization_percent": 50.0,
            "min_gpu_mean_utilization_percent": 50.0,
            "max_gpu_mean_utilization_percent": 50.0,
            "utilization_spread_percent": 0.0,
            "least_utilized_gpu_uuid": "GPU-C",
            "idle_fraction": 0.0,
            "idle_threshold_percent": 10,
            "max_memory_used_bytes": None,
        },
        {
            "allocation_id": f"{TASK}.1",
            "gpu_count": 2,
            "requested_slots": 2,
            "gpu_models": ["Model X"],
            "mean_utilization_percent": 50.0,
            "min_gpu_mean_utilization_percent": 10.0,
            "max_gpu_mean_utilization_percent": 90.0,
            "utilization_spread_percent": 80.0,
            "least_utilized_gpu_uuid": "GPU-B",
            "idle_fraction": 0.2,
            "idle_threshold_percent": 10,
            "max_memory_used_bytes": 6e9,
        },
    ]

    by_gpu = {
        item["gpu_uuid"]: item
        for item in summarize(client, two_slots, metrics=["gpu_utilization_percent"])["series"]
    }
    assert by_gpu["GPU-A"]["gpu_model"] == "Model X"
    assert by_gpu["GPU-B"]["gpu_model"] is None
    assert by_gpu["GPU-B"]["idle_fraction"] == pytest.approx(1 / 3, abs=1e-6)
    assert (by_gpu["GPU-B"]["p50"], by_gpu["GPU-B"]["p95"]) == (10, 20)


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [([4, 1, 3, 2], 2, 4), ([5, 1, 4, 2, 3], 3, 5), (list(range(1, 21)), 10, 19)],
)
def test_percentiles_are_nearest_rank(client, values, p50, p95):
    client.series = [series("cpu_cores", [[NOW - i, value] for i, value in enumerate(values)])]

    (summary,) = summarize(client, job())["series"]

    assert (summary["p50"], summary["p95"]) == (p50, p95)
    assert "idle_fraction" not in summary


def test_null_gpu_samples_are_unavailable_and_the_idle_threshold_is_ten_percent(client):
    client.series = [series("gpu_utilization_percent", [[NOW - 30, None], [NOW - 15, 9.99],
                                                        [NOW, 10]], gpu="GPU-A")]

    (summary,) = summarize(client, job())["series"]

    assert (summary["available_points"], summary["idle_fraction"]) == (2, 0.5)


@pytest.mark.parametrize(
    ("failing", "name"), [("pools", "resource_pool"), ("agents", "gpu_models")]
)
def test_failed_context_lookup_keeps_measurements(client, failing, name):
    client.series = [series("gpu_utilization_percent", [[NOW, 70]], gpu="GPU-A")]
    client.agents = [{"id": "a", "devices": [{"type": "cuda", "brand": "X", "uuid": "GPU-A"}]}]
    client.failing = {failing}

    result = summarize(client, job())

    assert result["context_unavailable"] == [name]
    assert result["series"][0]["last"] == 70
    if failing == "pools":
        assert result["resource_pool"] == {"name": "gpu", "description": None}
    else:
        assert result["series"][0]["gpu_model"] is None


def test_an_unreachable_master_skips_the_remaining_context_lookups(client):
    client.series = [series("gpu_utilization_percent", [[NOW, 70]], gpu="GPU-A")]

    def timeout(*args):
        client.calls.append(("timeout",))
        raise APIError("Determined did not answer", code="unavailable", retryable=True)

    client.get_trial = timeout
    result = summarize(client, job("experiment", tasks=[trial_task(1, "9.a", allocation(
        task="9.a", start=SUBMITTED))]))

    assert client.calls[client.calls.index(resources_call(client)) + 1:] == [("timeout",)]
    assert result["context_unavailable"] == ["trial", "resource_pool", "gpu_models"]
    assert result["series"][0]["last"] == 70


def test_non_api_errors_from_context_lookups_are_not_hidden(client):
    def broken():
        raise RuntimeError("bug")

    client.list_resource_pools = broken
    with pytest.raises(RuntimeError):
        summarize(client, job())


def test_trial_throughput_context(client):
    trial = {"id": 17, "experimentId": 9, "state": "STATE_ACTIVE",
             "totalBatchesProcessed": 1000, "wallClockTime": 500.0, "restarts": 1}
    client.trials[17] = trial
    experiment = job("experiment", tasks=[trial_task(17, "9.a")])

    context = summarize(client, experiment)["trial"]
    assert (context["total_batches_processed"], context["wall_clock_seconds"]) == (1000, 500.0)
    assert (context["restarts"], context["batches_per_second_lower_bound"]) == (1, 2.0)
    assert context["state"] == "active"  # short and lowercase, like a job's states

    trial.update(totalBatchesProcessed=0, wallClockTime=0)
    result = summarize(client, experiment)
    assert result["trial"]["batches_per_second_lower_bound"] is None
    assert "Core API" in result["explanation"]

    trial.update(totalBatchesProcessed=True, wallClockTime="500", restarts=-1)
    context = summarize(client, experiment)["trial"]
    assert (context["total_batches_processed"], context["wall_clock_seconds"]) == (None, None)
    assert context["restarts"] is None

    for state in ("STATE_UNSPECIFIED", "ACTIVE", 3):
        trial["state"] = state
        assert summarize(client, experiment)["trial"]["state"] is None


def test_summary_metrics_keep_numeric_statistics_and_report_truncation(client, monkeypatch):
    client.trials[17] = {
        "id": 17, "experimentId": 9,
        "summaryMetrics": {
            "perf": {"step_s": {"type": "number", "last": 0.3, "min": True}},
            "avg_metrics": {
                "loss": {"type": "number", "count": 10, "min": 0.1, "max": 2.0, "last": 0.2,
                         "sum": 5.0, "mean": 0.5, "extra": "drop"},
                "note": {"type": "string", "last": "user text"},
            },
            "validation_metrics": {"accuracy": {"type": "number", "last": 0.9}},
            "broken": ["not", "a", "map"],
        },
    }
    experiment = job("experiment", tasks=[trial_task(17, "9.a")])

    context = summarize(client, experiment)["trial"]
    assert context["summary_metrics"] == {
        "validation_metrics": {"accuracy": {"type": "number", "last": 0.9}},
        "avg_metrics": {
            "loss": {"type": "number", "count": 10, "sum": 5.0, "min": 0.1, "max": 2.0,
                     "last": 0.2, "mean": 0.5},
            "note": {"type": "string"},
        },
        "perf": {"step_s": {"type": "number", "last": 0.3}},
    }
    assert context["summary_metrics_truncated"] is False

    monkeypatch.setattr(usage_module, "_MAX_SUMMARY_METRICS", 2)
    context = summarize(client, experiment)["trial"]
    # Validation metrics survive the cap because the built-in groups come first.
    assert context["summary_metrics"] == {
        "validation_metrics": {"accuracy": {"type": "number", "last": 0.9}},
        "avg_metrics": {"loss": {"type": "number", "count": 10, "sum": 5.0, "min": 0.1,
                                 "max": 2.0, "last": 0.2, "mean": 0.5}},
    }
    assert context["summary_metrics_truncated"] is True


def test_pool_lookup_follows_the_selected_allocation(client):
    moved = job(tasks=[{"taskId": TASK, "allocations": [
        allocation(1, state="STATE_TERMINATED", resourcePool="cpu"),
        allocation(2, start=SUBMITTED),
    ]}])
    client.pools = [{"name": "cpu", "description": "CPU agents"},
                    {"name": "gpu", "description": ""}]

    assert summarize(client, moved)["resource_pool"] == {"name": "gpu", "description": ""}
    assert summarize(client, moved, allocation_id=f"{TASK}.1")["resource_pool"] == {
        "name": "cpu", "description": "CPU agents",
    }
    assert ("agents",) not in client.calls
