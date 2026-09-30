"""The tools against a fake master that keeps the ledger's key, digest and cancel rules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from determined_compute.client import APIError
from determined_compute.code import CodeError
from determined_compute.mcp_server import PLACEMENT, Tools, explain, tool_error
from determined_compute.policy import Policy, PolicyError
from determined_compute.spec import TaskSpec
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService
from determined_compute.usage import UsageError

from fakes import SUBMITTED_AT, FakeMaster, allocation, submission

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


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
    completed = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-C", str(repo)]
        + list(args),
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout.decode().strip()


def commit(repo: Path, files: Dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def shared(tmp_path) -> Path:
    root = tmp_path / "shared"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def master() -> FakeMaster:
    return FakeMaster()


def make_tools(master, shared, *, access=None, **policy_fields) -> Tools:
    policy = Policy.from_dict(
        {
            "mounts": [
                {"host_path": str(shared), "container_path": "/shared"},
                {"host_path": str(shared / "data"), "container_path": "/data", "read_only": True},
            ],
            "defaults": {"image": "registry.example/train:1", "pool": "gpu", "slots": 1},
            "pools": ["gpu", "cpu"],
            "max_slots": 4,
            **policy_fields,
        }
    )
    storage = StorageService(policy, access or StorageAccessConfig())
    clock = iter(range(1000))
    return Tools(
        master, policy, storage, sleep=lambda seconds: None, clock=lambda: next(clock)
    )


@pytest.fixture
def tools(master, shared) -> Tools:
    return make_tools(master, shared)


@pytest.fixture
def repo(shared) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is required")
    root = shared / "repos" / "app"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    commit(root, {"train.py": "print('train')\n"})
    return root


def spec(**fields: Any) -> TaskSpec:
    values: Dict[str, Any] = {
        "kind": "command",
        "name": "train",
        "command": "python train.py",
        "output_dir": "/shared/out",
    }
    values.update(fields)
    return TaskSpec.model_validate(values)


def refused(code: str, operation, *args, **kwargs):
    with pytest.raises((APIError, ValueError)) as caught:
        operation(*args, **kwargs)
    assert getattr(caught.value, "code", "invalid_request") == code, caught.value
    return caught.value


# Plan


def test_plan_dry_runs_the_exact_request_once(tools, master):
    plan = tools.plan(spec(env={"B": "2", "A": "1"}))

    ((name, sent),) = master.calls
    assert name == "submit" and sent["dry_run"] and sent["key"] is None
    assert sent["config"]["resources"] == {"resource_pool": "gpu", "slots": 1}
    assert sent["config"]["entrypoint"][:2] == ["/bin/bash", "-lc"]
    assert plan["request_digest"] == FakeMaster.digest("command", sent["config"], (), None)
    assert uuid.UUID(plan["request_id"]).version == 4
    assert plan["spec"]["pool"] == "gpu" and plan["spec"]["slots"] == 1
    assert plan["spec"]["image"] == "registry.example/train:1"
    assert (plan["commit"], plan["content_digest"], plan["code"]) == (None, None, None)
    assert plan["placement"] == PLACEMENT
    assert plan["warnings"] == []
    effective = plan["effective_config"]
    assert effective["observed"].startswith("observed at plan time")
    assert effective["environment"]["environment_variables"] == "[redacted]"
    assert "registry_auth" not in json.dumps(effective)
    assert effective["bind_mounts"] == master.bind_mounts
    assert tools.plan(spec(env={"A": "1", "B": "2"}))["request_digest"] == plan["request_digest"]


def test_every_plan_mints_a_new_request_id(tools):
    assert tools.plan(spec())["request_id"] != tools.plan(spec())["request_id"]


def test_paths_outside_the_effective_bind_mounts_are_warned(tools, master):
    master.bind_mounts = None
    master.warnings = ["current_slots_exceeded"]

    warnings = tools.plan(spec(code={"source": "path", "dir": "/shared/app"}))["warnings"]

    codes = [item["code"] for item in warnings]
    assert codes == ["path_not_bind_mounted", "current_slots_exceeded"]
    assert warnings[0]["paths"] == ["/shared/out", "/shared/app"]
    assert "waits in the queue" in warnings[1]["message"]


@needs_git
def test_git_plan_pins_the_commit_and_launch_submits_it_after_head_moves(tools, master, repo):
    first = git(repo, "rev-parse", "HEAD")
    plan = tools.plan(spec(code={"source": "git", "repo": "/shared/repos/app"}, workdir="src"))

    assert plan["commit"] == plan["content_digest"] == first
    assert plan["spec"]["code"]["revision"] == first
    assert plan["code"] == {
        "source": "git",
        "repo": "/shared/repos/app",
        "commit": first,
        "uses_lfs": False,
    }
    entrypoint = master.calls[0][1]["config"]["entrypoint"][2]
    assert f"checkout -q --detach {first}" in entrypoint

    commit(repo, {"train.py": "print('moved')\n"})
    resolved = TaskSpec.model_validate(plan["spec"])
    launched = tools.launch(resolved, plan["request_id"], plan["request_digest"])

    assert launched["replayed"] is False and launched["outcome"] == "queued"
    assert launched["submitted_at"] == SUBMITTED_AT
    assert launched["request_id"] == plan["request_id"]
    (created,) = master.created()
    assert created["key"] == plan["request_id"]
    assert created["expected"] == plan["request_digest"]
    assert f"checkout -q --detach {first}" in created["config"]["entrypoint"][2]


@needs_git
def test_a_moving_revision_returns_plan_changed_and_creates_nothing(tools, master, repo):
    moving = spec(code={"source": "git", "repo": "/shared/repos/app", "revision": "main"})
    plan = tools.plan(moving)
    moved = commit(repo, {"train.py": "print('moved')\n"})

    error = refused(
        "plan_changed", tools.launch, moving, plan["request_id"], plan["request_digest"]
    )

    assert error.details["commit"] == error.details["content_digest"] == moved
    assert error.details["expected_digest"] == plan["request_digest"]
    assert error.details["request_digest"] != plan["request_digest"]
    assert master.jobs == {} and master.keys == {}
    # A fresh plan and key succeed.
    fresh = tools.plan(moving)
    assert fresh["commit"] == moved
    launched = tools.launch(moving, fresh["request_id"], fresh["request_digest"])
    assert launched["job_id"] in master.jobs


@needs_git
def test_a_lost_launch_response_replays_after_the_tree_changes(tools, master, repo):
    (repo / "notes.txt").write_text("first\n")
    context = spec(
        code={"source": "context", "repo": str(repo), "include": ["notes.txt"]},
    )
    plan = tools.plan(context)
    resolved = TaskSpec.model_validate(plan["spec"])
    first = tools.launch(resolved, plan["request_id"], plan["request_digest"])

    (repo / "notes.txt").write_text("changed\n")
    again = tools.launch(resolved, plan["request_id"], plan["request_digest"])

    assert (again["job_id"], again["replayed"]) == (first["job_id"], True)
    assert len(master.jobs) == 1
    # A new request with the changed tree needs its own plan.
    refused("plan_changed", tools.launch, resolved, str(uuid.uuid4()), plan["request_digest"])


@needs_git
def test_context_code_is_the_command_files_or_the_model_definition(tools, master, repo):
    commit(repo, {"startup-hook.sh": "echo hook\n"})
    command = tools.plan(spec(code={"source": "context", "repo": str(repo)}))
    experiment = tools.plan(
        spec(kind="experiment", code={"source": "context", "repo": str(repo)})
    )

    sent_command, sent_experiment = (call[1] for call in master.calls)
    assert sent_command["files"] and sent_command["files"] == sent_experiment["files"]
    paths = {item["path"] for item in sent_command["files"]}
    assert {"train.py", "startup-hook.sh", ".code-provenance.json"} <= paths
    assert sent_experiment["kind"] == "experiment"
    assert command["commit"] == git(repo, "rev-parse", "HEAD")
    assert len(command["content_digest"]) == 64 and command["content_digest"] != command["commit"]
    assert command["code"]["source"] == "context" and command["code"]["dirty"] is False
    assert "startup_hook" in [item["code"] for item in command["warnings"]]
    assert experiment["spec"]["code"]["revision"] == command["commit"]


def test_path_code_is_unpinned_and_must_be_mounted(tools, master, shared):
    (shared / "app").mkdir()

    plan = tools.plan(spec(code={"source": "path", "dir": "/shared/app"}))

    assert plan["content_digest"] == "unpinned" and plan["commit"] is None
    assert plan["code"] == {
        "source": "path",
        "dir": "/shared/app",
        "observed_commit": None,
        "observed_dirty": None,
        "verified": False,
    }
    master.calls.clear()
    refused("path_not_mounted", tools.plan, spec(code={"source": "path", "dir": "/elsewhere"}))
    assert master.calls == []


@needs_git
def test_git_code_is_planned_only_through_a_local_mount(master, shared, repo):
    access = StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example"}})
    tools = make_tools(master, shared, access=access)

    error = refused(
        "storage_not_local", tools.plan, spec(code={"source": "git", "repo": "/shared/repos/app"})
    )

    assert "not over SSH" in str(error)
    assert master.calls == []


def test_a_git_repo_outside_every_mount_is_refused(tools, master):
    refused("path_not_mounted", tools.plan, spec(code={"source": "git", "repo": "/elsewhere"}))
    assert master.calls == []


@pytest.mark.parametrize("operation", ["plan", "launch"])
def test_immediate_admission_is_refused_before_anything_runs(tools, master, operation):
    # The repository does not exist: nothing may read it before the refusal.
    immediate = spec(admission="immediate", code={"source": "git", "repo": "/shared/missing"})
    args = (immediate,) if operation == "plan" else (immediate, str(uuid.uuid4()), "d" * 64)

    refused("admission_unsupported", getattr(tools, operation), *args)

    assert master.calls == []


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"pool": "tpu"}, "pool_not_allowed"),
        ({"slots": 5}, "slots_exceed_limit"),
        (
            {
                "kind": "experiment",
                "slots": 2,
                "experiment": {
                    "searcher": {"name": "random", "metric": "loss", "max_concurrent_trials": 3}
                },
            },
            "slots_exceed_limit",
        ),
        ({"output_dir": "/elsewhere/out"}, "path_not_mounted"),
        ({"output_dir": "/sharedx/out"}, "path_not_mounted"),
        ({"output_dir": "/data/out"}, "read_only_storage"),
        ({"workspace": "unknown"}, "not_found"),
    ],
)
def test_policy_and_lookups_refuse_before_the_dry_run(tools, master, fields, code):
    refused(code, tools.plan, spec(**fields))

    assert [call for call in master.calls if call[0] == "submit"] == []


def test_a_command_workspace_is_resolved_to_its_id(tools, master):
    tools.plan(spec(workspace="research"))

    assert master.calls[0] == ("find_workspace_id", "research")
    assert master.calls[1][1]["workspace_id"] == 7


def test_an_experiment_names_its_workspace_and_project_for_the_master(tools, master):
    tools.plan(spec(kind="experiment", workspace="research", project="baselines"))

    ((name, sent),) = master.calls
    assert sent["workspace_id"] is None
    assert (sent["config"]["workspace"], sent["config"]["project"]) == ("research", "baselines")


def test_a_dry_run_error_creates_nothing(tools, master):
    master.dry_run_error = APIError('unknown field "storage_path"', code="invalid_request")

    refused("invalid_request", tools.plan, spec())

    assert master.jobs == {}


# Launch


def test_launch_replays_a_retry_and_conflicts_on_other_content(tools, master):
    plan = tools.plan(spec())
    first = tools.launch(spec(), plan["request_id"], plan["request_digest"])
    again = tools.launch(spec(), plan["request_id"], plan["request_digest"])

    assert (again["job_id"], again["replayed"]) == (first["job_id"], True)

    other = tools.plan(spec(command="python other.py"))
    error = refused(
        "key_conflict",
        tools.launch,
        spec(command="python other.py"),
        plan["request_id"],
        other["request_digest"],
    )
    assert error.details == {"job_id": first["job_id"]}
    assert len(master.jobs) == 1


@pytest.mark.parametrize(
    "request_id, digest",
    [("not-a-uuid", "d" * 64), (str(uuid.uuid1()), "d" * 64), (str(uuid.uuid4()), "")],
)
def test_launch_validates_the_plan_handles_before_any_call(tools, master, request_id, digest):
    refused("invalid_request", tools.launch, spec(), request_id, digest)

    assert master.calls == []


def test_a_failed_read_after_the_create_still_returns_the_job(tools, master):
    plan = tools.plan(spec())
    master.read_error = APIError("unavailable", code="unavailable", retryable=True)

    launched = tools.launch(spec(), plan["request_id"], plan["request_digest"])

    assert launched["job_id"] in master.jobs
    assert launched["submitted_at"] is None
    assert "compute_status" in launched["note"]


# Observe


def test_status_explains_the_job_and_names_its_request_id(tools, master):
    master.jobs["j"] = submission("j", key="k")

    status = tools.status("j")

    assert status["request_id"] == "k" and "idempotency_key" not in status
    assert status["tasks"][0]["allocations"][0]["state"] == "queued"
    assert "waits for the scheduler" in status["explanation"]


@pytest.mark.parametrize(
    "fields, text",
    [
        ({"state": "completed", "exit_class": "none"}, "The job completed. It ended without"),
        ({"state": "failed", "exit_class": "workload_failed"}, "'compute:'"),
        ({"state": "failed", "exit_class": "infrastructure_failed"}, "did not cause it"),
        ({"state": "canceled", "exit_class": None}, "The job was cancelled. No exit class"),
        ({"state": "deleted", "exit_class": "none"}, "The job was deleted."),
        ({"state": "paused"}, "paused"),
        ({"state": "running", "kind": "experiment"}, "waits for the scheduler"),
    ],
)
def test_explanations(fields, text):
    assert text in explain(submission("j", **fields))


def test_a_running_allocation_reads_running():
    job = submission("j", state="running")
    job["tasks"][0]["allocations"][0]["state"] = "running"

    assert explain(job) == "The job is running."


def test_list_pages_jobs_without_their_tasks(tools, master):
    master.jobs["j"] = submission("j", key="k")

    listed = tools.list(kind="command", state="queued", limit=10, cursor="c1")

    assert master.calls[-1] == ("list_submissions", "command", "queued", 10, "c1")
    (job,) = listed["jobs"]
    assert job["request_id"] == "k" and "tasks" not in job
    assert listed["next_cursor"] == "next"


def test_logs_resolve_the_task_through_the_submission(tools, master):
    master.jobs["j"] = submission("j")

    logs = tools.logs("j", tail=5)

    assert master.calls[-1] == ("task_logs", "task-j", 5)
    assert logs == {
        "job_id": "j",
        "task_id": "task-j",
        "trial_id": None,
        "lines": [{"log": "hello"}],
    }


def test_logs_select_an_experiment_trial(tools, master):
    job = submission("j", kind="experiment", entity_id="12")
    job["tasks"] = [
        {"task_id": "trial-1", "trial_id": 1, "allocations": [allocation("trial-1")]},
        {"task_id": "trial-2", "trial_id": 2, "allocations": [allocation("trial-2")]},
    ]
    master.jobs["j"] = job

    assert tools.logs("j")["task_id"] == "trial-2"
    assert tools.logs("j", trial_id=1)["trial_id"] == 1
    refused("not_found", tools.logs, "j", trial_id=9)


def test_logs_of_a_job_without_a_task_are_empty(tools, master):
    master.jobs["j"] = submission("j", kind="experiment", tasks=[])

    logs = tools.logs("j")

    assert logs["lines"] == [] and logs["task_id"] is None
    assert "no trials yet" in logs["note"]


def test_usage_of_a_queued_job_is_unmeasured(tools, master):
    master.jobs["j"] = submission("j")

    measured = tools.usage("j", window_seconds=600)

    assert measured["measurement"] == "unmeasured"
    assert measured["allocations"][0]["allocation_id"] == "task-j.1"


def test_resources_are_a_projection_stamped_with_observed_at(tools, master):
    pool = {
        "name": "gpu",
        "description": None,
        "type": "static",
        "num_agents": 2,
        "slots_available": 8,
        "slots_used": 3,
        "slot_type": "cuda",
        "slots_per_agent": 4,
        "aux_container_capacity": 100,
        "aux_containers_running": 0,
    }
    master.pools = [pool, {**pool, "name": "cpu"}]
    a100 = {"type": "cuda", "brand": "NVIDIA A100", "uuid": None}
    master.agents = [
        {"id": "a", "resource_pools": ["gpu"], "devices": [a100, a100]},
        {"id": "b", "resource_pools": ["gpu"], "devices": [a100, {**a100, "brand": "NVIDIA H100"}]},
        {"id": "c", "resource_pools": ["cpu"], "devices": []},
    ]

    result = tools.resources("gpu")

    assert set(result) == {"observed_at", "pools"}
    (projected,) = result["pools"]
    assert projected == {
        **pool,
        "device_models": [
            {"type": "cuda", "brand": "NVIDIA A100", "count": 3},
            {"type": "cuda", "brand": "NVIDIA H100", "count": 1},
        ],
    }
    assert len(tools.resources()["pools"]) == 2
    refused("not_found", tools.resources, "tpu")


def test_storage_check_states_its_viewpoint(tools, shared):
    checked = tools.storage_check("/shared")

    assert checked["exists"] is True
    assert checked["viewpoint"]["backend"] == "local"
    assert "not of the container user" in checked["viewpoint"]["note"]


# Control


def test_cancel_waits_for_the_job_to_end(tools, master):
    master.jobs["j"] = submission("j")
    master.cancel_after_polls = 2

    cancelled = tools.cancel("j")

    assert cancelled["cancel"] == "ended" and cancelled["state"] == "canceled"
    assert [call[0] for call in master.calls] == [
        "cancel_submission",
        "get_submission",
        "get_submission",
    ]


def test_cancel_reports_a_recorded_cancel_when_the_job_has_not_ended(master, shared):
    master.jobs["j"] = submission("j")
    master.cancel_after_polls = 10**6
    tools = make_tools(master, shared)
    tools._cancel_wait = 3

    cancelled = tools.cancel("j")

    assert cancelled["cancel"] == "recorded" and cancelled["state"] == "queued"
    assert "compute_status" in cancelled["explanation"]


def test_cancel_stops_polling_when_the_read_fails(tools, master):
    master.jobs["j"] = submission("j")
    master.read_error = APIError("down", code="unavailable", retryable=True)

    assert tools.cancel("j")["cancel"] == "recorded"


def test_transfers_respect_the_overwrite_policy(tools, tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    refused("overwrite_not_allowed", tools.storage_sync, str(source), "/shared/job", True, True)
    assert tools.storage_sync(str(source), "/shared/job")["overwrite"] is False


# Errors


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            APIError("used", code="key_conflict", details={"job_id": "j"}),
            {"code": "key_conflict", "message": "used", "retryable": False,
             "details": {"job_id": "j"}},
        ),
        (
            APIError("down", code="unavailable", retryable=True),
            {"code": "unavailable", "message": "down", "retryable": True},
        ),
        (
            PolicyError("no", code="pool_not_allowed", details={"pool": "x"}),
            {"code": "pool_not_allowed", "message": "no", "retryable": False,
             "details": {"pool": "x"}},
        ),
        (
            CodeError("big", code="context_too_large", details={"paths": ("a",)}),
            {"code": "context_too_large", "message": "big", "retryable": False,
             "details": {"paths": ["a"]}},
        ),
        (
            StorageError("ro", code="read_only_storage"),
            {"code": "read_only_storage", "message": "ro", "retryable": False},
        ),
        (
            UsageError("later", code="task_not_started"),
            {"code": "task_not_started", "message": "later", "retryable": False},
        ),
        (
            ValueError("tail must be an integer from 0 to 10000"),
            {"code": "invalid_request", "message": "tail must be an integer from 0 to 10000",
             "retryable": False},
        ),
        (OSError("disk"), {"code": "internal_error", "message": "disk", "retryable": False}),
    ],
)
def test_tool_errors_have_stable_codes_and_json_details(error, expected):
    assert tool_error(error) == {"error": expected}
    json.dumps(tool_error(error))


def test_an_invalid_spec_is_an_invalid_request():
    with pytest.raises(ValidationError) as caught:
        spec(kind="shell", command="bash")

    assert tool_error(caught.value)["error"]["code"] == "invalid_request"
