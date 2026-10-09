"""Local stdio MCP adapter for the stateless compute service.

The server keeps no task records. Tools address tasks by Determined's own IDs and
act only on tasks owned by the account whose credentials the process was started
with. It is intended for a trusted same-user MCP client launched as a child process.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional, Sequence, Union

from determined_compute.compute import ComputeError, ComputeProfile, ComputeService
from determined_compute.core.api_client import APIError as ClientAPIError
from determined_compute.core.api_client import DeterminedAPIClient

try:  # pydantic ships with the optional mcp extra; tool annotations need it only then.
    from pydantic import BeforeValidator
except ImportError:  # pragma: no cover - exercised without the optional extra
    BeforeValidator = None


DEFAULT_SHELL_ACCESS_DIR = "~/.cache/determined-compute/shell-access"


def _exact_preference(value: Any) -> Any:
    # Lax validation would turn 0 and 0.0 into False; the request accepts only the bool.
    if value is None or value is False or isinstance(value, str):
        return value
    raise ValueError('prefer_gpu_topology must be "soft", "strong", false, or null')


class _LazyClient:
    """Construct the API client only when a service operation needs it."""

    def __init__(self, factory: Callable[[], DeterminedAPIClient]) -> None:
        import threading

        self._factory = factory
        self._client: Optional[DeterminedAPIClient] = None
        self._lock = threading.Lock()

    def _resolve_client(self) -> DeterminedAPIClient:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    client = self._factory()
                    self._client = client
        return self._client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve_client(), name)


def safe_error_details(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    allowed = {
        "kind", "submission_marker", "source", "status_code", "proxy_error", "resource_pool",
        "requested_slots", "available", "candidate_pools",
    }
    return {key: value for key, value in details.items() if key in allowed} if isinstance(details, dict) else {}


def _tool_error(exc: BaseException) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": getattr(exc, "code", "internal_error"),
        "message": str(exc),
    }
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        error["retryable"] = bool(retryable)
    details = safe_error_details(exc)
    if details:
        error["details"] = details
    return {"error": error}


def create_server(
    service: ComputeService,
    storage_service: Any = None,
    resource_inspector: Any = None,
    shell_access: Any = None,
) -> Any:
    """Create an MCP server for the compute service, optional storage access, and optional
    local SSH access to shells."""

    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised without the optional extra
        raise RuntimeError(
            "MCP support is not installed; install determined-compute[mcp]"
        ) from exc

    server = MCPServer(
        "determined-compute",
        instructions=(
            "Choose a meaningful request.name and request.description for each launch. "
            "Use compute_resources for capacity questions. Launch checks capacity unless "
            "queuing is explicitly authorized with allow_queue=true. "
            "For multi-GPU work, pass the same prefer_gpu_topology to compute_resources and "
            "the request. "
            "Keep code and data on shared mounts; use storage_check/sync/fetch for file access. "
            "Plan before launch. Every launch is a new submission: the server keeps no task "
            "records, so keep the returned kind and id, which are Determined's own task ID. "
            "If a launch is unconfirmed, look for it with compute_list(kind, marker=...); an "
            "empty result does not prove it failed, so never launch again automatically: "
            "resubmitting is the user's decision. Use compute_list to find the account's tasks "
            "(compute_list(kind, states=['STATE_ACTIVE']) lists the active experiments or "
            "generic tasks, queued or running, one page at a time; follow pagination.next_offset "
            "until it is null) "
            "and compute_usage to check a task's measured CPU, memory, and GPU use. "
            "compute_status gives an unended task's queue position (queue.jobs_ahead) and "
            "placement; a null queue does not mean the task is not queued. "
            "Experiments and generic tasks can be paused and resumed; a resumed experiment "
            "continues from its trials' latest checkpoints, a resumed generic task reruns its "
            "command from the start. "
            "compute_shell_connect opens local SSH access to a running shell (127.0.0.1, a "
            "port, a key file, and a pinned host key): run its ssh_command where a local shell "
            "tool is allowed, or use an SSH MCP server such as ssh-mcp, which must be started "
            "or reconnected after each connect; disconnect when done. "
            "Credentials belong in local configuration, never in tool arguments."
        ),
    )

    def fail(exc: BaseException) -> None:
        raise ToolError(json.dumps(_tool_error(exc), sort_keys=True, separators=(",", ":")))

    async def call(operation: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except (ComputeError, ClientAPIError) as exc:
            fail(exc)
        except (OSError, ValueError) as exc:
            fail(exc)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
    ))
    async def compute_plan(request: dict[str, Any]) -> dict[str, Any]:
        """Render a request offline. Include name, description, command, workdir and output_dir.

        Optional kind, slots, pool, prefer_gpu_topology, image and allow_queue control execution;
        paths use container mounts.
        """

        return await call(service.plan, request)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True,
    ))
    async def compute_launch(request: dict[str, Any]) -> dict[str, Any]:
        """Submit a request once and return its kind and Determined id; checks capacity unless allow_queue=true.

        Every call is a new submission. On an unconfirmed outcome, look for the task with
        compute_list and the returned marker, and leave any resubmission to the user.
        """

        return await call(service.launch, request)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_status(kind: str, id: Union[int, str]) -> dict[str, Any]:
        """Return the current state of one of the account's tasks by kind and Determined id.

        For a task that has not ended, queue is its job in the pool's queue (state,
        jobs_ahead, slots, placement), or null with queue_note or context_unavailable.
        An ended task (end time set, or a command or shell in STATE_TERMINATED) gets
        queue: null.
        """

        return await call(service.status, kind, id)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_logs(kind: str, id: Union[int, str], tail: int = 200) -> list[Any]:
        """Return a task's latest log records; tail must be a positive integer."""

        if tail < 1:
            fail(ValueError("tail must be at least 1"))
        return await call(service.logs, kind, id, tail)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_usage(
        kind: str,
        id: Union[int, str],
        window_seconds: int = 3600,
        allocation_id: Optional[str] = None,
        trial_id: Optional[int] = None,
        metrics: Optional[list[str]] = None,
        include_samples: bool = False,
    ) -> dict[str, Any]:
        """Summarize one task's measured CPU, memory, and GPU use; compute_resources is cluster capacity."""

        return await call(
            service.usage, kind, id, window_seconds, allocation_id, trial_id,
            metrics, include_samples,
        )

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_cancel(kind: str, id: Union[int, str]) -> dict[str, Any]:
        """Cancel one of the account's tasks; a generic task's descendants are killed with it."""

        result = await call(service.cancel, kind, id)
        if kind == "shell" and shell_access is not None:
            # The shell is going away, so its local tunnel and key file go too.
            try:
                closed = await asyncio.to_thread(shell_access.disconnect, id)
                result["shell_access_closed"] = closed["disconnected"]
            except Exception:
                result["shell_access_closed"] = None
        return result

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_pause(kind: str, id: Union[int, str]) -> dict[str, Any]:
        """Pause an experiment, or a generic task and its pausable descendants."""

        return await call(service.pause, kind, id)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_resume(kind: str, id: Union[int, str]) -> dict[str, Any]:
        """Resume a paused experiment or generic task."""

        return await call(service.resume, kind, id)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_list(
        kind: str,
        limit: int = 50,
        offset: int = 0,
        marker: Optional[str] = None,
        states: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """List one page of the account's tasks of one kind, newest first.

        With states (experiments and generic tasks only), list only tasks in those states;
        STATE_ACTIVE covers experiments shown as QUEUED, PULLING, STARTING or RUNNING.
        With marker, return the tasks on that page (one read each) whose config carries it.
        """

        return await call(service.list_tasks, kind, limit, offset, marker, states)

    if resource_inspector is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def compute_resources(
            slots: int = 1,
            pool: Optional[str] = None,
            prefer_gpu_topology: Annotated[
                Optional[Literal["soft", "strong", False]], BeforeValidator(_exact_preference)
            ] = None,
        ) -> dict[str, Any]:
            """Inspect current scheduler capacity; slots=0 checks auxiliary capacity, not free GPUs.

            Each pool has description (null when the pool has none) and gpu_models (null when
            unknown). Pass the request's prefer_gpu_topology; with "strong" and 2 or more slots
            each pool adds max_numa_node_free_slots and max_numa_node_slots.
            """
            return await call(resource_inspector.resources, slots, pool, prefer_gpu_topology)

    if shell_access is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def compute_shell_connect(id: str, local_port: Optional[int] = None) -> dict[str, Any]:
            """Open local SSH access to one of the account's running shells.

            Listens on 127.0.0.1 (local_port, or a free port) and relays to the shell through
            the master. Returns port, user, key_path, host_key_fingerprint, ssh_command, and an
            ssh-mcp profile in a generated config; the private key stays in key_path. Calling
            it again returns the open tunnel; another local_port needs a disconnect first.
            """
            return await call(shell_access.connect, id, local_port)

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
        ))
        async def compute_shell_disconnect(id: str) -> dict[str, Any]:
            """Close a shell's local SSH tunnel and delete its key file; the shell keeps running."""
            return await call(shell_access.disconnect, id)

    if storage_service is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def storage_check(path: str) -> dict[str, Any]:
            """Check a shared container path through a local mount or configured SSH login node."""
            return await call(storage_service.check, path)

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
        ))
        async def storage_sync(local_dir: str, shared_dir: str, dry_run: bool = True) -> dict[str, Any]:
            """Copy local directory contents to a mapped shared directory; preview by default, no deletions."""
            return await call(storage_service.sync, local_dir, shared_dir, dry_run)

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
        ))
        async def storage_fetch(shared_dir: str, local_dir: str, dry_run: bool = True) -> dict[str, Any]:
            """Copy shared directory contents to a local directory; preview by default, no deletions."""
            return await call(storage_service.fetch, shared_dir, local_dir, dry_run)

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute-mcp",
        description="Run the trusted local Determined compute MCP server over stdio",
    )
    parser.add_argument("--profile", help="Compute profile YAML (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument("--storage-config", help="Client storage access YAML (or DETERMINED_COMPUTE_STORAGE)")
    parser.add_argument(
        "--api-url",
        help="Determined master URL (defaults to the secrets file's DET_MASTER, else DET_MASTER); "
        "must match a master the secrets file names",
    )
    parser.add_argument(
        "--api-token",
        help="Determined API token for the selected master; replaces any other token or login",
    )
    parser.add_argument("--secrets-file", help="Path to a KEY=VALUE secrets file")
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument("--verify-ssl", action="store_true", dest="verify_ssl")
    verify.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    parser.set_defaults(verify_ssl=None)
    parser.add_argument(
        "--shell-access-dir",
        help="Private directory for shell tunnel keys and the generated ssh-mcp config "
        f"(or DETERMINED_COMPUTE_SHELL_ACCESS; default {DEFAULT_SHELL_ACCESS_DIR})",
    )
    return parser


def _runtime(args: argparse.Namespace) -> Any:
    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")
    profile = ComputeProfile.from_file(profile_path)

    def make_client() -> DeterminedAPIClient:
        return DeterminedAPIClient(
            api_url=args.api_url,
            api_token=args.api_token,
            secrets_path=Path(args.secrets_file) if args.secrets_file else None,
            verify_ssl=args.verify_ssl,
        )

    service = ComputeService(_LazyClient(make_client), profile)

    from determined_compute.storage import StorageAccessConfig, StorageService
    access_path = args.storage_config or os.environ.get("DETERMINED_COMPUTE_STORAGE")
    access = StorageAccessConfig.from_file(access_path) if access_path else StorageAccessConfig()
    storage = StorageService(profile, access, Path(args.secrets_file).expanduser() if args.secrets_file else None)

    from determined_compute.compute.shell_access import ShellAccess
    shell_dir = (
        args.shell_access_dir
        or os.environ.get("DETERMINED_COMPUTE_SHELL_ACCESS")
        or DEFAULT_SHELL_ACCESS_DIR
    )
    shell_access = ShellAccess(service, Path(shell_dir).expanduser())
    try:
        shell_access.sweep()
    except (OSError, ValueError, ComputeError) as exc:
        # Shell access is optional; report a bad directory, and let connect fail on it later.
        print(f"determined-compute-mcp: shell access directory: {exc}", file=os.sys.stderr)
    atexit.register(shell_access.close_all)

    from determined_compute.compute.admission import ResourceInspector
    return create_server(service, storage, ResourceInspector(service.client), shell_access)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = _runtime(args)
    except Exception as exc:
        # stdout is reserved for MCP frames.
        print(f"determined-compute-mcp: {exc}", file=os.sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
