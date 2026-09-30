"""The compute MCP: eleven tools over the Determined job ledger and shared storage.

``Tools`` implements each tool without the MCP package, so it can be tested with a fake client;
``create_server`` publishes them. There is no local state: the master keeps every job, keyed by
the ``request_id`` a plan mints, and ``job_id`` is the only handle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import ValidationError

from determined_compute import __version__, usage
from determined_compute import code as code_plan
from determined_compute.client import APIError, Client
from determined_compute.policy import Policy, PolicyError, Resources
from determined_compute.spec import (
    CreateRequest,
    PlannedCode,
    TaskSpec,
    compile_request,
    concurrent_trials,
    resolve,
)
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService

PLACEMENT = "not evaluated; the scheduler decides after launch"
_ENDINGS = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "was cancelled",
    "deleted": "was deleted",
}
ENDED_STATES = frozenset(_ENDINGS)
_REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_OBSERVED = (
    "observed at plan time: master and pool defaults are not bound by the plan and apply as "
    "they stand at launch"
)
# Top-level effective settings worth reviewing; the rest are Determined's defaults.
_SUMMARY_KEYS = (
    "name",
    "description",
    "entrypoint",
    "resources",
    "bind_mounts",
    "work_dir",
    "checkpoint_storage",
    "searcher",
    "hyperparameters",
    "max_restarts",
    "workspace",
    "project",
)
_STORAGE_KEYS = frozenset(
    {
        "type",
        "bucket",
        "prefix",
        "container",
        "host_path",
        "container_path",
        "storage_path",
        "checkpoint_path",
        "tensorboard_path",
        "propagation",
        "save_experiment_best",
        "save_trial_best",
        "save_trial_latest",
    }
)
_MASTER_WARNINGS = {
    "current_slots_exceeded": (
        "the request needs more slots than the cluster has now; the job waits in the queue "
        "until they exist"
    ),
}
_EXIT_CLASSES = {
    "none": "It ended without a failure: it completed or was cancelled.",
    "workload_failed": (
        "The workload exited with an error. A failed prelude (code delivery, output_dir or "
        "workdir) counts too, and prints a line starting with 'compute:' in compute_logs."
    ),
    "workload_initialization_failed": (
        "The container failed before the workload started, for example while pulling the image "
        "or creating the container."
    ),
    "node_preflight_failed": "The node refused the allocation in its preflight checks.",
    "placement_unsatisfied": "The scheduler could not place the job as its admission required.",
    "infrastructure_failed": (
        "An agent or its connection was lost, or the master could not restore the task; the "
        "workload did not cause it."
    ),
}
_NO_EXIT_CLASS = (
    "No exit class was recorded: jobs submitted before the ledger have none, and neither has a "
    "cancelled experiment whose trial never started."
)
INSTRUCTIONS = (
    "Plan, then launch. compute_plan(spec) validates a TaskSpec, pins its code revision, "
    "applies the policy and dry-runs the exact request on the master; review the resolved "
    "spec, commit, effective config and warnings, then call compute_launch with the returned "
    "spec, request_id and request_digest. Retrying a launch with the same arguments returns "
    "the same job; plan_changed means the code or request moved since the plan, so plan again. "
    "job_id is the only handle: compute_status, compute_logs, compute_usage and compute_cancel "
    "take it, and compute_list finds the jobs of every client with their request_id. Only "
    "admission=queue is supported, and placement is not evaluated before launch; "
    "compute_resources is a projection of the pools, not a verdict. Keep data and outputs on "
    "the mounted shared storage and move files with storage_sync and storage_fetch, previewing "
    "with dry_run first. Credentials belong in the local configuration, never in tool "
    "arguments or env."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _refuse_immediate(spec: TaskSpec) -> None:
    # Checked before any code is read or request sent, so nothing is created.
    if spec.admission != "queue":
        raise APIError(
            "admission=immediate is not supported in this release; use queue",
            code="admission_unsupported",
        )


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise ValueError("request_id must be the UUID that compute_plan returned")
    return value


def _job(submission: Mapping[str, Any], *, tasks: bool = True) -> Dict[str, Any]:
    """A submission as the tools report it; its idempotency key is the plan's request_id."""

    job = {key: value for key, value in submission.items() if key != "idempotency_key"}
    job["request_id"] = submission["idempotency_key"]
    if not tasks:
        job.pop("tasks")
    return job


