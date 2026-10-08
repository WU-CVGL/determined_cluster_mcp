"""compute_status reports where an unended task's job stands in its pool's queue."""

from __future__ import annotations

import copy
import json

import pytest
import requests

from determined_compute.compute import (
    APIError,
    ComputeProfile,
    ComputeService,
    ConflictError,
)
from determined_compute.core.api_client import DeterminedAPIClient


COMMAND_ID = "12345678-1234-5678-9234-567812345678"
GENERIC_ID = "3f1c2b8e-1111-4c7a-9d55-0123456789ab"


def queued(job_id="job-7", **overrides):
    job = {
        "job_id": job_id, "resource_pool": "gpu", "state": "STATE_SCHEDULED",
        "jobs_ahead": 0, "requested_slots": 2, "allocated_slots": 2,
        "placement": [{"agent_id": "agent-1", "device_ids": [1, 2]}],
    }
    job.update(overrides)
    return job


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self, entity, jobs=(), total=None, error=None):
        self.entity = entity
        self.jobs = list(jobs)
        self.total = len(self.jobs) if total is None else total
        self.error = error
        self.calls = []

    def get_current_user(self):
        self.calls.append(("GET", "api/v1/me"))
        return {"id": "7", "username": "alice"}

    def get_task(self, kind, remote_id):
        self.calls.append(("GET", f"api/v1/{kind}s/{remote_id}"))
        return copy.deepcopy(self.entity)

    def job_queue(self, pool, limit):
        self.calls.append(("GET", "api/v1/job-queues-v2", {"resourcePool": pool, "limit": limit}))
        if self.error is not None:
            raise self.error
        return {"jobs": copy.deepcopy(self.jobs), "total": self.total}


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [{"host_path": "/shared/host", "container_path": "/shared/container"}],
            "defaults": {"image": "image:stable", "pool": "gpu", "slots": 1},
        }
    )


def entity(**overrides):
    value = {
        "id": 11, "userId": 7, "username": "alice", "name": "train", "state": "STATE_ACTIVE",
        "resourcePool": "gpu", "jobId": "job-7", "startTime": "2026-10-01T00:00:00Z",
    }
    value.update(overrides)
    return value


def status(profile, client, kind="experiment", task_id=11):
    return ComputeService(client, profile).status(kind, task_id)


def queue_calls(client):
    return [call for call in client.calls if call[1] == "api/v1/job-queues-v2"]


def test_a_scheduled_job_reports_its_placement_after_the_ownership_check(profile):
    client = FakeClient(entity(), jobs=[queued("job-other"), queued()])

    result = status(profile, client)

    assert client.calls == [
        ("GET", "api/v1/me"),
        ("GET", "api/v1/experiments/11"),
        ("GET", "api/v1/job-queues-v2", {"resourcePool": "gpu", "limit": 1000}),
    ]
    assert result["queue"] == {
        "resource_pool": "gpu", "state": "STATE_SCHEDULED", "jobs_ahead": 0,
        "requested_slots": 2, "allocated_slots": 2,
        "placement": [{"agent_id": "agent-1", "device_ids": [1, 2]}],
    }
    assert result["context_unavailable"] == []
    assert "queue_note" not in result
    assert result["state"] == "STATE_ACTIVE"


def test_a_queued_job_reports_jobs_ahead_and_an_empty_placement(profile):
    client = FakeClient(
        entity(), jobs=[queued(state="STATE_QUEUED", jobs_ahead=3, allocated_slots=0, placement=[])]
    )

    queue = status(profile, client)["queue"]

    assert queue["state"] == "STATE_QUEUED"
    assert queue["jobs_ahead"] == 3
    assert queue["allocated_slots"] == 0
    assert queue["placement"] == []


def test_a_master_without_placement_gives_null(profile):
    client = FakeClient(entity(), jobs=[queued(placement=None)])

    assert status(profile, client)["queue"]["placement"] is None


