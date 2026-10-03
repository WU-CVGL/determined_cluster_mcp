"""Local stdio MCP adapter for the persistent compute service.

The configured owner is a local namespace, not an authentication mechanism.
This server is intended for a trusted same-user MCP client launched as a child
process.  No tool accepts an owner argument.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from determined_compute.compute import ComputeError, ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.compute_cli import DEFAULT_DB_PATH, _LazyClient, normalize_owner, safe_error_details
from determined_compute.core.api_client import APIError as ClientAPIError
from determined_compute.core.api_client import DeterminedAPIClient


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
    owner: str,
    workflow_manager: Any = None,
    storage_service: Any = None,
    resource_inspector: Any = None,
) -> Any:
    """Create an MCP server bound to one local owner namespace."""

    owner = normalize_owner(owner)
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
            "Check capacity with compute_resources; queuing requires explicit allow_queue=true. "
            "Keep code and data on shared mounts; use storage_check/sync/fetch for file access. "
            "Plan before launch, keep request_id stable, and use the returned task_id for control. "
            "Use compute_usage to check a task's measured CPU, memory, and GPU use. "
            "Only generic tasks can be paused and resumed; resuming reruns the command "
            "from the start, so make it restart-safe. "
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

        Optional kind, slots, pool, image and allow_queue control execution; paths use container mounts.
        """

        return await call(service.plan, request)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_launch(request: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Launch a named request idempotently, checking capacity unless allow_queue=true."""

        return await call(service.launch, request, request_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_status(task_id: str) -> dict[str, Any]:
        """Refresh and return a task in the server's owner namespace."""

        return await call(service.status, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_logs(task_id: str, tail: int = 200) -> list[Any]:
        """Return the latest task log records; tail must be a positive integer."""

        if tail < 1:
            fail(ValueError("tail must be at least 1"))
        return await call(service.logs, task_id, owner, tail)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_usage(
        task_id: str,
        window_seconds: int = 3600,
        allocation_id: Optional[str] = None,
        trial_id: Optional[int] = None,
        metrics: Optional[list[str]] = None,
        include_samples: bool = False,
    ) -> dict[str, Any]:
        """Summarize one task's measured CPU, memory, and GPU use; compute_resources is cluster capacity."""

        return await call(
            service.usage, task_id, owner, window_seconds, allocation_id, trial_id,
            metrics, include_samples,
        )

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_cancel(task_id: str) -> dict[str, Any]:
        """Cancel a task in the server's owner namespace."""

        return await call(service.cancel, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_pause(task_id: str) -> dict[str, Any]:
        """Pause a generic task and its pausable descendants; their containers stop."""

        return await call(service.pause, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_resume(task_id: str) -> dict[str, Any]:
        """Resume a paused generic task; its command runs again from the start."""

        return await call(service.resume, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_reconcile(task_id: str, remote_id: str) -> dict[str, Any]:
        """Bind an uncertain task to a remote id after verifying its identity marker."""

        return await call(service.reconcile, task_id, owner, remote_id)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
    ))
    async def compute_list_tasks() -> list[dict[str, Any]]:
        """List tasks in the server's owner namespace."""

        return await call(service.list_tasks, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_discover(
        kind: str, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Discover one kind of remote task with bounded pagination; this does not adopt it."""

        return await call(service.discover, kind, owner, limit, offset)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_adopt(kind: str, remote_id: str) -> dict[str, Any]:
        """Register an existing remote task in the local owner namespace without submitting work."""

        return await call(service.adopt, kind, remote_id, owner)

    if resource_inspector is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def compute_resources(slots: int = 1, pool: Optional[str] = None) -> dict[str, Any]:
            """Inspect current scheduler capacity; slots=0 checks auxiliary capacity, not free GPUs."""
            return await call(resource_inspector.resources, slots, pool)

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

    if workflow_manager is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def compute_consult(
            question: str,
            request_id: str,
            context: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            """Queue a read-only consultation for this repository."""

            try:
                return await asyncio.to_thread(
                    workflow_manager.submit, question, owner, request_id, context
                )
            except (OSError, ValueError) as exc:
                fail(exc)
            except Exception as exc:
                if getattr(exc, "code", None) == "workflow_conflict":
                    fail(exc)
                raise

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
        ))
        async def workflow_status(workflow_id: str) -> dict[str, Any]:
            """Return persisted status and logs for one consultation workflow."""

            try:
                return await asyncio.to_thread(workflow_manager.status, workflow_id, owner)
            except (OSError, ValueError) as exc:
                fail(exc)
            except Exception as exc:
                if getattr(exc, "code", None) == "workflow_not_found":
                    fail(exc)
                raise

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute-mcp",
        description="Run the trusted local Determined compute MCP server over stdio",
    )
    parser.add_argument("--profile", help="Compute profile YAML (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument("--storage-config", help="Client storage access YAML (or DETERMINED_COMPUTE_STORAGE)")
    parser.add_argument("--db", help="Shared SQLite database (or DETERMINED_COMPUTE_DB)")
    parser.add_argument(
        "--owner",
        help="Bound owner namespace (or DETERMINED_COMPUTE_OWNER)",
    )
    parser.add_argument(
        "--repo-root",
        help="Optional repository root for the configured consultation backend",
    )
    parser.add_argument(
        "--consultation-backend",
        choices=("none", "codex"),
        default="none",
        help="Optional repository consultation backend (default: none)",
    )
    parser.add_argument(
        "--consultation-model",
        help="Model for the optional Codex consultation backend",
    )
    parser.add_argument(
        "--consultation-codex-bin",
        help="Codex executable for the consultation backend (default: codex)",
    )
    parser.add_argument("--api-url", help="Determined master URL (defaults to DET_MASTER)")
    parser.add_argument("--api-token", help="Determined API token (defaults to DET_API_TOKEN)")
    parser.add_argument("--secrets-file", help="Path to a KEY=VALUE secrets file")
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument("--verify-ssl", action="store_true", dest="verify_ssl")
    verify.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    parser.set_defaults(verify_ssl=None)
    return parser


def _runtime(args: argparse.Namespace) -> tuple[Any, str]:
    if args.consultation_backend == "none":
        consultation_options = [
            name
            for name, value in (
                ("--consultation-model", args.consultation_model),
                ("--consultation-codex-bin", args.consultation_codex_bin),
            )
            if value is not None
        ]
    else:
        consultation_options = []
    if consultation_options:
        raise ValueError(
            f"{', '.join(consultation_options)} require --consultation-backend codex"
        )

    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")

    db_path = Path(args.db or os.environ.get("DETERMINED_COMPUTE_DB") or DEFAULT_DB_PATH)
    if db_path == Path(":memory:"):
        raise ValueError("MCP requires a persistent local database; :memory: is unsupported")
    owner = args.owner or os.environ.get("DETERMINED_COMPUTE_OWNER")
    if not owner:
        raise ValueError("--owner or DETERMINED_COMPUTE_OWNER is required")
    owner = normalize_owner(owner)
    if db_path != Path(":memory:"):
        db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
        db_path = db_path.expanduser()
    profile = ComputeProfile.from_file(profile_path)
    store = SQLiteTaskStore(db_path)

    def make_client() -> DeterminedAPIClient:
        return DeterminedAPIClient(
            api_url=args.api_url,
            api_token=args.api_token,
            secrets_path=Path(args.secrets_file) if args.secrets_file else None,
            verify_ssl=args.verify_ssl,
        )

    service = ComputeService(_LazyClient(make_client), store, profile)

    from determined_compute.storage import StorageAccessConfig, StorageService
    access_path = args.storage_config or os.environ.get("DETERMINED_COMPUTE_STORAGE")
    access = StorageAccessConfig.from_file(access_path) if access_path else StorageAccessConfig()
    storage = StorageService(profile, access, Path(args.secrets_file).expanduser() if args.secrets_file else None)

    workflow_manager = None
    if args.consultation_backend == "codex":
        from determined_compute.agent_worker import WorkflowManager

        repo_root = Path(
            args.repo_root or os.environ.get("DETERMINED_COMPUTE_REPO_ROOT") or os.getcwd()
        ).resolve()
        workflow_options: dict[str, Any] = {}
        if args.consultation_model is not None:
            workflow_options["model"] = args.consultation_model
        if args.consultation_codex_bin is not None:
            workflow_options["codex_bin"] = args.consultation_codex_bin
        workflow_manager = WorkflowManager(db_path, repo_root, **workflow_options)
    from determined_compute.compute.admission import ResourceInspector
    return create_server(service, owner, workflow_manager, storage, ResourceInspector(service.client)), owner


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server, _owner = _runtime(args)
    except Exception as exc:
        # stdout is reserved for MCP frames.
        print(f"determined-compute-mcp: {exc}", file=os.sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
