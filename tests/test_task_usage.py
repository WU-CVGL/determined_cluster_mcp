from __future__ import annotations

import copy

import pytest

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from determined_compute.compute import service as service_module


NOW = 1_800_000_000
TASK_START = "2027-01-11T04:00:00Z"  # NOW - 100 hours, well before the default window
CMD = "c0d0c0d0-0000-4000-8000-000000000001"


class UsageClient:
    api_url = "https://det.example.test"

    def __init__(self) -> None:
        self.calls = []
        self.enabled = True
        self.launched = 0
        self.task_info = {}
        self.latest = {"trial": None, "total": 0}
        self.trials = {}
        self.series = []
        self.warnings = []
        self.pools = [{"name": "gpu", "description": "Shared GPU agents"}]
        self.allocation_details = {}
        self.gpu_models = {}
        self.failing = set()

    def get_current_user(self):
        return {"id": "7", "username": "alice"}

    def launch_task(self, kind, config):
        self.launched += 1
        remote_id = CMD if kind == "command" else 9
        return {"id": remote_id, "state": "QUEUED"}

    def task_resources_enabled(self):
        self.calls.append(("capability",))
        return self.enabled

    def get_latest_trial(self, experiment_id):
        self.calls.append(("latest_trial", experiment_id))
        return copy.deepcopy(self.latest)

    def get_trial(self, trial_id):
        self.calls.append(("trial", trial_id))
        return copy.deepcopy(self.trials[trial_id])

    def get_task_info(self, task_id):
        self.calls.append(("task_info", task_id))
        return copy.deepcopy(
            self.task_info.get(
                task_id,
                {"task_id": task_id, "start_time": TASK_START, "end_time": None,
                 "allocations": [{"allocation_id": f"{task_id}.1", "state": "STATE_RUNNING",
                                  "is_ready": True, "start_time": None, "end_time": None}]},
            )
        )

    def get_task_resources(self, task_id, *, start, end, step, allocation_id=None):
        self.calls.append(("resources", task_id, start, end, step, allocation_id))
        return {"enabled": True, "series": copy.deepcopy(self.series),
                "warnings": copy.deepcopy(self.warnings)}

    def _context(self, call, value):
        self.calls.append(call)
        if call[0] in self.failing:
            raise APIError("context unavailable", code=503, retryable=True)
        return copy.deepcopy(value)

    def get_task(self, kind, remote_id):
        self.calls.append(("entity", kind, remote_id))
        return {"id": remote_id, "userId": 7, "resourcePool": "gpu"}

    def list_resource_pools(self):
        return self._context(("pools",), self.pools)

    def get_allocation(self, allocation_id):
        return self._context(
            ("allocation", allocation_id),
            self.allocation_details.get(
                allocation_id,
                {"allocation_id": allocation_id, "slots": 1, "exit_reason": None,
                 "status_code": None},
            ),
        )

    def list_gpu_devices(self):
        return self._context(("agents",), self.gpu_models)


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "registry/image:stable", "pool": "gpu", "slots": 1},
        }
    )


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(service_module.time, "time", lambda: NOW + 0.75)


def launched(tmp_path, profile, kind="command", client=None):
    client = client or UsageClient()
    service = ComputeService(client, profile)
    request = {
        "kind": kind,
        "name": "usage probe",
        "command": ["python", "train.py"],
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "allow_queue": True,
    }
    task = service.launch(request)
    client.calls.clear()
    return service, client, task


def resources_call(client):
    (call,) = [item for item in client.calls if item[0] == "resources"]
    return call


def context_calls(allocations, agents=False):
    calls = [("pools",)] + [("allocation", item) for item in allocations]
    return calls + [("agents",)] if agents else calls


def series(metric, samples, allocation=f"{CMD}.1", node="node-a", gpu=None):
    return {
        "metric": metric,
        "labels": {"allocation_id": allocation, "node": node, "gpu_uuid": gpu},
        "samples": samples,
    }


