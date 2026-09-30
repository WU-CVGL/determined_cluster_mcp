"""The tools against a real master; see ``live.py`` for how to name one.

Every job is launched with zero slots, so it stays queued on a pool without agents, and is
cancelled before its test ends, even when the test fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import live
from determined_compute.client import APIError
from determined_compute.mcp_server import Tools, build_parser, build_tools
from determined_compute.policy import Policy
from determined_compute.spec import TaskSpec
from determined_compute.storage import StorageAccessConfig, StorageService

pytestmark = live.requires_master


@pytest.fixture
def shared(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


@pytest.fixture
def tools(monkeypatch, shared, tmp_path):
    monkeypatch.setenv("DETERMINED_COMPUTE_SECRETS", str(tmp_path / "absent.env"))
    client = live.client(monkeypatch)
    policy = Policy.from_dict(
        {
            "mounts": [{"host_path": str(shared), "container_path": "/shared"}],
            "defaults": {"image": "busybox", "pool": live.pool(), "slots": 0},
        }
    )
    tools = Tools(client, policy, StorageService(policy, StorageAccessConfig()))
    launched = []
    original = tools.launch

    def launch(*args, **kwargs):
        result = original(*args, **kwargs)
        launched.append(result["job_id"])
        return result

    tools.launch = launch
    yield tools
    for job_id in dict.fromkeys(launched):
        client.cancel_submission(job_id)


def spec(tag: str, **fields) -> TaskSpec:
    values = {
        "kind": "command",
        "name": f"live-{tag}",
        "command": "echo live",
        "output_dir": f"/shared/live-{tag}",
    }
    values.update(fields)
    return TaskSpec.model_validate(values)


def refused(code, operation, *args, **kwargs):
    with pytest.raises(APIError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code, caught.value
    return caught.value


def launch_plan(tools, plan):
    resolved = TaskSpec.model_validate(plan["spec"])
    return tools.launch(resolved, plan["request_id"], plan["request_digest"])


def request_ids(tools, **filters):
    return {job["request_id"] for job in tools.list(limit=100, **filters)["jobs"]}


def ended(tools, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while (status := tools.status(job_id))["state"] not in {"canceled", "completed", "failed"}:
        assert time.monotonic() < deadline, "the cancel did not land"
        time.sleep(1)
    return status


def git(repo, *args):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    command = ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
    subprocess.run([*command, "-C", str(repo), *args], check=True, env=environment)


def test_the_protocol_gate_admits_the_master(monkeypatch, tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /cluster/shared\n    container_path: /shared\n"
        f"defaults:\n  image: busybox\n  pool: {live.pool()}\n  slots: 0\n",
        encoding="utf-8",
    )
    client = live.client(monkeypatch)
    arguments = [
        "--profile",
        str(profile),
        "--api-url",
        client.api_url,
        "--secrets-file",
        str(live.settings_path()),
    ]

    assert client.check_protocol()["submission_protocol"] >= 1
    assert build_tools(build_parser().parse_args(arguments)).client.api_url == client.api_url


def test_plan_launch_replay_observe_and_cancel_a_command(tools):
    tag = uuid.uuid4().hex[:12]
    # Every master has the Uncategorized workspace, and a command names it by its id.
    plan = tools.plan(spec(tag, workspace="Uncategorized"))
    assert tools.plan(spec(tag, workspace="Uncategorized"))["request_digest"] == (
        plan["request_digest"]
    )
    assert plan["placement"].startswith("not evaluated")
    assert plan["effective_config"]["resources"]["slots"] == 0

    job = launch_plan(tools, plan)
    assert (job["replayed"], job["outcome"]) == (False, "queued")
    assert job["submitted_at"]
    again = launch_plan(tools, plan)
    assert (again["job_id"], again["replayed"]) == (job["job_id"], True)

    status = tools.status(job["job_id"])
    assert (status["state"], status["request_id"]) == ("queued", plan["request_id"])
    assert status["workspace_id"] == 1
    assert status["request_digest"] == plan["request_digest"]
    assert "waits for the scheduler" in status["explanation"]
    assert plan["request_id"] in request_ids(tools, kind="command")
    logs = tools.logs(job["job_id"], tail=5)
    assert logs["task_id"] == status["entity_id"] and isinstance(logs["lines"], list)
    measured = tools.usage(job["job_id"], window_seconds=600)
    assert measured["allocations"][0]["allocation_id"].startswith(status["entity_id"])

    cancelled = tools.cancel(job["job_id"])
    assert cancelled["cancel"] in {"ended", "recorded"}
    assert ended(tools, job["job_id"])["state"] == "canceled"
    assert tools.cancel(job["job_id"])["cancel"] == "ended"
    replayed = launch_plan(tools, plan)
    assert (replayed["replayed"], replayed["state"]) == (True, "canceled")


def test_a_changed_request_is_refused_and_creates_nothing(tools):
    tag = uuid.uuid4().hex[:12]
    plan = tools.plan(spec(tag))
    job = launch_plan(tools, plan)
    changed = spec(tag, command="echo changed")
    other = tools.plan(changed)

    conflict = refused(
        "key_conflict", tools.launch, changed, plan["request_id"], other["request_digest"]
    )
    assert conflict.details == {"job_id": job["job_id"]}
    fresh = str(uuid.uuid4())
    drift = refused("plan_changed", tools.launch, changed, fresh, plan["request_digest"])
    assert drift.details["expected_digest"] == plan["request_digest"]
    assert drift.details["request_digest"] == other["request_digest"]
    assert fresh not in request_ids(tools)
    # The key stays free: the same key with the new plan creates the job.
    created = tools.launch(changed, fresh, other["request_digest"])
    assert created["replayed"] is False and created["job_id"] != job["job_id"]


def test_immediate_admission_is_refused_and_creates_nothing(tools):
    tag = uuid.uuid4().hex[:12]
    immediate = spec(tag, admission="immediate")

    refused("admission_unsupported", tools.plan, immediate)
    refused("admission_unsupported", tools.launch, immediate, str(uuid.uuid4()), "0" * 64)
    # The master refuses it too, for a command and for an experiment.
    for kind, config in (
        ("command", {"description": f"live-{tag}", "resources": {"slots": 0}}),
        ("experiment", {"name": f"live-{tag}", "searcher": {"name": "single", "metric": "loss"}}),
    ):
        refused(
            "admission_unsupported",
            tools.client.submit,
            kind,
            config,
            dry_run=True,
            admission="immediate",
        )
    assert not [job for job in tools.list(limit=100)["jobs"] if tag in job["name"]]


def test_an_experiment_plans_launches_and_cancels(tools):
    tag = uuid.uuid4().hex[:12]
    experiment = {
        "searcher": {"name": "single", "metric": "loss"},
        "max_restarts": 0,
        "checkpoint_storage": {"type": "shared_fs", "storage_path": f"live-{tag}/checkpoints"},
    }
    bogus = {"searcher": {"name": "bogus", "max_concurrent_trials": 1}}
    broken = spec(tag, kind="experiment", experiment=bogus)
    refused("invalid_request", tools.plan, broken)

    # The master resolves the workspace and project names of an experiment.
    names = {"workspace": "Uncategorized", "project": "Uncategorized"}
    plan = tools.plan(spec(tag, kind="experiment", experiment=experiment, **names))
    assert plan["effective_config"]["checkpoint_storage"]["storage_path"] == (
        f"live-{tag}/checkpoints"
    )
    job = launch_plan(tools, plan)
    assert job["outcome"] == "queued"
    status = tools.status(job["job_id"])
    assert status["kind"] == "experiment" and status["request_id"] == plan["request_id"]
    assert (status["workspace_id"], status["project_id"]) == (1, 1)
    assert isinstance(tools.logs(job["job_id"], tail=5)["lines"], list)
    if status["tasks"]:  # usage needs the trial's task, which the master may not have made yet
        tools.usage(job["job_id"], window_seconds=600)
    tools.cancel(job["job_id"])
    assert ended(tools, job["job_id"])["state"] == "canceled"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_git_and_context_code_launch_their_pinned_commit(tools, shared):
    repo = shared / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "train.py").write_text("print('live')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    tag = uuid.uuid4().hex[:12]

    for code in (
        {"source": "git", "repo": "/shared/repo"},
        {"source": "context", "repo": str(repo)},
    ):
        plan = tools.plan(spec(tag, code=code))
        assert plan["spec"]["code"]["revision"] == plan["commit"]
        job = launch_plan(tools, plan)
        assert launch_plan(tools, plan)["job_id"] == job["job_id"]
        tools.cancel(job["job_id"])