def explain(submission: Mapping[str, Any]) -> str:
    state = submission["state"]
    if state in ENDED_STATES:
        exit_class = submission["exit_class"]
        text = (
            _NO_EXIT_CLASS
            if exit_class is None
            else _EXIT_CLASSES.get(exit_class, f"Its exit class is {exit_class}.")
        )
        return f"The job {_ENDINGS[state]}. {text}"
    if state == "paused":
        return "The job is paused."
    latest = [task["allocations"][-1] for task in submission["tasks"] if task["allocations"]]
    # An active experiment reads running while every trial still waits for resources.
    if state == "queued" or (latest and all(item["state"] == "queued" for item in latest)):
        return "The job waits for the scheduler to place it; placement is decided after launch."
    return "The job is running."


def _code_summary(planned: PlannedCode) -> Optional[Dict[str, Any]]:
    if isinstance(planned, code_plan.GitCode):
        return {
            "source": "git",
            "repo": planned.repo,
            "commit": planned.commit,
            "uses_lfs": planned.uses_lfs,
        }
    if isinstance(planned, code_plan.ContextCode):
        return {
            "source": "context",
            "repo": planned.repo,
            "commit": planned.commit,
            "dirty": planned.dirty,
            "files": len(planned.files),
            "size": planned.size,
            "included": list(planned.included),
            "excluded": list(planned.excluded),
            "skipped": list(planned.skipped),
        }
    if isinstance(planned, code_plan.PathCode):
        # What the planner saw in the directory; nothing ties it to what the task runs.
        return {
            "source": "path",
            "dir": planned.dir,
            "observed_commit": planned.observed_commit,
            "observed_dirty": planned.observed_dirty,
            "verified": planned.verified,
        }
    return None


