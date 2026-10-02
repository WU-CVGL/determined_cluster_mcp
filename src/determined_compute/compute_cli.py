"""JSON command-line interface for persistent Determined compute tasks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import yaml

from determined_compute.compute import ComputeError, ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.core.api_client import APIError as ClientAPIError
from determined_compute.core.api_client import DeterminedAPIClient


DEFAULT_DB_PATH = Path("~/.local/state/determined-compute/tasks.sqlite3").expanduser()


def normalize_owner(value: str) -> str:
    """Use the same startup namespace for tasks and consultation workflows."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("owner must be a non-empty string")
    value = value.strip()
    if len(value.encode("utf-8")) > 256:
        raise ValueError("owner is too long")
    return value


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


def _json_dump(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def safe_error_details(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    allowed = {"task_id", "resource_pool", "requested_slots", "available", "candidate_pools"}
    result = {key: value for key, value in details.items() if key in allowed} if isinstance(details, dict) else {}
    if "task_id" not in result and getattr(exc, "task_id", None):
        result["task_id"] = str(exc.task_id)
    return result


def _error_payload(exc: BaseException) -> dict[str, Any]:
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
    return {"ok": False, "error": error}


def _success_payload(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _load_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_file:
        if args.request_file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.request_file).read_text(encoding="utf-8")
    else:
        raw = args.request
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError("compute request must be a JSON object")
    return value


def _client_factory(args: argparse.Namespace) -> DeterminedAPIClient:
    return DeterminedAPIClient(
        api_url=args.api_url,
        api_token=args.api_token,
        secrets_path=Path(args.secrets_file) if args.secrets_file else None,
        verify_ssl=args.verify_ssl,
    )


def _resolve_runtime(args: argparse.Namespace) -> tuple[ComputeService, str]:
    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")

    profile = ComputeProfile.from_file(profile_path)
    client = _LazyClient(lambda: _client_factory(args))
    if args.command == "plan":
        return ComputeService(client, SQLiteTaskStore(":memory:"), profile), ""

    db_path = Path(args.db or os.environ.get("DETERMINED_COMPUTE_DB") or DEFAULT_DB_PATH)
    if db_path == Path(":memory:"):
        raise ValueError("Task management requires a persistent local database; :memory: is unsupported")
    owner = args.owner or os.environ.get("DETERMINED_COMPUTE_OWNER")
    if not owner:
        raise ValueError("--owner or DETERMINED_COMPUTE_OWNER is required")
    owner = normalize_owner(owner)
    if db_path != Path(":memory:"):
        db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
        db_path = db_path.expanduser()
    store = SQLiteTaskStore(db_path)
    return ComputeService(client, store, profile), owner


def _resolve_storage(args: argparse.Namespace) -> Any:
    from determined_compute.storage import StorageAccessConfig, StorageService

    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")
    access_path = args.storage_config or os.environ.get("DETERMINED_COMPUTE_STORAGE")
    access = StorageAccessConfig.from_file(access_path) if access_path else StorageAccessConfig()
    secrets_path = Path(args.secrets_file).expanduser() if args.secrets_file else None
    return StorageService(ComputeProfile.from_file(profile_path), access, secrets_path)


def _dispatch_storage(args: argparse.Namespace) -> Any:
    service = _resolve_storage(args)
    if args.command == "storage-check":
        return service.check(args.path)
    if args.command == "storage-sync":
        return service.sync(args.local_dir, args.shared_dir, dry_run=not args.execute)
    return service.fetch(args.shared_dir, args.local_dir, dry_run=not args.execute)


def _add_request_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", help="Compute request as a JSON or YAML object")
    source.add_argument(
        "--request-file",
        metavar="PATH",
        help="Read a JSON/YAML compute request from PATH, or '-' for stdin",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute",
        description="Plan and manage persistent Determined compute tasks",
    )
    parser.add_argument("--profile", help="Compute profile YAML (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument("--storage-config", help="Client storage access YAML (or DETERMINED_COMPUTE_STORAGE)")
    parser.add_argument("--db", help="Shared SQLite task database (or DETERMINED_COMPUTE_DB)")
    parser.add_argument(
        "--owner",
        help="Local owner namespace (or DETERMINED_COMPUTE_OWNER)",
    )
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

    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Validate and render a request without cluster access")
    _add_request_args(plan)

    launch = commands.add_parser("launch", help="Launch a request idempotently")
    _add_request_args(launch)
    launch.add_argument("--request-id", required=True, help="Caller-generated idempotency key")

    status = commands.add_parser("status", help="Refresh and show one task")
    status.add_argument("task_id")

    logs = commands.add_parser("logs", help="Fetch the tail of one task's logs")
    logs.add_argument("task_id")
    logs.add_argument("--tail", type=int, default=200)

    usage = commands.add_parser(
        "usage", help="Summarize one task's measured CPU, memory, and GPU use"
    )
    usage.add_argument("task_id")
    usage.add_argument("--window-seconds", type=int, default=3600)
    usage.add_argument("--allocation-id", help="Restrict results to one allocation of the task")
    usage.add_argument("--trial-id", type=int, help="Experiment trial to inspect (default: latest)")
    usage.add_argument(
        "--metric", action="append", dest="metrics", help="Report only this metric (repeatable)"
    )
    usage.add_argument(
        "--samples", action="store_true", dest="include_samples", help="Include raw samples"
    )

    cancel = commands.add_parser("cancel", help="Cancel one task")
    cancel.add_argument("task_id")

    reconcile = commands.add_parser(
        "reconcile", help="Bind an uncertain task to a verified remote task id"
    )
    reconcile.add_argument("task_id")
    reconcile.add_argument("remote_id")

    discover = commands.add_parser(
        "discover",
        help="Discover existing remote tasks of one kind without registering or submitting them",
    )
    discover.add_argument("kind", choices=("command", "shell", "experiment"))
    discover.add_argument("--limit", type=int, default=50)
    discover.add_argument("--offset", type=int, default=0)

    adopt = commands.add_parser(
        "adopt",
        help="Register an existing remote task locally without submitting new work",
    )
    adopt.add_argument("kind", choices=("command", "shell", "experiment"))
    adopt.add_argument("remote_id")

    commands.add_parser("list", help="List tasks in the bound owner namespace")
    resources = commands.add_parser("resources", help="Inspect current cluster scheduling capacity")
    resources.add_argument("--slots", type=int, default=1, help="Required slots; zero checks auxiliary capacity")
    resources.add_argument("--pool", help="Inspect one resource pool")

    check = commands.add_parser("storage-check", help="Check a mapped shared path locally or through SSH")
    check.add_argument("path", help="Shared path in the container namespace")
    sync = commands.add_parser("storage-sync", help="Preview copying local directory contents to shared storage")
    sync.add_argument("local_dir")
    sync.add_argument("shared_dir", help="Destination directory in the container namespace")
    sync.add_argument("--execute", action="store_true", help="Perform the transfer instead of previewing")
    fetch = commands.add_parser("storage-fetch", help="Preview copying shared directory contents to local storage")
    fetch.add_argument("shared_dir", help="Source directory in the container namespace")
    fetch.add_argument("local_dir")
    fetch.add_argument("--execute", action="store_true", help="Perform the transfer instead of previewing")
    return parser


def _dispatch(args: argparse.Namespace, service: ComputeService, owner: str) -> Any:
    if args.command == "plan":
        return service.plan(_load_request(args))
    if args.command == "launch":
        return service.launch(_load_request(args), args.request_id, owner)
    if args.command == "status":
        return service.status(args.task_id, owner)
    if args.command == "logs":
        if args.tail < 1:
            raise ValueError("--tail must be at least 1")
        return service.logs(args.task_id, owner, args.tail)
    if args.command == "usage":
        return service.usage(
            args.task_id, owner, args.window_seconds, args.allocation_id, args.trial_id,
            args.metrics, args.include_samples,
        )
    if args.command == "cancel":
        return service.cancel(args.task_id, owner)
    if args.command == "reconcile":
        return service.reconcile(args.task_id, owner, args.remote_id)
    if args.command == "discover":
        return service.discover(args.kind, owner, args.limit, args.offset)
    if args.command == "adopt":
        return service.adopt(args.kind, args.remote_id, owner)
    if args.command == "list":
        return service.list_tasks(owner)
    raise ValueError(f"unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "resources":
            from determined_compute.compute.admission import ResourceInspector
            _json_dump(_success_payload(ResourceInspector(_client_factory(args)).resources(args.slots, args.pool)))
            return 0
        if args.command in {"storage-check", "storage-sync", "storage-fetch"}:
            _json_dump(_success_payload(_dispatch_storage(args)))
            return 0
        service, owner = _resolve_runtime(args)
        _json_dump(_success_payload(_dispatch(args, service, owner)))
        return 0
    except (ComputeError, ClientAPIError, OSError, ValueError, yaml.YAMLError) as exc:
        _json_dump(_error_payload(exc))
        return 2
    except Exception as exc:  # keep stdout machine-readable at the process boundary
        _json_dump(_error_payload(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