def test_an_ended_task_makes_no_queue_request(profile):
    client = FakeClient(entity(state="STATE_COMPLETED", endTime="2026-10-02T00:00:00Z"))

    result = status(profile, client)

    assert queue_calls(client) == []
    assert result["queue"] is None
    assert result["context_unavailable"] == []
    assert "queue_note" not in result


def test_a_task_owned_by_another_user_never_reaches_the_queue(profile):
    client = FakeClient(entity(userId=8), jobs=[queued()])

    with pytest.raises(ConflictError):
        status(profile, client)

    assert queue_calls(client) == []


@pytest.mark.parametrize(
    ("overrides", "note"),
    [
        ({"resourcePool": ""}, "task reports no resource pool"),
        ({"resourcePool": None}, "task reports no resource pool"),
        ({"jobId": ""}, "task reports no job ID"),
        ({"jobId": None}, "task reports no job ID"),
    ],
)
def test_a_task_without_a_pool_or_job_id_sends_no_request(profile, overrides, note):
    client = FakeClient(entity(**overrides), jobs=[queued()])

    result = status(profile, client)

    assert queue_calls(client) == []
    assert result["queue"] is None
    assert result["queue_note"] == note
    assert result["context_unavailable"] == []


def test_a_job_missing_from_the_queue_gets_a_note(profile):
    client = FakeClient(entity(), jobs=[queued("job-other")])

    result = status(profile, client)

    assert result["queue"] is None
    assert result["queue_note"] == (
        "not in pool gpu's job queue: not yet queued, paused, or just ended"
    )
    assert result["context_unavailable"] == []


def test_a_job_beyond_the_first_page_is_not_reported_as_not_queued(profile):
    client = FakeClient(entity(), jobs=[queued("job-other")], total=1001)

    result = status(profile, client)

    assert result["queue"] is None
    assert result["queue_note"] == "not among the first 1000 jobs of pool gpu"


def test_a_pool_with_exactly_one_page_of_jobs_reports_not_queued(profile):
    client = FakeClient(entity(), jobs=[queued("job-other")], total=1000)

    result = status(profile, client)

    assert result["queue"] is None
    assert result["queue_note"] == (
        "not in pool gpu's job queue: not yet queued, paused, or just ended"
    )
    assert result["context_unavailable"] == []


@pytest.mark.parametrize(
    "error",
    [
        APIError("Determined request failed", code="transport_error", retryable=True),
        APIError("internal", code=500),
        APIError("Job-queue response is malformed", code="invalid_response"),
    ],
)
def test_a_failed_lookup_is_context_unavailable_and_keeps_the_state(profile, error):
    client = FakeClient(entity(), error=error)

    result = status(profile, client)

    assert result["queue"] is None
    assert result["context_unavailable"] == ["queue"]
    assert "queue_note" not in result
    assert result["state"] == "STATE_ACTIVE"
    assert len(queue_calls(client)) == 1


def test_a_job_listed_twice_is_context_unavailable(profile):
    client = FakeClient(entity(), jobs=[queued(), queued(jobs_ahead=1)])

    result = status(profile, client)

    assert result["queue"] is None
    assert result["context_unavailable"] == ["queue"]
    assert "queue_note" not in result


def test_a_command_uses_its_reported_job_id(profile):
    command = entity(
        id=COMMAND_ID, state="RUNNING", description="eval\nmore", jobId="job-c",
    )
    del command["name"]
    client = FakeClient(command, jobs=[queued("job-c", requested_slots=0, allocated_slots=0,
                                              placement=[])])

    result = status(profile, client, "command", COMMAND_ID)

    assert result["queue"]["placement"] == []
    assert queue_calls(client) == [
        ("GET", "api/v1/job-queues-v2", {"resourcePool": "gpu", "limit": 1000})
    ]


@pytest.mark.parametrize("kind", ["command", "shell"])
def test_a_terminated_command_or_shell_makes_no_queue_request(profile, kind):
    # Commands and shells report no end time; the master keeps them as STATE_TERMINATED.
    task = entity(id=COMMAND_ID, state="STATE_TERMINATED", description="eval", jobId="job-c")
    del task["name"]
    client = FakeClient(task, jobs=[queued("job-c")])

    result = status(profile, client, kind, COMMAND_ID)

    assert queue_calls(client) == []
    assert result["queue"] is None
    assert result["context_unavailable"] == []
    assert "queue_note" not in result