def test_running_command_uses_trailing_window_and_summarizes_non_null_samples(
    tmp_path, profile
):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("cpu_cores", [[NOW - 30, 1.5], [NOW - 15, None], [NOW, 0.5]]),
        series("gpu_utilization_percent", [[NOW, None]], gpu="GPU-1"),
    ]
    client.warnings = [{"code": "gpu_full_device", "message": "whole device"}]

    result = service.usage(task["kind"], task["id"])

    assert client.calls == [
        ("entity", "command", CMD),
        ("capability",),
        ("task_info", CMD),
        ("resources", CMD, NOW - 3600, NOW, 15, None),
    ] + context_calls([f"{CMD}.1"], agents=True)
    assert (result["kind"], result["id"]) == ("command", CMD)
    assert result["determined_task_id"] == CMD
    assert result["trial"] is None
    assert result["window"] == {
        "start": NOW - 3600,
        "end": NOW,
        "step": 15,
        "start_at": "2027-01-15T07:00:00+00:00",
        "end_at": "2027-01-15T08:00:00+00:00",
        "anchor": "now",
        "expected_points": 241,
    }
    assert result["allocations"][0]["allocation_id"] == f"{CMD}.1"
    cpu, gpu = result["series"]
    assert cpu == {
        "metric": "cpu_cores",
        "unit": "cores",
        "allocation_id": f"{CMD}.1",
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
    assert gpu["available_points"] == 0
    assert gpu["last"] is None and gpu["mean"] is None
    assert result["warnings"] == client.warnings
    assert "not zero use" in result["advisory"]
    assert "samples" not in cpu and "samples_omitted" not in result


def test_ended_task_uses_window_before_end_and_never_before_start(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD,
        "start_time": "2027-01-15T06:58:00.9Z",
        "end_time": "2027-01-15T07:00:00.999999999+00:00",
        "allocations": [],
    }

    result = service.usage(task["kind"], task["id"], window_seconds=604800)

    end = NOW - 3600
    assert result["window"]["start"] == end - 120
    assert result["window"]["end"] == end
    assert result["window"]["step"] == 15
    assert result["explanation"].startswith("No measurements were returned")


def test_week_window_step_respects_point_limit(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": "2020-01-01T00:00:00Z",
        "end_time": None, "allocations": [],
    }

    window = service.usage(task["kind"], task["id"], window_seconds=604800)["window"]

    assert window["end"] - window["start"] == 604800
    assert window["step"] == 421
    assert window["expected_points"] <= 1440


def test_disabled_monitoring_stops_before_task_reads(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.enabled = False

    with pytest.raises(APIError) as caught:
        service.usage(task["kind"], task["id"])

    assert caught.value.code == "task_resources_disabled"
    assert caught.value.retryable is False
    assert client.calls == [("entity", "command", CMD), ("capability",)]


def test_allocation_must_belong_to_selected_task(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)

    with pytest.raises(NotFoundError) as caught:
        service.usage(task["kind"], task["id"], allocation_id="other.1")
    assert caught.value.code == "allocation_not_found"
    assert not any(call[0] == "resources" for call in client.calls)

    service.usage(task["kind"], task["id"], allocation_id=f"{CMD}.1")
    assert resources_call(client)[-1] == f"{CMD}.1"


def test_metric_filter_and_bounded_samples(tmp_path, profile, monkeypatch):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("cpu_cores", [[NOW, 1.0]]),
        series("memory_rss_bytes", [[NOW, 0], [NOW - 15, 0]]),
    ]

    filtered = service.usage(
        task["kind"], task["id"], metrics=["memory_rss_bytes"], include_samples=True
    )
    assert [item["metric"] for item in filtered["series"]] == ["memory_rss_bytes"]
    assert filtered["samples_omitted"] is False
    assert filtered["series"][0]["samples"] == [[NOW, 0], [NOW - 15, 0]]
    assert filtered["series"][0]["first_at"] == "2027-01-15T07:59:45+00:00"
    assert filtered["series"][0]["last"] == 0

    monkeypatch.setattr(service_module, "_USAGE_MAX_RETURNED_SAMPLES", 2)
    bounded = service.usage(task["kind"], task["id"], include_samples=True)
    assert bounded["samples_omitted"] is True
    assert bounded["samples_limit"] == 2
    assert all("samples" not in item for item in bounded["series"])


def test_experiment_reports_latest_trial_task(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {
        "trial": {"id": 17, "experimentId": 9, "state": "RUNNING",
                  "taskIds": ["9.aaaa", "9.aaaa-1"]},
        "total": 3,
    }

    result = service.usage(task["kind"], task["id"])

    assert client.calls == [
        ("entity", "experiment", "9"),
        ("capability",),
        ("latest_trial", "9"),
        ("task_info", "9.aaaa-1"),
        ("resources", "9.aaaa-1", NOW - 3600, NOW, 15, None),
    ] + context_calls(["9.aaaa-1.1"])
    assert (result["kind"], result["id"]) == ("experiment", 9)
    assert result["determined_task_id"] == "9.aaaa-1"
    assert result["trial"] == {
        "id": 17,
        "state": "RUNNING",
        "selection": "latest",
        "experiment_trial_count": 3,
        "task_count": 2,
        "total_batches_processed": None,
        "wall_clock_seconds": None,
        "restarts": None,
        "batches_per_second_lower_bound": None,
        "summary_metrics": {},
        "summary_metrics_truncated": False,
    }
    assert "has 3 trials" in result["explanation"]
    assert "reports trial 17" in result["explanation"]


def test_experiment_requested_trial_must_belong_to_experiment(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.trials["4"] = {"id": 4, "experimentId": 9, "taskId": "9.bbbb"}
    client.trials["5"] = {"id": 5, "experimentId": 10, "taskIds": ["10.cccc"]}

    result = service.usage(task["kind"], task["id"], trial_id=4)
    assert client.calls == [
        ("entity", "experiment", "9"),
        ("capability",),
        ("trial", "4"),
        ("task_info", "9.bbbb"),
        ("resources", "9.bbbb", NOW - 3600, NOW, 15, None),
    ] + context_calls(["9.bbbb.1"])
    assert result["determined_task_id"] == "9.bbbb"
    assert result["trial"]["selection"] == "requested"
    assert result["trial"]["experiment_trial_count"] is None
    assert "pass trial_id" not in result["explanation"]

    client.calls.clear()
    with pytest.raises(NotFoundError) as caught:
        service.usage(task["kind"], task["id"], trial_id=5)
    assert caught.value.code == "trial_not_found"
    assert client.calls == [("entity", "experiment", "9"), ("capability",), ("trial", "5")]


def test_experiment_without_trials_is_not_started(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")

    with pytest.raises(ConflictError) as caught:
        service.usage(task["kind"], task["id"])

    assert caught.value.code == "task_not_started"


def test_trial_id_requires_experiment(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)

    with pytest.raises(ValidationError, match="experiment"):
        service.usage(task["kind"], task["id"], trial_id=1)
    assert client.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"window_seconds": 59},
        {"window_seconds": 604801},
        {"window_seconds": True},
        {"allocation_id": " a.1"},
        {"allocation_id": ""},
        {"metrics": "cpu_cores"},
        {"metrics": []},
        {"metrics": ["cpu_cores", "disk_bytes"]},
        {"include_samples": "yes"},
    ],
)
def test_invalid_arguments_fail_before_any_access(tmp_path, profile, arguments):
    service, client, task = launched(tmp_path, profile)

    with pytest.raises(ValidationError):
        service.usage(task["kind"], task["id"], **arguments)
    assert client.calls == []


def test_single_trial_experiment_omits_trial_hint(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {"trial": {"id": 17, "experimentId": 9, "taskIds": ["9.a"]}, "total": 1}

    result = service.usage(task["kind"], task["id"])

    assert result["trial"]["experiment_trial_count"] == 1
    assert "pass trial_id" not in result["explanation"]


def test_experiment_allocation_checked_against_trial_task(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {"trial": {"id": 17, "experimentId": 9, "taskIds": ["9.a", "9.a-1"]}, "total": 1}

    service.usage(task["kind"], task["id"], allocation_id="9.a-1.1")
    assert resources_call(client) == ("resources", "9.a-1", NOW - 3600, NOW, 15, "9.a-1.1")

    client.calls.clear()
    with pytest.raises(NotFoundError) as caught:
        service.usage(task["kind"], task["id"], allocation_id="9.1")
    assert caught.value.code == "allocation_not_found"
    assert not any(call[0] == "resources" for call in client.calls)


@pytest.mark.parametrize("trial_id", [0, -1, True, "abc", 1.5])
def test_invalid_trial_id_rejected_before_access_for_experiment(tmp_path, profile, trial_id):
    service, client, task = launched(tmp_path, profile, kind="experiment")

    with pytest.raises(ValidationError, match="positive integer"):
        service.usage(task["kind"], task["id"], trial_id=trial_id)
    assert client.calls == []


def test_ended_task_longer_than_window_anchors_window_at_task_end(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD,
        "start_time": TASK_START,
        "end_time": "2027-01-15T06:00:00Z",
        "allocations": [],
    }

    result = service.usage(task["kind"], task["id"])

    assert result["window"] == {
        "start": NOW - 10800,
        "end": NOW - 7200,
        "step": 15,
        "start_at": "2027-01-15T05:00:00+00:00",
        "end_at": "2027-01-15T06:00:00+00:00",
        "anchor": "task_end",
        "expected_points": 241,
    }
    assert resources_call(client) == ("resources", CMD, NOW - 10800, NOW - 7200, 15, None)


def allocation(allocation_id, start_time, end_time):
    return {"allocation_id": allocation_id, "state": "STATE_TERMINATED", "is_ready": False,
            "start_time": start_time, "end_time": end_time}


def test_paused_trial_window_ends_at_last_allocation_end(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {"trial": {"id": 3, "experimentId": 9, "state": "PAUSED",
                               "taskIds": ["9.p"]}, "total": 1}
    # Pausing leaves tasks.end_time NULL while every allocation has ended.
    client.task_info["9.p"] = {
        "task_id": "9.p", "start_time": TASK_START, "end_time": None,
        "allocations": [
            allocation("9.p.1", "2027-01-15T01:00:00.5", "2027-01-15T02:00:00"),
            allocation("9.p.2", "2027-01-15T02:30:00", "2027-01-15T05:00:00.1"),
        ],
    }

    result = service.usage(task["kind"], task["id"])

    end = NOW - 3 * 3600
    assert result["window"]["end"] == end
    assert result["window"]["start"] == end - 3600
    assert result["window"]["anchor"] == "allocation_end"
    assert "no selected allocation is running" in result["explanation"]
    assert [item["end_time"] for item in result["allocations"]] == [
        "2027-01-15T02:00:00Z", "2027-01-15T05:00:00.1Z",
    ]
    assert result["allocations"][0]["start_time"] == "2027-01-15T01:00:00.5Z"


def test_paused_then_cancelled_trial_uses_allocation_end_before_task_end(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": "2027-01-15T07:00:00Z",
        "allocations": [allocation(f"{CMD}.1", "2027-01-15T03:00:00", "2027-01-15T04:00:00")],
    }

    window = service.usage(task["kind"], task["id"])["window"]

    assert (window["start"], window["end"]) == (NOW - 5 * 3600, NOW - 4 * 3600)
    assert window["anchor"] == "allocation_end"


def test_requested_finished_allocation_of_running_task_uses_its_lifetime(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": None,
        "allocations": [
            allocation(f"{CMD}.1", "2027-01-15T02:30:00", "2027-01-15T03:00:00"),
            {**allocation(f"{CMD}.2", "2027-01-15T03:00:05", None), "state": "STATE_RUNNING"},
        ],
    }

    first = service.usage(
        task["kind"], task["id"], window_seconds=86400, allocation_id=f"{CMD}.1"
    )["window"]
    assert (first["start"], first["end"]) == (NOW - 5 * 3600 - 1800, NOW - 5 * 3600)
    assert first["anchor"] == "allocation_end"
    assert "no selected allocation is running" in service.usage(
        task["kind"], task["id"], allocation_id=f"{CMD}.1"
    )["explanation"]

    running = service.usage(task["kind"], task["id"])["window"]
    assert (running["end"], running["anchor"]) == (NOW, "now")


def test_task_without_allocations_or_with_unparsable_allocation_time_uses_now(
    tmp_path, profile
):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": None, "allocations": [],
    }
    assert service.usage(task["kind"], task["id"])["window"]["end"] == NOW

    client.task_info[CMD]["allocations"] = [allocation(f"{CMD}.1", "later", "soon")]
    result = service.usage(task["kind"], task["id"])
    assert (result["window"]["end"], result["window"]["anchor"]) == (NOW, "now")
    assert result["allocations"][0]["end_time"] == "soon"


@pytest.mark.parametrize(
    ("start_time", "end_time", "expected_end"),
    [
        ("2027-01-15T08:00:05Z", None, NOW),  # master clock ahead of the client
        ("2027-01-15T07:00:00Z", "2027-01-15T07:00:00.5Z", NOW - 3600),  # same second
    ],
)
def test_window_is_never_empty_or_inverted(
    tmp_path, profile, start_time, end_time, expected_end
):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": start_time, "end_time": end_time, "allocations": [],
    }

    window = service.usage(task["kind"], task["id"])["window"]

    assert (window["start"], window["end"]) == (expected_end - 1, expected_end)
    assert window["step"] == 15 and window["expected_points"] == 1
    assert resources_call(client) == (
        "resources", CMD, expected_end - 1, expected_end, 15, None
    )


def test_sample_limit_counts_only_selected_metrics_and_keeps_nulls(
    tmp_path, profile, monkeypatch
):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("cpu_cores", [[NOW - 15, None], [NOW, 1.0]]),
        series("gpu_power_watts", [[NOW - i * 15, 50.0] for i in range(5)], gpu="GPU-1"),
    ]
    monkeypatch.setattr(service_module, "_USAGE_MAX_RETURNED_SAMPLES", 2)

    selected = service.usage(
        task["kind"], task["id"], metrics=["cpu_cores"], include_samples=True
    )
    assert selected["samples_omitted"] is False
    assert "samples_limit" not in selected
    assert selected["series"][0]["samples"] == [[NOW - 15, None], [NOW, 1.0]]

    everything = service.usage(task["kind"], task["id"], include_samples=True)
    assert everything["samples_omitted"] is True
    assert everything["samples_limit"] == 2
    assert all("samples" not in item for item in everything["series"])


def test_filter_that_removes_every_series_names_the_returned_metrics(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("allocation_active", [[NOW, 1.0]]),
        series("cpu_cores", [[NOW, 0.5]]),
    ]

    result = service.usage(
        task["kind"], task["id"], metrics=["gpu_utilization_percent"]
    )

    assert result["series"] == []
    assert not result["explanation"].startswith("No measurements were returned")
    assert "allocation_active, cpu_cores" in result["explanation"]


MAPPING_DELAY = (
    "Determined attributes measurements to a task only after its allocation has run for the "
    "master's task-mapping delay (5 minutes by default), so the first minutes of each "
    "allocation, and any allocation that ended sooner, have no data; a longer window does "
    "not help."
)


def test_no_measurements_name_the_task_mapping_delay(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)

    result = service.usage(task["kind"], task["id"])

    assert result["series"] == []
    assert result["explanation"] == (
        "No measurements were returned; the task may not have run in this window, or "
        "monitoring retained no data for it. " + MAPPING_DELAY
    )
    assert result["advisory"].endswith(" " + MAPPING_DELAY)


def test_the_task_mapping_delay_ends_an_explanation_with_other_clauses(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": "2027-01-15T07:00:00Z",
        "allocations": [allocation(f"{CMD}.1", "2027-01-15T03:00:00", "2027-01-15T04:00:00")],
    }

    explanation = service.usage(task["kind"], task["id"])["explanation"]

    assert "no selected allocation is running. " + MAPPING_DELAY in explanation
    assert explanation.endswith(MAPPING_DELAY)
    assert explanation.count(MAPPING_DELAY) == 1


@pytest.mark.parametrize(
    ("returned", "metrics"),
    [
        # Measured series: the task was mapped.
        ([series("cpu_cores", [[NOW, 0.5]])], None),
        # A metrics filter hid every returned series, which still shows the task was mapped.
        ([series("cpu_cores", [[NOW, 0.5]])], ["gpu_utilization_percent"]),
    ],
    ids=["measured", "filtered"],
)
def test_returned_measurements_do_not_name_the_delay(tmp_path, profile, returned, metrics):
    service, client, task = launched(tmp_path, profile)
    client.series = returned

    result = service.usage(task["kind"], task["id"], metrics=metrics)

    assert MAPPING_DELAY not in result["explanation"]
    assert MAPPING_DELAY in result["advisory"]


def test_experiment_usage_through_real_client_wire_format(tmp_path, profile, monkeypatch):
    import json

    import requests

    from determined_compute.core.api_client import DeterminedAPIClient

    class Response:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status
            self.text = json.dumps(payload)
            self.content = self.text.encode()

        def json(self):
            return self.payload

    requested = []
    routes = {
        "/api/v1/me": {"user": {"id": 7, "username": "alice"}},
        "/api/v1/task-resources/capability": {"enabled": True},
        "/api/v1/experiments/9/trials": {
            "trials": [{"id": 17, "experimentId": 9, "state": "STATE_ACTIVE",
                        "taskId": "9.abc", "taskIds": ["9.abc", "9.abc-1"]}],
            "pagination": {"offset": 0, "limit": 1, "startIndex": 0, "endIndex": 1, "total": 2},
        },
        "/api/v1/tasks/9.abc-1": {"task": {
            "taskId": "9.abc-1", "taskType": "TASK_TYPE_TRIAL",
            "startTime": "2027-01-15T06:00:00.123456789Z",
            "allocations": [{"taskId": "9.abc-1", "allocationId": "9.abc-1.1",
                             "state": "STATE_RUNNING", "isReady": True,
                             "startTime": "2027-01-15T06:00:03.25", "slots": 1}],
        }},
        "/api/v1/experiments/9": {
            "experiment": {"id": 9, "userId": 7, "state": "STATE_ACTIVE",
                           "resourcePool": "gpu-a"},
            "config": {"resources": {"slots_per_trial": 2}},
        },
        "/api/v1/resource-pools": {"resourcePools": [
            {"name": "gpu-a", "description": "Operator text"}, {"name": "cpu", "description": ""},
        ]},
        "/api/v1/allocations/9.abc-1.1": {"allocation": {
            "taskId": "9.abc-1", "allocationId": "9.abc-1.1", "state": "STATE_RUNNING",
            "slots": 2, "startTime": "2027-01-15 06:00:03.25 +0000 UTC",
        }},
        "/api/v1/agents": {"agents": [{
            "id": "node-a",
            "slots": {
                "0": {"id": "0", "device": {"id": 0, "brand": "Model X", "uuid": "GPU-1",
                                            "type": "TYPE_CUDA"}},
                "1": {"id": "1", "device": {"id": -1, "brand": "Model X", "uuid": "********",
                                            "type": "TYPE_CUDA"}},
            },
        }]},
        "/api/v1/tasks/9.abc-1/resources": {
            "enabled": True,
            "series": [{
                "metric": "gpu_utilization_percent",
                "labels": {"allocationId": "9.abc-1.1", "node": "", "gpuUuid": "GPU-1"},
                "samples": [{"timestampSeconds": NOW - 15, "value": 80},
                            {"timestampSeconds": NOW, "value": None}],
            }],
            "warnings": [{"code": "gpu_full_device", "message": "whole device"}],
        },
    }

    def get(url, **kwargs):
        path = url.removeprefix("https://det.example.test")
        requested.append((path, kwargs.get("params")))
        return Response(routes[path])

    def post(url, **kwargs):
        return Response({"experiment": {"id": 9, "state": "STATE_ACTIVE"}})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    client = DeterminedAPIClient("https://det.example.test", api_token="token")
    service = ComputeService(client, profile)
    task = service.launch(
        {"kind": "experiment", "name": "wire", "command": "python train.py",
         "workdir": "/shared/container/code", "output_dir": "/shared/container/out",
         "allow_queue": True},
    )
    assert task["id"] == 9

    result = service.usage(task["kind"], task["id"], allocation_id="9.abc-1.1")

    assert [path for path, _params in requested] == [
        "/api/v1/me",
        "/api/v1/experiments/9",
        "/api/v1/task-resources/capability",
        "/api/v1/experiments/9/trials",
        "/api/v1/tasks/9.abc-1",
        "/api/v1/tasks/9.abc-1/resources",
        "/api/v1/resource-pools",
        "/api/v1/allocations/9.abc-1.1",
        "/api/v1/agents",
    ]
    assert requested[5][1] == {
        "start": NOW - 3600, "end": NOW, "step": 15, "allocationId": "9.abc-1.1",
    }
    assert result["resource_pool"] == {"name": "gpu-a", "description": "Operator text"}
    assert result["allocations"][0]["slots"] == 2
    assert result["allocations"][0]["exit_reason"] is None
    assert result["gpus"][0]["gpu_models"] == ["Model X"]
    assert result["gpus"][0]["requested_slots"] == 2
    assert result["context_unavailable"] == []
    assert requested[3][1] == {"sortBy": "SORT_BY_ID", "orderBy": "ORDER_BY_DESC", "limit": 1}
    assert result["determined_task_id"] == "9.abc-1"
    assert result["trial"]["experiment_trial_count"] == 2
    assert result["allocations"][0]["start_time"] == "2027-01-15T06:00:03.25Z"
    (gpu,) = result["series"]
    assert (gpu["gpu_uuid"], gpu["node"], gpu["last"], gpu["available_points"]) == (
        "GPU-1", None, 80, 1,
    )
    assert gpu["gpu_model"] == "Model X"
    assert result["warnings"] == [{"code": "gpu_full_device", "message": "whole device"}]


def test_gpu_comparison_uses_every_returned_gpu_series(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("cpu_cores", [[NOW, 2.0]]),
        series("gpu_utilization_percent", [[NOW - 15, 100], [NOW, 80]], gpu="GPU-A"),
        series("gpu_utilization_percent", [[NOW - 30, 0], [NOW - 15, 20], [NOW, 10]],
               gpu="GPU-B"),
        series("gpu_memory_used_bytes", [[NOW, 4e9], [NOW - 15, None]], gpu="GPU-A"),
        series("gpu_memory_used_bytes", [[NOW, 6e9]], gpu="GPU-B"),
        series("gpu_utilization_percent", [[NOW, 50]], allocation=f"{CMD}.0", gpu="GPU-C"),
    ]
    client.gpu_models = {"GPU-A": "Model X", "GPU-C": "Model Z"}
    client.allocation_details[f"{CMD}.1"] = {
        "allocation_id": f"{CMD}.1", "slots": 2, "exit_reason": None, "status_code": None,
    }

    result = service.usage(task["kind"], task["id"], metrics=["cpu_cores"])

    assert [item["metric"] for item in result["series"]] == ["cpu_cores"]
    assert result["gpus"] == [
        {
            "allocation_id": f"{CMD}.0",
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
            "allocation_id": f"{CMD}.1",
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

    gpu_series = service.usage(
        task["kind"], task["id"], metrics=["gpu_utilization_percent"]
    )["series"]
    by_gpu = {item["gpu_uuid"]: item for item in gpu_series}
    assert by_gpu["GPU-A"]["gpu_model"] == "Model X"
    assert by_gpu["GPU-B"]["gpu_model"] is None
    assert by_gpu["GPU-B"]["idle_fraction"] == pytest.approx(1 / 3, abs=1e-6)
    assert (by_gpu["GPU-B"]["p50"], by_gpu["GPU-B"]["p95"]) == (10, 20)


def test_equal_gpu_means_pick_the_first_uuid(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("gpu_utilization_percent", [[NOW, 30]], gpu="GPU-Z"),
        series("gpu_utilization_percent", [[NOW, 30]], gpu="GPU-M"),
    ]

    (gpus,) = service.usage(task["kind"], task["id"])["gpus"]

    assert gpus["least_utilized_gpu_uuid"] == "GPU-M"


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [([4, 1, 3, 2], 2, 4), ([5, 1, 4, 2, 3], 3, 5), (list(range(1, 21)), 10, 19)],
)
def test_percentiles_are_nearest_rank(tmp_path, profile, values, p50, p95):
    service, client, task = launched(tmp_path, profile)
    client.series = [series("cpu_cores", [[NOW - index, value] for index, value in enumerate(values)])]

    (summary,) = service.usage(task["kind"], task["id"])["series"]

    assert (summary["p50"], summary["p95"]) == (p50, p95)
    assert "idle_fraction" not in summary


@pytest.mark.parametrize(
    ("failing", "name"),
    [("pools", "resource_pool"), ("allocation", "allocation_details"),
     ("agents", "gpu_models")],
)
def test_failed_context_lookup_keeps_measurements(tmp_path, profile, failing, name):
    service, client, task = launched(tmp_path, profile)
    client.series = [series("gpu_utilization_percent", [[NOW, 70]], gpu="GPU-A")]
    client.gpu_models = {"GPU-A": "Model X"}
    client.failing = {failing}

    result = service.usage(task["kind"], task["id"])

    assert result["context_unavailable"] == [name]
    assert result["series"][0]["last"] == 70
    if failing == "pools":
        assert result["resource_pool"] == {"name": "gpu", "description": None}
    if failing == "allocation":
        assert result["allocations"][0]["slots"] is None
        assert result["gpus"][0]["requested_slots"] is None
    if failing == "agents":
        assert result["series"][0]["gpu_model"] is None


def test_non_api_errors_from_context_lookups_are_not_hidden(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)

    def broken():
        raise RuntimeError("bug")

    client.list_resource_pools = broken
    with pytest.raises(RuntimeError):
        service.usage(task["kind"], task["id"])


def test_pool_context_and_agent_lookup_only_when_needed(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [series("cpu_cores", [[NOW, 1.0]])]

    result = service.usage(task["kind"], task["id"])

    assert result["resource_pool"] == {"name": "gpu", "description": "Shared GPU agents"}
    assert ("agents",) not in client.calls
    assert result["gpus"] == []

    client.get_task = lambda kind, remote_id: {"id": remote_id, "userId": 7}
    client.calls.clear()
    assert service.usage(task["kind"], task["id"])["resource_pool"] is None
    assert ("pools",) not in client.calls


def test_allocation_details_are_bounded_but_include_the_requested_allocation(
    tmp_path, profile
):
    service, client, task = launched(tmp_path, profile)
    names = [f"{CMD}.{index}" for index in range(10, 0, -1)]
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": None,
        "allocations": [allocation(name, None, None) for name in names],
    }

    result = service.usage(task["kind"], task["id"], allocation_id=f"{CMD}.1")

    detailed = [call[1] for call in client.calls if call[0] == "allocation"]
    assert detailed == names[:8] + [f"{CMD}.1"]
    assert result["allocation_details_limit"] == 8
    slots = {item["allocation_id"]: item["slots"] for item in result["allocations"]}
    assert slots[f"{CMD}.1"] == 1 and slots[f"{CMD}.2"] is None

    client.task_info[CMD]["allocations"] = client.task_info[CMD]["allocations"][:8]
    assert "allocation_details_limit" not in service.usage(task["kind"], task["id"])


def test_trial_throughput_context(tmp_path, profile):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    trial = {"id": 17, "experimentId": 9, "taskIds": ["9.a"], "totalBatchesProcessed": 1000,
             "wallClockTime": 500.0, "restarts": 1}
    client.latest = {"trial": trial, "total": 1}

    context = service.usage(task["kind"], task["id"])["trial"]
    assert (context["total_batches_processed"], context["wall_clock_seconds"]) == (1000, 500.0)
    assert (context["restarts"], context["batches_per_second_lower_bound"]) == (1, 2.0)

    trial.update(totalBatchesProcessed=0, wallClockTime=0)
    result = service.usage(task["kind"], task["id"])
    assert result["trial"]["batches_per_second_lower_bound"] is None
    assert "Core API" in result["explanation"]

    trial.update(totalBatchesProcessed=True, wallClockTime="500", restarts=-1)
    context = service.usage(task["kind"], task["id"])["trial"]
    assert (context["total_batches_processed"], context["wall_clock_seconds"]) == (None, None)
    assert context["restarts"] is None


def test_summary_metrics_keep_numeric_statistics_and_report_truncation(
    tmp_path, profile, monkeypatch
):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {"trial": {
        "id": 17, "experimentId": 9, "taskIds": ["9.a"],
        "summaryMetrics": {
            "avg_metrics": {
                "loss": {"type": "number", "count": 10, "min": 0.1, "max": 2.0, "last": 0.2,
                         "sum": 5.0, "mean": 0.5, "extra": "drop"},
                "note": {"type": "string", "last": "user text"},
            },
            "perf": {"step_s": {"type": "number", "last": 0.3, "min": True}},
            "broken": ["not", "a", "map"],
        },
    }, "total": 1}

    context = service.usage(task["kind"], task["id"])["trial"]
    assert context["summary_metrics"] == {
        "avg_metrics": {
            "loss": {"type": "number", "count": 10, "sum": 5.0, "min": 0.1, "max": 2.0,
                     "last": 0.2, "mean": 0.5},
            "note": {"type": "string"},
        },
        "perf": {"step_s": {"type": "number", "last": 0.3}},
    }
    assert context["summary_metrics_truncated"] is False

    monkeypatch.setattr(service_module, "_USAGE_MAX_SUMMARY_METRICS", 2)
    context = service.usage(task["kind"], task["id"])["trial"]
    assert context["summary_metrics"] == {
        "avg_metrics": {
            "loss": {"type": "number", "count": 10, "sum": 5.0, "min": 0.1, "max": 2.0,
                     "last": 0.2, "mean": 0.5},
            "note": {"type": "string"},
        },
    }
    assert context["summary_metrics_truncated"] is True


def test_transport_failure_skips_remaining_context_lookups(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [series("gpu_utilization_percent", [[NOW, 70]], gpu="GPU-A")]
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": None,
        "allocations": [allocation(f"{CMD}.{index}", None, None) for index in (3, 2, 1)],
    }

    def timeout(*args):
        client.calls.append(("timeout",))
        raise APIError("Determined request failed", code="transport_error", retryable=True)

    client.list_resource_pools = timeout
    result = service.usage(task["kind"], task["id"])

    assert client.calls[client.calls.index(resources_call(client)) + 1:] == [("timeout",)]
    assert result["context_unavailable"] == ["resource_pool", "allocation_details", "gpu_models"]
    assert result["series"][0]["last"] == 70

    del client.list_resource_pools
    client.failing = {"pools"}
    client.calls.clear()
    assert service.usage(task["kind"], task["id"])["context_unavailable"] == ["resource_pool"]
    assert [call[0] for call in client.calls].count("allocation") == 3


@pytest.mark.parametrize("code", [404, 503])
def test_task_the_master_no_longer_serves_cannot_be_measured(tmp_path, profile, code):
    # Determined keeps an ended command or shell for only 24 hours and drops it on a
    # master restart; without the task its owner cannot be verified.
    service, client, task = launched(tmp_path, profile)

    def missing(kind, remote_id):
        client.calls.append(("entity", kind, remote_id))
        raise APIError(f"{code} command not found", code=code, retryable=code >= 500)

    client.get_task = missing
    with pytest.raises(APIError) as caught:
        service.usage(task["kind"], task["id"])

    assert caught.value.code == code
    assert client.calls == [("entity", "command", CMD)]


def test_validation_metrics_survive_the_summary_cap(tmp_path, profile, monkeypatch):
    service, client, task = launched(tmp_path, profile, kind="experiment")
    client.latest = {"trial": {
        "id": 17, "experimentId": 9, "taskIds": ["9.a"],
        "summaryMetrics": {
            "avg_metrics": {f"grad_{index:03}": {"type": "number", "last": 1.0}
                            for index in range(5)},
            "inference": {"latency": {"type": "number", "last": 2.0}},
            "validation_metrics": {"loss": {"type": "number", "last": 0.5}},
        },
    }, "total": 1}
    monkeypatch.setattr(service_module, "_USAGE_MAX_SUMMARY_METRICS", 3)

    context = service.usage(task["kind"], task["id"])["trial"]

    assert list(context["summary_metrics"]) == ["validation_metrics", "avg_metrics"]
    assert context["summary_metrics"]["validation_metrics"]["loss"]["last"] == 0.5
    assert list(context["summary_metrics"]["avg_metrics"]) == ["grad_000", "grad_001"]
    assert context["summary_metrics_truncated"] is True


def test_null_gpu_samples_are_unavailable_not_idle(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("gpu_utilization_percent", [[NOW - 15, 50], [NOW, None]], gpu="GPU-A"),
        series("gpu_utilization_percent", [[NOW - 15, None], [NOW, None]], gpu="GPU-B"),
        series("gpu_memory_used_bytes", [[NOW, 1e9]], gpu="GPU-B"),
    ]

    result = service.usage(task["kind"], task["id"])

    by_gpu = {
        item["gpu_uuid"]: item for item in result["series"]
        if item["metric"] == "gpu_utilization_percent"
    }
    assert by_gpu["GPU-A"]["idle_fraction"] == 0.0
    assert by_gpu["GPU-B"]["idle_fraction"] is None
    (gpus,) = result["gpus"]
    assert gpus["gpu_count"] == 2
    assert gpus["mean_utilization_percent"] == 50.0
    assert gpus["min_gpu_mean_utilization_percent"] == 50.0
    assert gpus["least_utilized_gpu_uuid"] == "GPU-A"
    assert gpus["idle_fraction"] == 0.0
    assert gpus["max_memory_used_bytes"] == 1e9


def test_idle_threshold_is_ten_percent(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [series(
        "gpu_utilization_percent",
        [[NOW - 60, 0], [NOW - 45, 5], [NOW - 30, 9.9], [NOW - 15, 10], [NOW, 50]],
        gpu="GPU-A",
    )]

    result = service.usage(task["kind"], task["id"])

    assert result["series"][0]["idle_fraction"] == 0.6
    assert result["gpus"][0]["idle_fraction"] == 0.6


def test_pool_description_matches_task_pool_by_name(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.pools = [{"name": "cpu", "description": "CPU only"},
                    {"name": "gpu", "description": "Shared GPU agents"}]
    assert service.usage(task["kind"], task["id"])["resource_pool"] == {
        "name": "gpu", "description": "Shared GPU agents",
    }

    client.pools = [{"name": "cpu", "description": "CPU only"}]
    assert service.usage(task["kind"], task["id"])["resource_pool"] == {
        "name": "gpu", "description": None,
    }


def test_allocation_exit_details_are_reported(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.allocation_details[f"{CMD}.1"] = {
        "allocation_id": f"{CMD}.1", "slots": 2, "exit_reason": "OOM killed", "status_code": 137,
    }

    (item,) = service.usage(task["kind"], task["id"])["allocations"]

    assert (item["slots"], item["exit_reason"], item["status_code"]) == (2, "OOM killed", 137)


def test_repeated_context_failures_are_reported_once(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.task_info[CMD] = {
        "task_id": CMD, "start_time": TASK_START, "end_time": None,
        "allocations": [allocation(f"{CMD}.{index}", None, None) for index in (3, 2, 1)],
    }
    client.failing = {"allocation"}

    result = service.usage(task["kind"], task["id"])

    assert result["context_unavailable"] == ["allocation_details"]
    assert sum(call[0] == "allocation" for call in client.calls) == 3


def test_gpu_series_without_allocation_label(tmp_path, profile):
    service, client, task = launched(tmp_path, profile)
    client.series = [
        series("gpu_utilization_percent", [[NOW, 40]], allocation=None, gpu="GPU-A"),
        series("gpu_utilization_percent", [[NOW, 60]], gpu="GPU-B"),
    ]

    gpus = service.usage(task["kind"], task["id"])["gpus"]

    assert [item["allocation_id"] for item in gpus] == [None, f"{CMD}.1"]
    assert gpus[0]["requested_slots"] is None