def _effective_summary(effective: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The reviewable part of the master's effective config, as the client redacted it."""

    effective = effective or {}
    summary: Dict[str, Any] = {"observed": _OBSERVED}
    for key in _SUMMARY_KEYS:
        if key in effective:
            summary[key] = effective[key]
    storage = summary.get("checkpoint_storage")
    if isinstance(storage, Mapping):
        # The master masks only S3 keys, and a workspace default may hold an account key in a
        # connection string or an endpoint URL, so only named, credential-free fields pass.
        summary["checkpoint_storage"] = {
            key: value for key, value in storage.items() if key in _STORAGE_KEYS
        }
    environment = effective.get("environment")
    if isinstance(environment, Mapping):
        summary["environment"] = {
            key: environment[key]
            for key in ("image", "environment_variables")
            if key in environment
        }
    return summary


def _bind_targets(effective: Optional[Mapping[str, Any]]) -> List[str]:
    mounts = (effective or {}).get("bind_mounts") or []
    return [
        mount["container_path"]
        for mount in mounts
        if isinstance(mount, Mapping) and isinstance(mount.get("container_path"), str)
    ]


def _warnings(
    spec: TaskSpec,
    planned: PlannedCode,
    effective: Optional[Mapping[str, Any]],
    master: Sequence[str],
) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []
    for warning in getattr(planned, "warnings", ()):
        warnings.append(
            {"code": warning.code, "message": warning.message, "paths": list(warning.paths)}
        )
    if isinstance(planned, code_plan.GitCode) and planned.uses_lfs:
        warnings.append(
            {
                "code": "lfs_required",
                "message": "the commit has Git LFS files, so the image needs git-lfs",
                "paths": [],
            }
        )
    # The container sees shared storage only through the bind mounts the master applies.
    paths = [spec.output_dir]
    if isinstance(planned, code_plan.GitCode):
        paths.append(planned.repo)
    elif isinstance(planned, code_plan.PathCode):
        paths.append(planned.dir)
    targets = _bind_targets(effective)
    outside = [path for path in paths if path and not code_plan.under_root(path, targets)]
    if outside:
        warnings.append(
            {
                "code": "path_not_bind_mounted",
                "message": (
                    "no bind mount of the effective config holds these paths, so the task "
                    "cannot reach them on shared storage"
                ),
                "paths": outside,
            }
        )
    for name in master:
        message = _MASTER_WARNINGS.get(name, "the master reported this warning")
        warnings.append({"code": name, "message": message, "paths": []})
    return warnings


class Tools:
    """The tool implementations over one master, one policy and one storage access."""

    def __init__(
        self,
        client: Any,
        policy: Policy,
        storage: StorageService,
        secrets_path: Optional[Path] = None,
        *,
        cancel_wait_seconds: float = 10.0,
        poll_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.policy = policy
        self.storage = storage
        self.secrets_path = secrets_path
        self._cancel_wait = cancel_wait_seconds
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock

    # Plan and launch

    def plan(self, spec: TaskSpec) -> Dict[str, Any]:
        """Render ``spec`` and dry-run it on the master; nothing is created."""

        _refuse_immediate(spec)
        planned, resources, request = self._render(spec)
        result = self.client.submit(
            request.kind,
            request.config,
            files=request.files,
            workspace_id=request.workspace_id,
            dry_run=True,
        )
        effective = result["effective_config"]
        return {
            "spec": resolve(spec, planned, resources).model_dump(mode="json"),
            "request_id": str(uuid.uuid4()),
            "request_digest": result["request_digest"],
            "commit": getattr(planned, "commit", None),
            "content_digest": getattr(planned, "content_digest", None),
            "code": _code_summary(planned),
            "effective_config": _effective_summary(effective),
            "warnings": _warnings(spec, planned, effective, result["warnings"]),
            "placement": PLACEMENT,
        }

    def launch(self, spec: TaskSpec, request_id: str, request_digest: str) -> Dict[str, Any]:
        """Create the planned job, bound to the plan's digest; a retry replays it."""

        _refuse_immediate(spec)
        key = _request_id(request_id)
        if not isinstance(request_digest, str) or not request_digest:
            raise ValueError("request_digest must be the digest that compute_plan returned")
        notes: List[str] = []
        try:
            # A revision pinned to a full SHA resolves to itself, so this renders the planned
            # commit.
            planned, _resources, request = self._render(spec)
        except (ValueError, APIError) as exc:
            if getattr(exc, "retryable", False):
                raise  # the master is unreachable, so it cannot be asked either
            result = self._replay_unrendered(spec.kind, key, request_digest, exc)
            notes.append(
                f"the spec no longer renders ({getattr(exc, 'code', 'invalid_request')}), so this "
                "is the job an earlier launch created; a new job needs a new plan"
            )
        else:
            result = self._create(request, key, request_digest, planned)
        launched: Dict[str, Any] = {
            "job_id": result["job_id"],
            "request_id": key,
            "replayed": result["replayed"],
            "outcome": result["outcome"],
            "submitted_at": None,
            "state": None,
            "explanation": None,
        }
        # The create answer carries no time, and a replay's outcome is the one stored at submit,
        # even for a job that has since ended; a failed read must not hide the job it created.
        try:
            submission = self.client.get_submission(result["job_id"])
            launched["submitted_at"] = submission["submitted_at"]
            launched["state"] = submission["state"]
            # An active experiment reads running while its trials wait for resources.
            launched["explanation"] = explain(submission)
        except APIError as exc:
            notes.append(
                f"the job exists, but its submission could not be read ({exc.code}); "
                "compute_status reports it"
            )
        if notes:
            launched["note"] = "; ".join(notes)
        return launched

    def _create(
        self, request: CreateRequest, key: str, digest: str, planned: PlannedCode
    ) -> Dict[str, Any]:
        try:
            return self.client.submit(
                request.kind,
                request.config,
                files=request.files,
                workspace_id=request.workspace_id,
                idempotency_key=key,
                expected_digest=digest,
            )
        except APIError as exc:
            if exc.code != "plan_changed":
                raise
            # The master's new digest is withheld, so content that differs from the plan goes
            # through compute_plan, and its review, before any launch.
            raise APIError(
                "the request differs from the plan, so nothing was created; review the new "
                "commit and content, then plan again",
                code="plan_changed",
                details={
                    "commit": getattr(planned, "commit", None),
                    "content_digest": getattr(planned, "content_digest", None),
                },
            ) from None

    def _replay_unrendered(
        self, kind: str, key: str, digest: str, error: Exception
    ) -> Dict[str, Any]:
        """The job an earlier launch created under ``key``, when the spec no longer renders.

        A retry after a lost response must return that job even if the tree, the policy or a
        workspace changed since, so the master is asked before ``error`` is reported. The error
        then names the request_id, and says so when the master could not tell.
        """

        details = {**(getattr(error, "details", None) or {}), "request_id": key}
        try:
            found = self.client.replay(kind, key, digest)
        except APIError as probe:
            if probe.code == "key_conflict":
                raise
            error.details = {**details, "replay": probe.code}  # type: ignore[attr-defined]
            error.args = (
                f"{error}; whether an earlier launch created a job under this request_id is "
                f"unknown ({probe.code}), so check compute_list before planning again",
            )
            raise error from None
        if found is None:
            error.details = details  # type: ignore[attr-defined]
            raise error
        return found

    def _render(self, spec: TaskSpec) -> Tuple[PlannedCode, Resources, CreateRequest]:
        resources = self.policy.resources(
            image=spec.image, pool=spec.pool, slots=spec.slots, trials=concurrent_trials(spec)
        )
        if spec.output_dir is not None:
            self._check_output(spec.output_dir)
        planned = self._plan_code(spec)
        workspace_id = None
        if spec.kind != "experiment" and spec.workspace is not None:
            workspace_id = self.client.find_workspace_id(spec.workspace)
        request = compile_request(spec, planned, resources, workspace_id=workspace_id)
        return planned, resources, request

    def _check_output(self, output_dir: str) -> None:
        # The prelude creates output_dir, so it must lie on writable shared storage.
        if self.policy.mounts.read_only(output_dir, "output_dir"):
            raise PolicyError(
                f"output_dir {output_dir} is on read-only shared storage",
                code="read_only_storage",
                details={"path": output_dir},
            )

    def _plan_code(self, spec: TaskSpec) -> PlannedCode:
        code = spec.code
        if code is None:
            return None
        if code.source == "context":
            return code_plan.plan_context(
                code.repo,
                code.revision,
                code.include,
                code.exclude,
                secrets_file=self.secrets_path,
            )
        if code.source == "path":
            self.policy.mounts.to_host(code.dir, "code.dir")
            try:
                local: Optional[Path] = self.storage.local_path(code.dir, "code.dir")
            except StorageError:
                local = None  # nothing to observe; path code is unpinned either way
            return code_plan.plan_path(code.dir, local)
        self.policy.mounts.to_host(code.repo, "code.repo")
        try:
            repository = self.storage.local_path(code.repo, "code.repo")
        except StorageError as exc:
            if exc.code != "storage_not_local":
                raise
            raise StorageError(
                f"{exc}; this release plans git code only through a local mount, not over SSH, "
                "so map the repository's root in local_mounts with storage mode auto or local, "
                "or send the code as context",
                code="storage_not_local",
            ) from None
        return code_plan.plan_git(
            repository, code.repo, self.policy.mounts.container_roots, code.revision
        )

    # Observe

    def status(self, job_id: str) -> Dict[str, Any]:
        submission = self.client.get_submission(job_id)
        return {**_job(submission), "explanation": explain(submission)}

    def list(
        self,
        kind: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self.client.list_submissions(kind=kind, state=state, limit=limit, page_token=cursor)
        return {
            "jobs": [_job(item, tasks=False) for item in page["submissions"]],
            "next_cursor": page["next_page_token"],
        }

    def logs(self, job_id: str, trial_id: Optional[int] = None, tail: int = 200) -> Dict[str, Any]:
        submission = self.client.get_submission(job_id)
        result: Dict[str, Any] = {"job_id": job_id, "task_id": None, "trial_id": None}
        try:
            task, trial = usage.select_task(submission, trial_id)
        except usage.UsageError as exc:
            if exc.code != "task_not_started":
                raise
            return {**result, "lines": [], "note": str(exc)}
        result["task_id"] = task["task_id"]
        result["trial_id"] = trial["id"] if trial else None
        result["lines"] = self.client.task_logs(task["task_id"], tail)
        return result

    def usage(
        self,
        job_id: str,
        trial_id: Optional[int] = None,
        allocation_id: Optional[str] = None,
        window_seconds: int = 3600,
        metrics: Optional[List[str]] = None,
        include_samples: bool = False,
    ) -> Dict[str, Any]:
        return usage.summarize(
            self.client,
            self.client.get_submission(job_id),
            trial_id=trial_id,
            allocation_id=allocation_id,
            window_seconds=window_seconds,
            metrics=metrics,
            include_samples=include_samples,
        )

    def resources(self, pool: Optional[str] = None) -> Dict[str, Any]:
        """Pools and their device models as the master reports them, with no verdict."""

        pools = self.client.list_resource_pools()
        if pool is not None:
            pools = [item for item in pools if item["name"] == pool]
            if not pools:
                raise APIError(f"pool {pool!r} does not exist", code="not_found")
        agents = self.client.list_agents()
        observed_at = _now()
        projected = []
        for item in pools:
            models = Counter(
                (device["type"], device["brand"])
                for agent in agents
                if item["name"] in agent["resource_pools"]
                for device in agent["devices"]
            )
            device_models = [
                {"type": kind, "brand": brand, "count": count}
                for (kind, brand), count in sorted(models.items(), key=str)
            ]
            projected.append({**item, "device_models": device_models})
        return {"observed_at": observed_at, "pools": projected}

    def storage_check(self, path: str) -> Dict[str, Any]:
        return self.storage.check(path)

    # Control and transfer

    def cancel(self, job_id: str) -> Dict[str, Any]:
        """Cancel a job and wait briefly for it to end; the master records the cancel first."""

        snapshot = self.client.cancel_submission(job_id)
        deadline = self._clock() + self._cancel_wait
        while snapshot["state"] not in ENDED_STATES and self._clock() < deadline:
            self._sleep(self._poll)
            try:
                snapshot = self.client.get_submission(job_id)
            except APIError:
                break  # the cancel is recorded; compute_status reads the rest
        if snapshot["state"] in ENDED_STATES:
            return {**_job(snapshot), "cancel": "ended", "explanation": explain(snapshot)}
        return {
            **_job(snapshot),
            "cancel": "recorded",
            "explanation": "The cancel is recorded and the job ends shortly; compute_status "
            "shows when.",
        }

    def storage_sync(
        self, local_dir: str, shared_dir: str, dry_run: bool = True, overwrite: bool = False
    ) -> Dict[str, Any]:
        return self.storage.sync(local_dir, shared_dir, dry_run, overwrite)

    def storage_fetch(
        self, shared_dir: str, local_dir: str, dry_run: bool = True, overwrite: bool = False
    ) -> Dict[str, Any]:
        return self.storage.fetch(shared_dir, local_dir, dry_run, overwrite)


# The MCP server


def tool_error(exc: BaseException) -> Dict[str, Any]:
    """The JSON error a tool returns: a stable code, the message, and JSON-safe details."""

    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = "invalid_request" if isinstance(exc, ValueError) else "internal_error"
    error: Dict[str, Any] = {
        "code": code,
        "message": str(exc),
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        error["details"] = json.loads(json.dumps(details, default=str))
    return {"error": error}


def create_server(tools: Tools) -> Any:
    """Publish ``tools`` as an MCP server."""

    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised without the optional extra
        raise RuntimeError("MCP support is not installed; install determined-compute[mcp]") from exc

    def envelope(exc: BaseException) -> str:
        return json.dumps(tool_error(exc), sort_keys=True, separators=(",", ":"))

    class Server(MCPServer):
        async def call_tool(self, name: str, arguments: Dict[str, Any], context: Any = None) -> Any:
            # The framework validates the arguments, a TaskSpec included, before a tool runs;
            # its refusal gets the envelope of every other error, without the rejected values.
            try:
                return await super().call_tool(name, arguments, context)
            except ToolError as exc:
                cause = exc.__cause__
                if isinstance(exc, UnexpectedToolError) or not isinstance(cause, ValidationError):
                    raise
                errors = [
                    {"loc": ".".join(map(str, item["loc"])), "message": item["msg"]}
                    for item in cause.errors(
                        include_url=False, include_context=False, include_input=False
                    )
                ]
                refused = ValueError(
                    "; ".join(f"{e['loc'] or 'arguments'}: {e['message']}" for e in errors)
                )
                refused.details = {"errors": errors}  # type: ignore[attr-defined]
                raise ToolError(f"Error executing tool {name}: {envelope(refused)}") from None

    server = Server("determined-compute", version=__version__, instructions=INSTRUCTIONS)

    async def call(operation: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except (APIError, ValueError, OSError) as exc:
            raise ToolError(envelope(exc)) from None

    def hints(read_only: bool, destructive: bool = False, idempotent: bool = True) -> Any:
        return ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=True,
        )

    @server.tool(annotations=hints(read_only=True, idempotent=False))
    async def compute_plan(spec: TaskSpec) -> dict[str, Any]:
        """Check a TaskSpec and dry-run its exact request on the master; nothing is created.

        Returns the resolved spec (revision pinned; pool, slots and image explicit), a new
        request_id, the master's request_digest, the commit and content digest, the effective
        config as observed now, and warnings. Placement is not evaluated.
        """
        return await call(tools.plan, spec)

    @server.tool(annotations=hints(read_only=False))
    async def compute_launch(
        spec: TaskSpec, request_id: str, request_digest: str
    ) -> dict[str, Any]:
        """Launch a planned spec with the plan's request_id and request_digest.

        Returns the job_id, replayed, the outcome, and the job's current state with an
        explanation; an active experiment whose trials wait for resources reads running. A
        retry with the same arguments returns the same job, even when the spec no longer
        renders. If the code or request changed since the plan, nothing is created and
        plan_changed names the new commit and content digest: plan again. An internal error
        may follow a created job: repeat the launch once, which returns it, and if the same
        error comes back, nothing was created.
        """
        return await call(tools.launch, spec, request_id, request_digest)

    @server.tool(annotations=hints(read_only=True))
    async def compute_status(job_id: str) -> dict[str, Any]:
        """Read a job, its tasks and allocations, and an explanation of its state."""
        return await call(tools.status, job_id)

    @server.tool(annotations=hints(read_only=True))
    async def compute_list(
        kind: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """List this user's jobs from every client, newest first, with their request_id.

        kind is command, shell or experiment; state is queued, running, paused, completed,
        failed, canceled or deleted. An active experiment whose trials wait for resources
        reads running, not queued; compute_status explains it. Pass next_cursor as cursor for
        the next page.
        """
        return await call(tools.list, kind, state, limit, cursor)

    @server.tool(annotations=hints(read_only=True))
    async def compute_logs(
        job_id: str, trial_id: Optional[int] = None, tail: int = 200
    ) -> dict[str, Any]:
        """Return the last tail log lines of a job's task, oldest first.

        For an experiment, trial_id selects the trial; the latest trial by default.
        """
        return await call(tools.logs, job_id, trial_id, tail)

    @server.tool(annotations=hints(read_only=True))
    async def compute_usage(
        job_id: str,
        trial_id: Optional[int] = None,
        allocation_id: Optional[str] = None,
        window_seconds: int = 3600,
        metrics: Optional[list[str]] = None,
        include_samples: bool = False,
    ) -> dict[str, Any]:
        """Summarize a job's measured CPU, memory and GPU use; compute_resources shows pools."""
        return await call(
            tools.usage, job_id, trial_id, allocation_id, window_seconds, metrics, include_samples
        )

    @server.tool(annotations=hints(read_only=True))
    async def compute_resources(pool: Optional[str] = None) -> dict[str, Any]:
        """Project the resource pools and their device models; not a placement verdict."""
        return await call(tools.resources, pool)

    @server.tool(annotations=hints(read_only=True))
    async def storage_check(path: str) -> dict[str, Any]:
        """Check a shared container path from this client's viewpoint (local mount or SSH)."""
        return await call(tools.storage_check, path)

    @server.tool(annotations=hints(read_only=False, destructive=True))
    async def compute_cancel(job_id: str) -> dict[str, Any]:
        """Cancel a job; an ended job is returned unchanged."""
        return await call(tools.cancel, job_id)

    @server.tool(annotations=hints(read_only=False, destructive=True, idempotent=False))
    async def storage_sync(
        local_dir: str, shared_dir: str, dry_run: bool = True, overwrite: bool = False
    ) -> dict[str, Any]:
        """Copy a local directory's contents into a shared directory; previews by default.

        Existing files are kept unless overwrite is set and the policy allows it.
        """
        return await call(tools.storage_sync, local_dir, shared_dir, dry_run, overwrite)

    @server.tool(annotations=hints(read_only=False, destructive=True, idempotent=False))
    async def storage_fetch(
        shared_dir: str, local_dir: str, dry_run: bool = True, overwrite: bool = False
    ) -> dict[str, Any]:
        """Copy a shared directory's contents into a local directory; previews by default.

        Existing files are kept unless overwrite is set and the policy allows it.
        """
        return await call(tools.storage_fetch, shared_dir, local_dir, dry_run, overwrite)

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute-mcp",
        description="Serve the Determined compute tools over MCP stdio",
    )
    parser.add_argument("--profile", help="Policy file (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument(
        "--storage-config", help="Storage access file (or DETERMINED_COMPUTE_STORAGE)"
    )
    parser.add_argument("--api-url", help="Determined master URL (defaults to DET_MASTER)")
    parser.add_argument("--api-token", help="Determined API token (defaults to DET_API_TOKEN)")
    parser.add_argument("--secrets-file", help="KEY=VALUE secrets file")
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument("--verify-ssl", action="store_true", dest="verify_ssl")
    verify.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    parser.set_defaults(verify_ssl=None)
    return parser


def build_tools(args: argparse.Namespace) -> Tools:
    """Read the configuration, then pass the master's protocol gate before serving anything.

    A master below the protocol stops startup. An unreachable one does not, so the storage
    tools still serve; the client runs the same gate before its first call to the master.
    """

    profile = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")
    policy = Policy.from_file(profile)
    access_path = args.storage_config or os.environ.get("DETERMINED_COMPUTE_STORAGE")
    access = StorageAccessConfig.from_file(access_path) if access_path else StorageAccessConfig()
    secrets_path = Path(args.secrets_file).expanduser() if args.secrets_file else None
    storage = StorageService(policy, access, secrets_path)
    client = Client(
        api_url=args.api_url,
        api_token=args.api_token,
        secrets_path=secrets_path,
        verify_ssl=args.verify_ssl,
    )
    try:
        client.check_protocol()
    except APIError as exc:
        if exc.code != "unavailable":
            raise
        # stdout carries MCP frames.
        print(
            f"determined-compute-mcp: {exc}; the protocol gate runs on the first master call",
            file=sys.stderr,
        )
    return Tools(client, policy, storage, secrets_path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = create_server(build_tools(args))
    except Exception as exc:
        # stdout carries MCP frames.
        print(f"determined-compute-mcp: {exc}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
