"""The client against a real master; see ``live.py`` for how to name one."""

import time
import uuid

import pytest

import live
from determined_compute import usage
from determined_compute.client import APIError

pytestmark = live.requires_master


@pytest.fixture
def live_client(monkeypatch):
    api = live.client(monkeypatch)
    launched = []
    yield api, launched
    for job_id in launched:
        api.cancel_submission(job_id)


def command(tag, text="echo live"):
    return {
        "description": f"client-live-{tag}",
        "entrypoint": ["sh", "-c", text],
        "resources": {"resource_pool": live.pool(), "slots": 0},
    }


def refused(code, operation, *args, **kwargs):
    with pytest.raises(APIError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code, caught.value
    return caught.value


def test_plan_launch_replay_and_cancel(live_client):
    api, launched = live_client
    tag = uuid.uuid4().hex[:12]
    assert api.check_protocol()["submission_protocol"] >= 1

    plan = api.submit("command", command(tag), dry_run=True)
    assert plan["job_id"] is None and plan["outcome"] is None
    assert plan["effective_config"]["resources"]["slots"] == 0
    assert api.submit("command", command(tag), dry_run=True)["request_digest"] == (
        plan["request_digest"]
    )

    key = f"client-live-{tag}"
    job = api.submit("command", command(tag), idempotency_key=key,
                     expected_digest=plan["request_digest"])
    launched.append(job["job_id"])
    assert (job["replayed"], job["outcome"]) == (False, "queued")
    replay = api.submit("command", command(tag), idempotency_key=key,
                        expected_digest=plan["request_digest"])
    assert (replay["job_id"], replay["replayed"]) == (job["job_id"], True)

    conflict = refused("key_conflict", api.submit, "command", command(tag, "echo changed"),
                       idempotency_key=key)
    assert conflict.details == {"job_id": job["job_id"]}
    changed = refused("plan_changed", api.submit, "command", command(tag, "echo changed"),
                      idempotency_key=f"{key}-2", expected_digest=plan["request_digest"])
    assert changed.details["expected_digest"] == plan["request_digest"]

    record = api.get_submission(job["job_id"])
    assert (record["kind"], record["idempotency_key"]) == ("command", key)
    assert record["request_digest"] == plan["request_digest"]
    listed = api.list_submissions(kind="command", limit=20)["submissions"]
    assert job["job_id"] in {item["job_id"] for item in listed}
    assert isinstance(api.task_logs(record["entity_id"], tail=5), list)
    measured = usage.summarize(api, record)
    assert measured["allocations"][0]["allocation_id"].startswith(record["entity_id"])
    assert measured["context_unavailable"] == []

    api.cancel_submission(job["job_id"])
    deadline = time.monotonic() + 30
    while api.get_submission(job["job_id"])["state"] != "canceled":
        assert time.monotonic() < deadline, "the cancel did not land"
        time.sleep(1)


def test_refusals_create_nothing(live_client):
    api, _ = live_client
    tag = uuid.uuid4().hex[:12]
    refused("admission_unsupported", api.submit, "command", command(tag), dry_run=True,
            admission="immediate")
    experiment = {
        "name": f"client-live-{tag}",
        "entrypoint": "echo live",
        "searcher": {"name": "single", "metric": "loss"},
        "max_restarts": 0,
        "resources": {"resource_pool": command(tag)["resources"]["resource_pool"],
                      "slots_per_trial": 0},
    }
    refused("admission_unsupported", api.submit, "experiment", experiment, dry_run=True,
            admission="immediate")
    broken = {**experiment, "searcher": {"name": "bogus"}}
    assert "searcher" in str(refused("invalid_request", api.submit, "experiment", broken,
                                     dry_run=True))
    refused("not_found", api.get_submission, str(uuid.uuid4()))
    created = api.list_submissions(limit=50)["submissions"]
    assert not any(tag in item["name"] for item in created)