def test_a_terminating_command_still_reads_the_queue(profile):
    task = entity(id=COMMAND_ID, state="STATE_TERMINATING", description="eval", jobId="job-c")
    del task["name"]
    client = FakeClient(task, jobs=[queued("job-c")])

    result = status(profile, client, "command", COMMAND_ID)

    assert len(queue_calls(client)) == 1
    assert result["queue"]["state"] == "STATE_SCHEDULED"


def test_an_unranked_job_reports_null_jobs_ahead(profile):
    client = FakeClient(entity(), jobs=[queued(state="STATE_QUEUED", jobs_ahead=None)])

    queue = status(profile, client)["queue"]

    assert queue["state"] == "STATE_QUEUED"
    assert queue["jobs_ahead"] is None


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self.headers = {}

    def json(self):
        return self.payload


def test_a_generic_task_takes_its_job_id_and_pool_from_the_existing_list_call(
    monkeypatch, profile
):
    # The task record and a config without a pool; the list item names both.
    responses = {
        f"/api/v1/tasks/{GENERIC_ID}": {"task": {
            "taskId": GENERIC_ID, "taskType": "TASK_TYPE_GENERIC",
            "taskState": "GENERIC_TASK_STATE_ACTIVE", "startTime": "2026-10-01T00:00:00Z",
        }},
        f"/api/v1/tasks/{GENERIC_ID}/config": {"config": json.dumps({
            "name": "eval", "resources": {"slots": 1},
            "environment": {"image": "image:tag", "environment_variables": []},
        })},
        "/api/v1/generic-tasks": {
            "tasks": [{
                "taskId": GENERIC_ID, "jobId": "job-g", "userId": 7, "username": "alice",
                "name": "eval", "state": "GENERIC_TASK_STATE_ACTIVE", "resourcePool": "cpu",
            }],
            "pagination": {"limit": 0, "offset": 0, "startIndex": 0, "endIndex": 1, "total": 1},
        },
        "/api/v1/me": {"user": {"id": 7, "username": "alice"}},
        "/api/v1/job-queues-v2": {
            "jobs": [
                {"limited": {
                    "jobId": "job-g", "type": "TYPE_GENERIC", "resourcePool": "cpu",
                    "summary": {"state": "STATE_SCHEDULED", "jobsAhead": 9},
                    "requestedSlots": 5, "allocatedSlots": 5,
                }},
                {"full": {
                    "jobId": "job-g", "type": "TYPE_GENERIC", "resourcePool": "cpu",
                    "summary": {"state": "STATE_QUEUED", "jobsAhead": 2},
                    "requestedSlots": 0, "allocatedSlots": 0, "placement": [],
                }},
            ],
            "pagination": {"offset": 0, "limit": 1000, "startIndex": 0, "endIndex": 2,
                           "total": 2},
        },
    }
    requested = []

    def get(url, **kwargs):
        path = url.removeprefix("https://det.example.test")
        requested.append((path, kwargs.get("params")))
        return Response(responses[path])

    monkeypatch.setattr(requests, "get", get)
    client = DeterminedAPIClient(api_url="https://det.example.test", api_token="test-token")

    result = ComputeService(client, profile).status("generic", GENERIC_ID)

    assert [path for path, _params in requested] == [
        "/api/v1/me",
        f"/api/v1/tasks/{GENERIC_ID}",
        f"/api/v1/tasks/{GENERIC_ID}/config",
        "/api/v1/generic-tasks",
        "/api/v1/job-queues-v2",
    ]
    assert requested[-1][1] == {"resourcePool": "cpu", "limit": 1000}
    assert result["resource_pool"] == "cpu"
    # The limited entry for the same job is ignored.
    assert result["queue"] == {
        "resource_pool": "cpu", "state": "STATE_QUEUED", "jobs_ahead": 2,
        "requested_slots": 0, "allocated_slots": 0, "placement": [],
    }
