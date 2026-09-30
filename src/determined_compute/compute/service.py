"""Transport-independent planning and persistent compute task orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import posixpath
import re
import shlex
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from determined_compute.core.api_client import DeterminedAPIClient

from . import gpu_admission
from .models import (
    APIError,
    ConflictError,
    NotFoundError,
    SubmissionUncertainError,
    TaskRecord,
    ValidationError,
)
from .profile import ComputeProfile
from .store import SQLiteTaskStore


_REQUEST_FIELDS = {
    "kind",
    "name",
    "description",
    "allow_queue",
    "interactive",
    "overnight",
    "command",
    "workdir",
    "output_dir",
    "slots",
    "pool",
    "image",
    "code_revision",
    "experiment_config",
    "create_directories",
    "gpu_admission",
}
_CREATE_DIRECTORY_FIELDS = ("checkpoint_storage", "output_dir")
_MANAGED_VARIABLES = frozenset(
    {
        "COMPUTE_WORKDIR",
        "COMPUTE_OUTPUT_DIR",
        "COMPUTE_CODE_REVISION",
        "COMPUTE_SUBMISSION_MARKER",
        *gpu_admission.ENVIRONMENT_NAMES,
    }
)
_CROSS_PROFILE_MESSAGE = (
    "cancel, reconcile and launch retries require the task's original compute profile"
)
_NAME_MAX_LENGTH = 128
_DESCRIPTION_MAX_LENGTH = 2048
_SUBMISSION_MARKER_VARIABLE = "COMPUTE_SUBMISSION_MARKER"
_FORBIDDEN_FIELDS = {
    "context",
    "contextdir",
    "contextpath",
    "files",
    "includes",
    "modeldefinition",
    "projectroot",
    "upload",
    "uploadcontext",
    "uploads",
}


# Units of the fixed metric names served by the task resources API.
_USAGE_METRICS = {
    "allocation_active": "count",
    "cpu_cores": "cores",
    "memory_working_set_bytes": "bytes",
    "memory_rss_bytes": "bytes",
    "gpu_utilization_percent": "percent",
    "gpu_memory_used_bytes": "bytes",
    "gpu_power_watts": "watts",
    "gpu_temperature_celsius": "celsius",
}
# Server limits: at most seven days, a 15-second step, and 1,440 points per series.
_USAGE_MIN_WINDOW_SECONDS = 60
_USAGE_MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
_USAGE_MIN_STEP_SECONDS = 15
_USAGE_MAX_POINTS = 1440
_USAGE_MAX_RETURNED_SAMPLES = 2880
# Context lookups are bounded: allocation details for the newest allocations, and a
# fixed number of trial summary metrics.
_USAGE_MAX_ALLOCATION_DETAILS = 8
_USAGE_MAX_SUMMARY_METRICS = 100
_USAGE_SUMMARY_STATISTICS = ("count", "sum", "min", "max", "last", "mean")
# A GPU utilization sample below this percentage counts as idle.
_GPU_IDLE_PERCENT = 10
_USAGE_ADVISORY = (
    "Values are point samples taken every step seconds, so min, max, and mean describe "
    "those samples rather than every moment of the window. A missing value means no "
    "measurement, not zero use. GPU metrics describe each whole assigned device and may "
    "include other processes. allocation_active above zero means the allocation was running. "
    f"idle_fraction is the share of GPU utilization samples below {_GPU_IDLE_PERCENT}%, and "
    "p50 and p95 are nearest-rank percentiles of the available samples. "
    "Coverage depends on the cluster's monitoring retention."
)
# Sentinels for the lazily resolved cluster identity used by list_tasks.
_UNRESOLVED = object()
_UNKNOWN = object()
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?"
)


def _normalized_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _reject_upload_fields(value: Any, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_FIELDS:
                raise ValidationError(
                    f"{path}.{key} is forbidden; compute tasks use shared mounts only"
                )
            _reject_upload_fields(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_upload_fields(nested, f"{path}[{index}]")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _display_name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ValidationError(f"{field} must not be empty")
    if len(result) > _NAME_MAX_LENGTH:
        raise ValidationError(f"{field} must be at most {_NAME_MAX_LENGTH} characters")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in result
    ):
        raise ValidationError(f"{field} must not contain control characters")
    return result


def _display_description(value: Any, field: str = "description") -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string or null")
    result = value.strip()
    if not result:
        return None
    if len(result) > _DESCRIPTION_MAX_LENGTH:
        raise ValidationError(
            f"{field} must be at most {_DESCRIPTION_MAX_LENGTH} characters"
        )
    if any(
        unicodedata.category(character).startswith("C")
        and character not in {"\n", "\t"}
        for character in result
    ):
        raise ValidationError(f"{field} contains an unsupported control character")
    return result


def _remote_id(entity: Any) -> str:
    if not isinstance(entity, Mapping):
        raise SubmissionUncertainError("launch response was not an object")
    value = entity.get("id")
    if value is None:
        raise SubmissionUncertainError("launch response did not contain a remote task id")
    return str(value)


def _unix_seconds(value: Optional[str]) -> Optional[int]:
    """Floor an RFC 3339 timestamp to Unix seconds; a missing offset means UTC."""
    if value is None:
        return None
    match = _TIMESTAMP.fullmatch(value)
    try:
        if match is None:
            raise ValueError(value)
        base, offset = match.groups()
        parsed = datetime.fromisoformat(base + ("+00:00" if offset in {None, "Z"} else offset))
    except ValueError as exc:
        raise APIError("Task response contained an invalid timestamp", code="invalid_response") from exc
    return int(parsed.timestamp())


def _finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _percentile(ordered: Sequence[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile of already sorted values."""
    if not ordered:
        return None
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _idle_fraction(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(value < _GPU_IDLE_PERCENT for value in values) / len(values), 6)


def _lenient_unix_seconds(value: Optional[str]) -> Optional[int]:
    try:
        return _unix_seconds(value)
    except APIError:
        return None


def _utc_text(value: Optional[str]) -> Optional[str]:
    """Mark an offset-less allocation time, stored by Determined in UTC, as UTC."""
    if value is None:
        return None
    match = _TIMESTAMP.fullmatch(value)
    return value + "Z" if match is not None and match.group(2) is None else value


def _iso_seconds(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _remote_state(entity: Any) -> Optional[str]:
    if not isinstance(entity, Mapping):
        return None
    value = entity.get("state") or entity.get("status")
    if value is None:
        return None
    state = str(value)
    return state if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", state) else None


class ComputeService:
    """Plan and launch tasks while preserving local ownership and idempotency."""

    def __init__(
        self,
        client: Any,
        store: SQLiteTaskStore,
        profile: ComputeProfile,
        submission_stale_seconds: int = 300,
        inspector: Any = None,
        path_inspector: Any = None,
    ) -> None:
        if (
            isinstance(submission_stale_seconds, bool)
            or not isinstance(submission_stale_seconds, int)
            or submission_stale_seconds < 0
        ):
            raise ValueError("submission_stale_seconds must be a non-negative integer")
        self.client = client
        self.store = store
        self.profile = profile
        self.submission_stale_seconds = submission_stale_seconds
        self.inspector = inspector
        # Optional local view of shared storage used to check and create launch paths.
        self.path_inspector = path_inspector

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize a request without contacting Determined.

        With a path inspector, the result also reports observational ``path_checks``
        and rejects a required launch path that is known to be missing.
        """

        rendered = self._render(request)
        if self.path_inspector is None:
            return rendered
        result = dict(rendered)
        result["path_checks"] = self._check_paths(rendered, request)
        return result

    def _render(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and render a request; this pure result is the idempotency payload."""

        if not isinstance(request, Mapping):
            raise ValidationError("request must be an object")
        unknown = set(request) - _REQUEST_FIELDS
        if unknown:
            raise ValidationError(f"request has unknown fields: {sorted(unknown)}")
        _reject_upload_fields(request)

        raw_kind = request.get("kind", "auto")
        if raw_kind not in {"auto", "command", "shell", "experiment"}:
            raise ValidationError("kind must be auto, command, shell, or experiment")
        interactive = request.get("interactive", False)
        overnight = request.get("overnight", False)
        allow_queue = request.get("allow_queue", False)
        if (
            not isinstance(interactive, bool)
            or not isinstance(overnight, bool)
            or not isinstance(allow_queue, bool)
        ):
            raise ValidationError(
                "interactive, overnight, and allow_queue must be booleans"
            )
        experiment_config = request.get("experiment_config")
        if experiment_config is not None and not isinstance(experiment_config, Mapping):
            raise ValidationError("experiment_config must be an object")

        if raw_kind == "auto":
            if interactive:
                kind = "shell"
            elif overnight or experiment_config is not None:
                kind = "experiment"
            else:
                kind = "command"
        else:
            kind = raw_kind
        if interactive and kind != "shell":
            raise ValidationError("interactive work must use a shell")
        if experiment_config is not None and kind != "experiment":
            raise ValidationError("experiment_config requires experiment kind")

        workdir = self.profile.validate_writable_container_path(
            request.get("workdir"), "workdir"
        )
        output_dir = self.profile.validate_writable_container_path(
            request.get("output_dir"), "output_dir"
        )
        slots = request.get("slots", self.profile.default_slots)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValidationError("slots must be a non-negative integer")
        pool = request.get("pool", self.profile.default_pool)
        image = request.get("image", self.profile.default_image)
        _required_text(pool, "pool")
        _required_text(image, "image")
        code_revision = request.get("code_revision")
        if code_revision is not None and not isinstance(code_revision, str):
            raise ValidationError("code_revision must be a string or null")
        requested_name = request.get("name")
        requested_description = request.get("description")
        if requested_name is not None:
            name = _display_name(requested_name)
        elif (
            kind == "experiment"
            and experiment_config is not None
            and experiment_config.get("name") is not None
        ):
            name = _display_name(
                experiment_config["name"], "experiment_config.name"
            )
        else:
            name = None
        if requested_description is not None:
            description = _display_description(requested_description)
        elif kind == "experiment" and experiment_config is not None:
            description = _display_description(
                experiment_config.get("description"),
                "experiment_config.description",
            )
        else:
            description = None
        generated_name = name is None
        if generated_name:
            basename = PurePosixPath(workdir).name or "shared-root"
            name = _display_name(f"{kind}: {basename}")
        admission = gpu_admission.normalize_policy(
            request.get("gpu_admission"),
            kind=kind,
            slots=slots,
            experiment_config=experiment_config,
        )

        if kind == "experiment":
            config = self._experiment_config(
                experiment_config,
                name,
                description,
                request.get("command"),
                workdir,
                output_dir,
                slots,
                pool,
                image,
                code_revision,
                admission,
            )
        else:
            config = self._task_config(
                kind,
                request,
                name,
                description,
                workdir,
                output_dir,
                slots,
                pool,
                image,
                code_revision,
                admission,
            )
        create_directories = self._create_directories(
            request.get("create_directories"), kind, config
        )

        advisories: List[Dict[str, Any]] = []
        if generated_name:
            advisories.append(
                {
                    "code": "generated_task_name",
                    "message": (
                        f"Generated task name '{name}'. Supply name and description "
                        "to make task lists easier to scan."
                    ),
                }
            )
        if overnight and kind != "experiment":
            advisories.append(
                {
                    "code": "overnight_experiment_recommended",
                    "message": "Long or overnight work is more robust as an experiment.",
                }
            )
        if kind == "shell" and self.profile.shell_inactivity_seconds is not None:
            advisories.append(
                {
                    "code": "shell_inactivity_policy",
                    "seconds": self.profile.shell_inactivity_seconds,
                    "message": (
                        "The deployment may stop an inactive shell after "
                        f"{self.profile.shell_inactivity_seconds} seconds."
                    ),
                }
            )
        result = {
            "kind": kind,
            "name": name,
            "description": description,
            "allow_queue": allow_queue,
            "config": config,
            "code_revision": code_revision,
            "advisories": advisories,
        }
        # New request-derived keys appear only when used, so existing payload hashes and
        # idempotent retries of earlier requests are unchanged.
        if create_directories:
            result["create_directories"] = create_directories
        if admission is not None:
            result["gpu_admission"] = admission
        return result

    @staticmethod
    def _create_directories(value: Any, kind: str, config: Mapping[str, Any]) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValidationError("create_directories must be a list of strings")
        if len(set(value)) != len(value):
            raise ValidationError("create_directories must not repeat an entry")
        unknown = set(value) - set(_CREATE_DIRECTORY_FIELDS)
        if unknown:
            raise ValidationError(
                f"create_directories accepts only {', '.join(_CREATE_DIRECTORY_FIELDS)}; "
                f"unknown: {sorted(unknown)}"
            )
        if "checkpoint_storage" in value and (
            kind != "experiment" or not isinstance(config.get("checkpoint_storage"), Mapping)
        ):
            raise ValidationError(
                "create_directories checkpoint_storage requires an experiment with "
                "experiment_config.checkpoint_storage"
            )
        return sorted(value)

    def _base_config(
        self,
        kind: str,
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
        admission: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        variables = [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
        if admission is not None:
            variables.extend(gpu_admission.environment_variables(admission))
        return {
            "resources": {
                ("slots_per_trial" if kind == "experiment" else "slots"): slots,
                "resource_pool": pool,
            },
            "environment": {
                "image": image,
                "environment_variables": variables,
            },
            "bind_mounts": [mount.as_config() for mount in self.profile.mounts],
        }

    def _task_config(
        self,
        kind: str,
        request: Mapping[str, Any],
        name: str,
        description: Optional[str],
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
        admission: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = self._base_config(
            kind, workdir, output_dir, slots, pool, image, code_revision, admission
        )
        config["description"] = name + (
            ("\n" + description) if description is not None else ""
        )
        command = request.get("command")
        if kind == "command":
            config["entrypoint"] = [
                "/bin/bash",
                "-lc",
                self._render_entrypoint(
                    command, workdir, output_dir, admission is not None
                ),
            ]
        elif command is not None:
            raise ValidationError(
                "shell kind does not accept command; startup is managed by Determined"
            )
        return config

    def _experiment_config(
        self,
        value: Optional[Mapping[str, Any]],
        name: str,
        description: Optional[str],
        command: Any,
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
        admission: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = copy.deepcopy(dict(value or {}))
        self._validate_config_paths(config)
        checkpoint = config.get("checkpoint_storage")
        if checkpoint is not None:
            if not isinstance(checkpoint, Mapping) or checkpoint.get("type") != "shared_fs":
                raise ValidationError("checkpoint_storage must be a shared_fs configuration on mapped storage")
            if any(_normalized_key(key) in {"checkpointpath", "tensorboardpath"} for key in checkpoint):
                raise ValidationError("checkpoint_storage must use storage_path instead of legacy path aliases")
            checkpoint_root = self.profile.validate_writable_host_path(
                checkpoint.get("host_path"), "experiment_config.checkpoint_storage.host_path"
            )
            if checkpoint.get("storage_path") is not None:
                storage_path = checkpoint["storage_path"]
                if not isinstance(storage_path, str):
                    raise ValidationError("checkpoint_storage.storage_path must be a string")
                resolved_storage = self.profile.validate_writable_host_path(
                    posixpath.join(checkpoint_root, storage_path),
                    "experiment_config.checkpoint_storage.storage_path",
                )
                if resolved_storage != checkpoint_root and not resolved_storage.startswith(checkpoint_root.rstrip("/") + "/"):
                    raise ValidationError("checkpoint_storage.storage_path must remain inside host_path")
            if checkpoint.get("container_path") is not None:
                self.profile.validate_writable_container_path(
                    checkpoint["container_path"], "experiment_config.checkpoint_storage.container_path"
                )
        if "bind_mounts" in config or "bindMounts" in config:
            raise ValidationError("experiment bind mounts come only from the compute profile")
        config["name"] = name
        if description is None:
            config.pop("description", None)
        else:
            config["description"] = description

        resources = config.get("resources", {})
        if not isinstance(resources, Mapping):
            raise ValidationError("experiment_config.resources must be an object")
        resources = copy.deepcopy(dict(resources))
        resources.update({"slots_per_trial": slots, "resource_pool": pool})
        config["resources"] = resources

        environment = config.get("environment", {})
        if not isinstance(environment, Mapping):
            raise ValidationError("experiment_config.environment must be an object")
        environment = copy.deepcopy(dict(environment))
        environment["image"] = image
        variables = environment.get("environment_variables", [])
        if not isinstance(variables, list) or not all(isinstance(item, str) for item in variables):
            raise ValidationError("environment.environment_variables must be a list of strings")
        for item in variables:
            if item.split("=", 1)[0] in _MANAGED_VARIABLES:
                raise ValidationError("compute-managed environment variables cannot be overridden")
        variables = list(variables) + [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
        if admission is not None:
            variables.extend(gpu_admission.environment_variables(admission))
        environment["environment_variables"] = variables
        config["environment"] = environment
        config["bind_mounts"] = [mount.as_config() for mount in self.profile.mounts]
        if command is not None:
            if "entrypoint" in config:
                raise ValidationError(
                    "provide command or experiment_config.entrypoint, not both"
                )
            config["entrypoint"] = self._render_entrypoint(
                command, workdir, output_dir, admission is not None
            )
        elif "entrypoint" in config:
            config["entrypoint"] = self._render_entrypoint(
                config["entrypoint"], workdir, output_dir, admission is not None
            )
        else:
            raise ValidationError("experiment requires command or experiment_config.entrypoint")
        return config

    def _validate_config_paths(self, value: Any, path: str = "experiment_config") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = _normalized_key(key)
                nested_path = f"{path}.{key}"
                if normalized == "hostpath":
                    self.profile.validate_host_path(nested, nested_path)
                elif normalized == "containerpath":
                    self.profile.validate_container_path(nested, nested_path)
                else:
                    self._validate_config_paths(nested, nested_path)
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                self._validate_config_paths(nested, f"{path}[{index}]")

    @staticmethod
    def _render_entrypoint(
        command: Any, workdir: str, output_dir: str, admission: bool = False
    ) -> str:
        if isinstance(command, str):
            if not command:
                raise ValidationError("command must not be empty")
            rendered = command
        elif isinstance(command, (list, tuple)):
            if not command or not all(isinstance(part, str) and part for part in command):
                raise ValidationError("command list must contain non-empty strings")
            rendered = " ".join(shlex.quote(part) for part in command)
        else:
            raise ValidationError("command must be a string or string-list")
        prefix = f"mkdir -p {shlex.quote(output_dir)} && cd {shlex.quote(workdir)}"
        if not admission:
            return f"{prefix} && {rendered}"
        # The command starts on its own line, so a failed mkdir, cd, or preflight (exit 86)
        # ends the shell before any statement of it runs, whatever ';', '&', or '||' it has.
        return f"{prefix} && {gpu_admission.entrypoint_step()} || exit $?\n{rendered}"

    def _path_entries(
        self, rendered: Mapping[str, Any], request: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """List launch paths in the cluster-agent host namespace."""

        creates = set(rendered.get("create_directories", ()))
        config = rendered["config"]
        entries: List[Dict[str, Any]] = [
            {
                "field": f"bind_mounts[{index}].host_path",
                "host_path": mount["host_path"],
                "container_path": mount["container_path"],
                "required": True,
                "create": False,
            }
            for index, mount in enumerate(config.get("bind_mounts", []))
        ]
        if rendered["kind"] in {"command", "experiment"}:
            workdir = self.profile.validate_container_path(request.get("workdir"), "workdir")
            entries.append({
                "field": "workdir",
                "host_path": self.profile.host_path_for(workdir, "workdir"),
                "container_path": workdir,
                "required": True,
                "create": False,
            })
        checkpoint = config.get("checkpoint_storage") if rendered["kind"] == "experiment" else None
        if isinstance(checkpoint, Mapping) and isinstance(checkpoint.get("host_path"), str):
            container = checkpoint.get("container_path")
            entries.append({
                "field": "experiment_config.checkpoint_storage.host_path",
                "host_path": posixpath.normpath(checkpoint["host_path"]),
                "container_path": container if isinstance(container, str) else None,
                "required": True,
                "create": "checkpoint_storage" in creates,
            })
        output_dir = self.profile.validate_container_path(request.get("output_dir"), "output_dir")
        # The entrypoint creates output_dir, so it is required only when created here.
        entries.append({
            "field": "output_dir",
            "host_path": self.profile.host_path_for(output_dir, "output_dir"),
            "container_path": output_dir,
            "required": "output_dir" in creates,
            "create": "output_dir" in creates,
        })
        return entries

    def _check_paths(
        self, rendered: Mapping[str, Any], request: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Observe launch paths through a trusted local view; unknown is never missing."""

        entries = self._path_entries(rendered, request)
        observed = self.path_inspector.inspect([entry["host_path"] for entry in entries])
        checks: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        for entry, (status, reason) in zip(entries, observed):
            if entry["create"] and status in {"missing", "unverified"}:
                # Nothing is created while planning; launch creates it before the claim.
                reason = "missing" if status == "missing" else reason
                status = "will_create"
            check = {
                "field": entry["field"],
                "host_path": entry["host_path"],
                "container_path": entry["container_path"],
                "required": entry["required"],
                "status": status,
                "reason": reason,
            }
            checks.append(check)
            if entry["required"] and status in {"missing", "not_directory"}:
                missing.append(
                    {"field": entry["field"], "host_path": entry["host_path"], "status": status}
                )
        if missing:
            error = ValidationError(
                "required launch paths are missing or not directories: "
                + ", ".join(f"{item['field']} ({item['host_path']})" for item in missing)
                + "; create them or request create_directories",
                code="path_not_found",
            )
            error.details = {"missing_paths": missing}
            raise error
        return checks

    def _prepare_directories(
        self, rendered: Mapping[str, Any], request: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        if self.path_inspector is None:
            raise ConflictError(
                "create_directories requires shared-storage access configuration",
                code="configuration_required",
            )
        entries = [
            {"field": entry["field"], "host_path": entry["host_path"]}
            for entry in self._path_entries(rendered, request)
            if entry["create"]
        ]
        return self.path_inspector.ensure_directories(entries)

    def launch(self, request: Dict[str, Any], request_id: str, owner: str) -> Dict[str, Any]:
        request_id = _required_text(request_id, "request_id")
        owner = _required_text(owner, "owner")
        plan = self._render(request)
        payload_hash = self._payload_hash(plan)
        existing = self.store.lookup_request(request_id, owner)
        if existing is not None:
            # An established request is returned unchanged, even if its paths later moved.
            self._validate_idempotent_payload(existing, payload_hash, request, plan)
            return self._public(existing)
        if self.path_inspector is not None:
            self._check_paths(plan, request)
        # Bind the submit call before any durable write: a lazily built client that fails
        # (for example a login error) must fail before directories exist or the id is claimed.
        launch_task = self.client.launch_task

        if not plan["allow_queue"]:
            try:
                self._inspector().require_capacity(plan["kind"], plan["config"])
            except APIError:
                # A concurrent process may have claimed this id after our lookup.
                existing = self.store.lookup_request(request_id, owner)
                if existing is not None:
                    self._validate_idempotent_payload(
                        existing, payload_hash, request, plan
                    )
                    return self._public(existing)
                raise
        prepared = (
            self._prepare_directories(plan, request) if plan.get("create_directories") else None
        )
        workdir = self.profile.validate_container_path(request.get("workdir"), "workdir")
        output_dir = self.profile.validate_container_path(request.get("output_dir"), "output_dir")
        record, created = self.store.claim(
            request_id=request_id,
            owner=owner,
            payload_hash=payload_hash,
            profile_hash=self.profile.fingerprint,
            kind=plan["kind"],
            code_revision=plan["code_revision"],
            name=plan["name"],
            description=plan["description"],
            workdir=workdir,
            output_dir=output_dir,
            cluster_identity=self._cluster_identity(),
        )
        if not created:
            return self._public(record)

        self.store.mark_submitting(record.task_id)
        launch_config = self._with_submission_marker(plan["config"], record.submission_marker)
        try:
            entity = launch_task(plan["kind"], launch_config)
            remote_id = _remote_id(entity)
            result = self._public(self.store.mark_submitted(record.task_id, remote_id))
            if prepared is not None:
                result["prepared_directories"] = prepared
            return result
        except SubmissionUncertainError as exc:
            self._best_effort_submission_state(record.task_id, "uncertain")
            self._attach_task_details(exc, record)
            raise
        except APIError as exc:
            # API-provided strings can echo request data; persist only a fixed class.
            self._best_effort_submission_state(record.task_id, "failed")
            self._attach_task_details(exc, record)
            raise
        except Exception as exc:
            self._best_effort_submission_state(record.task_id, "uncertain")
            error = SubmissionUncertainError("remote submission outcome is uncertain")
            self._attach_task_details(error, record)
            raise error from exc

    def _inspector(self) -> Any:
        if self.inspector is None:
            from .admission import ResourceInspector

            self.inspector = ResourceInspector(self.client)
        return self.inspector

    def _validate_idempotent_payload(
        self,
        record: TaskRecord,
        payload_hash: str,
        request: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        if record.origin == "adopted":
            raise ConflictError(
                "an adopted task cannot be used as a launch retry",
                code="idempotency_conflict",
            )
        if record.payload_hash == payload_hash:
            return
        new_fields = {"name", "description", "allow_queue", "create_directories", "gpu_admission"}
        legacy_record = record.name is None and record.description is None
        legacy_request = not new_fields.intersection(request)
        if (
            legacy_record
            and legacy_request
            and record.payload_hash == self._legacy_payload_hash(plan, request)
        ):
            return
        raise ConflictError(
            "request_id was already used with a different request",
            code="idempotency_conflict",
        )

    def _legacy_payload_hash(
        self, plan: Mapping[str, Any], request: Mapping[str, Any]
    ) -> str:
        """Reconstruct the 0.4 plan hash for migrated task rows only."""

        config = copy.deepcopy(dict(plan["config"]))
        if plan["kind"] == "experiment":
            original = request.get("experiment_config")
            original = original if isinstance(original, Mapping) else {}
            for field in ("name", "description"):
                if field in original:
                    config[field] = copy.deepcopy(original[field])
                else:
                    config.pop(field, None)
        else:
            config.pop("description", None)
        legacy_plan = {
            "kind": plan["kind"],
            "config": config,
            "code_revision": plan["code_revision"],
            "advisories": [
                copy.deepcopy(item)
                for item in plan["advisories"]
                if item.get("code") != "generated_task_name"
            ],
        }
        return self._payload_hash(legacy_plan)

    def _best_effort_submission_state(self, task_id: str, state: str) -> None:
        try:
            if state == "failed":
                self.store.mark_failed(task_id, "api_error")
            else:
                self.store.mark_uncertain(task_id)
        except Exception:
            # A durable pending/submitting record is already safe: retrying it cannot
            # launch again. Preserve the original API/outcome error for the caller.
            pass

    def status(self, task_id: str, owner: str) -> Dict[str, Any]:
        record = self.store.get_owned(task_id, owner)
        binding = self._validate_binding(record, "observe")
        cross_profile = binding["mode"] == "cross_profile"
        if record.remote_id is None and cross_profile:
            # Another profile's unbound submission is shown as stored; nothing is written.
            result = self._public(record)
            result["binding"] = binding
            return result
        if record.remote_id is None and record.state in {"pending", "submitting"}:
            record = self.store.mark_stale_submission_uncertain(
                task_id, owner, self.submission_stale_seconds
            )
        result = self._public(record)
        if record.remote_id is None:
            result["binding"] = binding
            return result
        user_id = self._current_user_id() if cross_profile else None
        entity = self.client.get_task(record.kind, record.remote_id)
        if record.origin == "adopted":
            self._check_adopted_entity(record, entity)
        elif cross_profile:
            self._verify_cross_profile(record, entity, user_id, binding)
        state = _remote_state(entity)
        if state != record.remote_state:
            record = self.store.update_remote_state(record.task_id, state)
            result = self._public(record)
        result["remote"] = entity
        result["binding"] = binding
        return result

    def logs(
        self, task_id: str, owner: str, tail: int = 100, include_binding: bool = False
    ) -> Any:
        """Return log records, or ``{task_id, binding, logs}`` with ``include_binding``."""
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValidationError("tail must be a positive integer")
        if not isinstance(include_binding, bool):
            raise ValidationError("include_binding must be a boolean")
        record = self.store.get_owned(task_id, owner)
        binding = self._validate_binding(record, "observe")
        result: List[Any] = []
        if record.remote_id is not None:
            if record.origin == "adopted":
                self._check_adopted_entity(
                    record, self.client.get_task(record.kind, record.remote_id)
                )
            elif binding["mode"] == "cross_profile":
                user_id = self._current_user_id()
                entity = self.client.get_task(record.kind, record.remote_id)
                self._verify_cross_profile(record, entity, user_id, binding)
            result = self.client.task_logs(record.kind, record.remote_id, tail)
            if not isinstance(result, list):
                raise APIError("task log response was not a list", code="invalid_api_response")
        if include_binding:
            return {"task_id": record.task_id, "binding": binding, "logs": result}
        return result

    def usage(
        self,
        task_id: str,
        owner: str,
        window_seconds: int = 3600,
        allocation_id: Optional[str] = None,
        trial_id: Optional[int] = None,
        metrics: Optional[Sequence[str]] = None,
        include_samples: bool = False,
    ) -> Dict[str, Any]:
        """Summarize measured CPU, memory, and GPU use of one owned task."""
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not _USAGE_MIN_WINDOW_SECONDS <= window_seconds <= _USAGE_MAX_WINDOW_SECONDS
        ):
            raise ValidationError(
                f"window_seconds must be an integer from {_USAGE_MIN_WINDOW_SECONDS} "
                f"to {_USAGE_MAX_WINDOW_SECONDS}"
            )
        if allocation_id is not None and (
            not isinstance(allocation_id, str)
            or not 1 <= len(allocation_id) <= 256
            or not allocation_id.isprintable()
            or allocation_id.strip() != allocation_id
        ):
            raise ValidationError("allocation_id must be a printable string of 1 to 256 characters")
        if trial_id is not None:
            try:
                trial_id = int(DeterminedAPIClient.normalize_user_id(trial_id))
            except ValueError as exc:
                raise ValidationError("trial_id must be a positive integer") from exc
        if metrics is not None:
            if (
                isinstance(metrics, str)
                or not isinstance(metrics, (list, tuple))
                or not metrics
                or not all(isinstance(item, str) and item in _USAGE_METRICS for item in metrics)
            ):
                raise ValidationError(
                    f"metrics must be a non-empty list drawn from: {', '.join(_USAGE_METRICS)}"
                )
            metrics = list(dict.fromkeys(metrics))
        if not isinstance(include_samples, bool):
            raise ValidationError("include_samples must be a boolean")

        record = self.store.get_owned(task_id, owner)
        if trial_id is not None and record.kind != "experiment":
            raise ValidationError("trial_id applies only to experiment tasks")
        binding = self._validate_binding(record, "observe")
        if record.remote_id is None:
            raise ConflictError(
                "remote task id is unknown; reconcile the submission before reading usage",
                code="remote_id_unknown",
            )
        entity: Any = None
        if record.origin == "adopted":
            entity = self.client.get_task(record.kind, record.remote_id)
            self._check_adopted_entity(record, entity)
        elif binding["mode"] == "cross_profile":
            user_id = self._current_user_id()
            entity = self.client.get_task(record.kind, record.remote_id)
            self._verify_cross_profile(record, entity, user_id, binding)
        if not self.client.task_resources_enabled():
            raise APIError(
                "task resource monitoring is not enabled on this Determined master",
                code="task_resources_disabled",
            )
        trial = (
            self._usage_trial(record.remote_id, trial_id)
            if record.kind == "experiment"
            else None
        )
        determined_task_id = trial["task_id"] if trial is not None else record.remote_id
        info = self.client.get_task_info(determined_task_id)
        allocations = [
            {
                **item,
                "start_time": _utc_text(item["start_time"]),
                "end_time": _utc_text(item["end_time"]),
            }
            for item in info["allocations"]
        ]
        selected = [
            item for item in allocations
            if allocation_id is None or item["allocation_id"] == allocation_id
        ]
        if allocation_id is not None and not selected:
            raise NotFoundError(
                "allocation does not belong to the selected Determined task",
                code="allocation_not_found",
            )

        now = int(time.time())
        task_start = _unix_seconds(info["start_time"])
        task_end = _unix_seconds(info["end_time"])
        # Mirror the WebUI: an ended task shows the window preceding its end.
        end, anchor = (min(task_end, now), "task_end") if task_end is not None else (now, "now")
        # A paused trial has no task end time, and an older allocation of a running task
        # has its own end; nothing is measured after the last selected allocation ends.
        allocation_ends = [_lenient_unix_seconds(item["end_time"]) for item in selected]
        if selected and all(value is not None for value in allocation_ends):
            if max(allocation_ends) < end:
                end, anchor = max(allocation_ends), "allocation_end"
        start = end - window_seconds
        floors = [task_start]
        if allocation_id is not None:
            floors.append(_lenient_unix_seconds(selected[0]["start_time"]))
        for floor in floors:
            if floor is not None:
                start = max(start, floor)
        start = max(0, min(start, end - 1))
        step = max(
            _USAGE_MIN_STEP_SECONDS, math.ceil((end - start) / (_USAGE_MAX_POINTS - 1))
        )
        response = self.client.get_task_resources(
            determined_task_id, start=start, end=end, step=step, allocation_id=allocation_id
        )
        expected = (end - start) // step + 1
        returned = response["series"]
        series = [item for item in returned if metrics is None or item["metric"] in metrics]

        # Context is best-effort: a failed lookup is reported and never hides measurements.
        unavailable: List[str] = []
        unreachable = False

        def context(name: str, operation: Any, *args: Any) -> Any:
            nonlocal unreachable
            if not unreachable:
                try:
                    return operation(*args)
                except APIError as exc:
                    # After a timeout, each further lookup would wait the full timeout too.
                    unreachable = exc.code == "transport_error"
            if name not in unavailable:
                unavailable.append(name)
            return None

        if entity is None:
            try:
                entity = self.client.get_task(record.kind, record.remote_id)
            except APIError as exc:
                unreachable = exc.code == "transport_error"
                # Determined keeps an ended command or shell for only 24 hours and drops it
                # on a master restart, so its absence then leaves the pool unknown.
                if not (exc.code == 404 and task_end is not None):
                    unavailable.append("resource_pool")
        pool_name = (
            self._remote_text(entity.get("resourcePool"), 256)
            if isinstance(entity, Mapping)
            else None
        )
        resource_pool = None
        if pool_name is not None:
            pools = context("resource_pool", self.client.list_resource_pools) or []
            described = next((item for item in pools if item["name"] == pool_name), None)
            resource_pool = {
                "name": pool_name,
                "description": self._remote_text(described["description"]) if described else None,
            }
        detail_ids = [item["allocation_id"] for item in allocations[:_USAGE_MAX_ALLOCATION_DETAILS]]
        if allocation_id is not None and allocation_id not in detail_ids:
            detail_ids.append(allocation_id)
        details: Dict[str, Dict[str, Any]] = {}
        for detail_id in detail_ids:
            detail = context("allocation_details", self.client.get_allocation, detail_id)
            if detail is not None:
                details[detail_id] = detail
        for item in allocations:
            detail = details.get(item["allocation_id"], {})
            for key in ("slots", "exit_reason", "status_code"):
                item[key] = detail.get(key)
        gpu_models: Dict[str, str] = {}
        if any(item["labels"]["gpu_uuid"] for item in returned):
            gpu_models = context("gpu_models", self.client.list_gpu_devices) or {}

        summaries = [self._usage_series(item, gpu_models) for item in series]
        if summaries:
            explanation = f"{len(summaries)} measurement series over the selected window"
        elif returned:
            others = sorted({item["metric"] for item in returned})
            explanation = (
                "None of the requested metrics were measured in this window; Determined "
                f"returned {len(returned)} other series ({', '.join(others)})"
            )
        else:
            explanation = (
                "No measurements were returned; the task may not have run in this "
                "window, or monitoring retained no data for it"
            )
        if anchor == "allocation_end":
            explanation += (
                "; the window ends when the last selected allocation ended because no "
                "selected allocation is running"
            )
        if trial is not None and (trial["experiment_trial_count"] or 0) > 1:
            explanation += (
                f"; the experiment has {trial['experiment_trial_count']} trials and this "
                f"reports trial {trial['id']}, so pass trial_id to inspect another"
            )
        if trial is not None and trial["total_batches_processed"] == 0:
            explanation += (
                "; the trial reports no batches, which is expected when the workload does "
                "not report training progress through Determined's Core API or has not "
                "reported yet"
            )
        result: Dict[str, Any] = {
            "task_id": record.task_id,
            "kind": record.kind,
            "remote_id": record.remote_id,
            "determined_task_id": determined_task_id,
            "trial": (
                {key: value for key, value in trial.items() if key != "task_id"}
                if trial is not None
                else None
            ),
            "resource_pool": resource_pool,
            "task_start_time": info["start_time"],
            "task_end_time": info["end_time"],
            "allocations": allocations,
            "allocation_id": allocation_id,
            "window": {
                "start": start,
                "end": end,
                "step": step,
                "start_at": _iso_seconds(start),
                "end_at": _iso_seconds(end),
                "anchor": anchor,
                "expected_points": expected,
            },
            "series": summaries,
            "gpus": self._usage_gpus(returned, gpu_models, details),
            "warnings": response["warnings"],
            "context_unavailable": unavailable,
            "explanation": explanation,
            "observed_at": _iso_seconds(now),
            "advisory": _USAGE_ADVISORY,
            "binding": binding,
        }
        if len(allocations) > _USAGE_MAX_ALLOCATION_DETAILS:
            result["allocation_details_limit"] = _USAGE_MAX_ALLOCATION_DETAILS
        if include_samples:
            omitted = sum(len(item["samples"]) for item in series) > _USAGE_MAX_RETURNED_SAMPLES
            result["samples_omitted"] = omitted
            if omitted:
                result["samples_limit"] = _USAGE_MAX_RETURNED_SAMPLES
            else:
                for summary, item in zip(summaries, series):
                    summary["samples"] = item["samples"]
        return result

    def _usage_trial(self, experiment_id: str, trial_id: Optional[int]) -> Dict[str, Any]:
        count: Optional[int] = None
        if trial_id is None:
            latest = self.client.get_latest_trial(experiment_id)
            trial, count = latest["trial"], latest["total"]
            if trial is None:
                raise ConflictError("experiment has no trials yet", code="task_not_started")
        else:
            trial = self.client.get_trial(str(trial_id))
        try:
            actual_id = int(DeterminedAPIClient.normalize_user_id(trial.get("id")))
            parent = DeterminedAPIClient.normalize_user_id(trial.get("experimentId"))
        except ValueError as exc:
            raise APIError("Trial response is malformed", code="invalid_response") from exc
        if parent != experiment_id or (trial_id is not None and actual_id != trial_id):
            raise NotFoundError("trial does not belong to this experiment", code="trial_not_found")
        task_ids = trial.get("taskIds") or ([trial["taskId"]] if trial.get("taskId") else [])
        if not isinstance(task_ids, list) or not all(
            isinstance(item, str) and item for item in task_ids
        ):
            raise APIError("Trial response is malformed", code="invalid_response")
        if not task_ids:
            raise ConflictError("trial has no Determined task yet", code="task_not_started")
        state = trial.get("state")
        batches, restarts = (
            value if not isinstance(value, bool) and isinstance(value, int) and value >= 0 else None
            for value in (trial.get("totalBatchesProcessed"), trial.get("restarts"))
        )
        wall_clock = trial.get("wallClockTime")
        wall_clock = float(wall_clock) if _finite(wall_clock) and wall_clock >= 0 else None
        summary_metrics, truncated = self._summary_metrics(trial.get("summaryMetrics"))
        return {
            "id": actual_id,
            "state": state if isinstance(state, str) else None,
            "selection": "requested" if trial_id is not None else "latest",
            "experiment_trial_count": count,
            "task_count": len(task_ids),
            "total_batches_processed": batches,
            "wall_clock_seconds": wall_clock,
            "restarts": restarts,
            # Wall-clock time spans every allocation, including image pulls, startup, and
            # restarts, so this is a floor on the training rate.
            "batches_per_second_lower_bound": (
                round(batches / wall_clock, 6) if batches is not None and wall_clock else None
            ),
            "summary_metrics": summary_metrics,
            "summary_metrics_truncated": truncated,
            # taskIds is ordered by task start; a continued trial's newest task is last.
            "task_id": task_ids[-1],
        }

    @staticmethod
    def _summary_metrics(value: Any) -> tuple[Dict[str, Dict[str, Any]], bool]:
        """Keep numeric per-metric statistics from a trial's summary metrics."""
        result: Dict[str, Dict[str, Any]] = {}
        if not isinstance(value, Mapping):
            return result, False
        kept = 0
        # Determined's built-in groups come first so a large training or custom group
        # cannot crowd validation metrics out of the cap.
        builtin = ("validation_metrics", "avg_metrics")
        groups = [key for key in builtin if key in value] + sorted(
            key for key in value if isinstance(key, str) and key not in builtin
        )
        for group in groups:
            metrics = value[group]
            if not isinstance(metrics, Mapping):
                continue
            for name in sorted(key for key in metrics if isinstance(key, str)):
                stats = metrics[name]
                if not isinstance(stats, Mapping):
                    continue
                if kept == _USAGE_MAX_SUMMARY_METRICS:
                    return result, True
                entry: Dict[str, Any] = (
                    {"type": stats["type"]} if isinstance(stats.get("type"), str) else {}
                )
                entry.update(
                    (key, stats[key])
                    for key in _USAGE_SUMMARY_STATISTICS
                    if _finite(stats.get(key))
                )
                result.setdefault(group, {})[name] = entry
                kept += 1
        return result, False

    @staticmethod
    def _usage_series(series: Mapping[str, Any], gpu_models: Mapping[str, str]) -> Dict[str, Any]:
        points = sorted(
            (stamp, value) for stamp, value in series["samples"] if value is not None
        )
        values = [value for _stamp, value in points]
        ordered = sorted(values)
        gpu_uuid = series["labels"]["gpu_uuid"]
        summary = {
            "metric": series["metric"],
            "unit": _USAGE_METRICS.get(series["metric"]),
            **series["labels"],
            "gpu_model": gpu_models.get(gpu_uuid) if gpu_uuid else None,
            "points": len(series["samples"]),
            "available_points": len(values),
            "first_at": _iso_seconds(points[0][0]) if points else None,
            "last_at": _iso_seconds(points[-1][0]) if points else None,
            "last": values[-1] if values else None,
            "min": ordered[0] if ordered else None,
            "max": ordered[-1] if ordered else None,
            "mean": round(sum(values) / len(values), 6) if values else None,
            "p50": _percentile(ordered, 0.5),
            "p95": _percentile(ordered, 0.95),
        }
        if series["metric"] == "gpu_utilization_percent":
            summary["idle_fraction"] = _idle_fraction(values)
        return summary

    @staticmethod
    def _usage_gpus(
        series: Sequence[Mapping[str, Any]],
        gpu_models: Mapping[str, str],
        details: Mapping[str, Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Compare the GPUs of each allocation using every returned GPU series."""
        groups: Dict[Optional[str], Dict[str, Dict[str, List[float]]]] = {}
        for item in series:
            gpu_uuid = item["labels"]["gpu_uuid"]
            if not gpu_uuid or item["metric"] not in {
                "gpu_utilization_percent", "gpu_memory_used_bytes",
            }:
                continue
            values = [value for _stamp, value in item["samples"] if value is not None]
            devices = groups.setdefault(item["labels"]["allocation_id"], {})
            devices.setdefault(gpu_uuid, {}).setdefault(item["metric"], []).extend(values)
        result: List[Dict[str, Any]] = []
        for allocation, devices in sorted(groups.items(), key=lambda pair: pair[0] or ""):
            means = {
                gpu_uuid: sum(values) / len(values)
                for gpu_uuid, metrics in devices.items()
                if (values := metrics.get("gpu_utilization_percent"))
            }
            utilization = [
                value
                for metrics in devices.values()
                for value in metrics.get("gpu_utilization_percent", [])
            ]
            memory = [
                value
                for metrics in devices.values()
                for value in metrics.get("gpu_memory_used_bytes", [])
            ]
            lowest = min(sorted(means), key=means.__getitem__) if means else None
            detail = details.get(allocation) if allocation is not None else None
            result.append({
                "allocation_id": allocation,
                "gpu_count": len(devices),
                "requested_slots": detail["slots"] if detail else None,
                "gpu_models": sorted({gpu_models[key] for key in devices if key in gpu_models}),
                # Each GPU counts equally, whatever its number of available samples.
                "mean_utilization_percent": (
                    round(sum(means.values()) / len(means), 6) if means else None
                ),
                "min_gpu_mean_utilization_percent": round(means[lowest], 6) if means else None,
                "max_gpu_mean_utilization_percent": (
                    round(max(means.values()), 6) if means else None
                ),
                "utilization_spread_percent": (
                    round(max(means.values()) - means[lowest], 6) if means else None
                ),
                "least_utilized_gpu_uuid": lowest,
                "idle_fraction": _idle_fraction(utilization),
                "idle_threshold_percent": _GPU_IDLE_PERCENT,
                "max_memory_used_bytes": max(memory) if memory else None,
            })
        return result

    def cancel(self, task_id: str, owner: str) -> Dict[str, Any]:
        record = self.store.get_owned(task_id, owner)
        self._validate_binding(record)
        if record.remote_id is None:
            raise ConflictError(
                "remote task id is unknown; reconcile the submission before cancelling",
                code="remote_id_unknown",
            )
        if record.origin == "adopted":
            self._check_adopted_entity(
                record, self.client.get_task(record.kind, record.remote_id)
            )
        entity = self.client.cancel_task(record.kind, record.remote_id)
        state = _remote_state(entity)
        if state is not None:
            record = self.store.update_remote_state(record.task_id, state)
        updated = self._public(record)
        updated["cancellation_acknowledged"] = True
        updated["remote"] = entity
        return updated

    def list_tasks(self, owner: str) -> List[Dict[str, Any]]:
        owner = _required_text(owner, "owner")
        records = self.store.list_owned(owner)
        identity: Any = _UNRESOLVED
        result = []
        for record in records:
            if identity is _UNRESOLVED and record.origin != "adopted":
                try:
                    identity = self._cluster_identity()
                except Exception:
                    # Listing stays local and never fails on client configuration.
                    identity = _UNKNOWN
            item = self._public(record)
            item["binding"] = self._offline_binding(record, identity)
            result.append(item)
        return result

    def _offline_binding(self, record: TaskRecord, identity: Any) -> Dict[str, Any]:
        """Summarize, without remote calls, which operations the current binding permits."""
        if record.origin == "adopted":
            # The live cluster ID and account are checked only when the task is used.
            return {
                "mode": "adopted",
                "profile_matches": None,
                "cluster_identity_matches": None,
                "mutations_allowed": None,
            }
        profile_matches = record.profile_hash == self.profile.fingerprint
        identity_matches = None if identity is _UNKNOWN else record.cluster_identity == identity
        if identity_matches is None:
            mode, mutations = "unknown", None
        elif not identity_matches:
            mode, mutations = "mismatch", False
        elif profile_matches:
            mode, mutations = "profile", True
        else:
            mode, mutations = "cross_profile", False
        return {
            "mode": mode,
            "profile_matches": profile_matches,
            "cluster_identity_matches": identity_matches,
            "mutations_allowed": mutations,
        }

    @staticmethod
    def _management_kind(kind: Any) -> str:
        if not isinstance(kind, str) or kind not in {"command", "shell", "experiment"}:
            raise ValidationError("kind must be command, shell, or experiment")
        return kind

    @staticmethod
    def _management_remote_id(kind: str, value: Any) -> str:
        if kind == "experiment":
            try:
                return DeterminedAPIClient.normalize_user_id(value)
            except ValueError as exc:
                raise ValidationError("experiment remote_id must be a positive integer") from exc
        if not isinstance(value, str):
            raise ValidationError("command and shell remote_id must be a UUID")
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValidationError("command and shell remote_id must be a UUID") from exc

    def _remote_account(self) -> tuple[str, Dict[str, str]]:
        cluster_id = self.client.get_cluster_id()
        user = self.client.get_current_user()
        if (
            not isinstance(cluster_id, str)
            or not cluster_id.strip()
            or len(cluster_id.encode("utf-8")) > 256
            or not isinstance(user, Mapping)
            or not isinstance(user.get("username"), str)
            or not user["username"].strip()
        ):
            raise APIError("Remote cluster or account identity is unavailable", code="invalid_response")
        try:
            user_id = DeterminedAPIClient.normalize_user_id(user.get("id"))
        except ValueError as exc:
            raise APIError("Remote account identity is unavailable", code="invalid_response") from exc
        return cluster_id.strip(), {"id": user_id, "username": user["username"].strip()}

    @staticmethod
    def _check_remote_owner(entity: Any, user_id: str) -> None:
        if not isinstance(entity, Mapping):
            raise APIError("Remote task response is malformed", code="invalid_response")
        try:
            task_user_id = DeterminedAPIClient.normalize_user_id(entity.get("userId"))
        except ValueError as exc:
            raise APIError("Remote task ownership cannot be established", code="ownership_unavailable") from exc
        if task_user_id != user_id:
            raise ConflictError(
                "remote task is not owned by the authenticated account",
                code="ownership_mismatch",
            )

    def _check_remote_entity(self, kind: str, remote_id: str, entity: Any, user_id: str) -> None:
        self._check_remote_owner(entity, user_id)
        try:
            actual_id = self._management_remote_id(kind, entity.get("id"))
        except ValidationError as exc:
            raise APIError("Remote task identity is malformed", code="invalid_response") from exc
        if actual_id != remote_id:
            raise ConflictError("remote task identity does not match", code="identity_mismatch")

    def _check_adopted_entity(self, record: TaskRecord, entity: Any) -> None:
        self._check_remote_entity(record.kind, record.remote_id, entity, record.remote_user_id)

    @staticmethod
    def _remote_text(value: Any, limit: int = 4096) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text[:limit] if text else None

    @classmethod
    def _remote_metadata(cls, entity: Mapping[str, Any]) -> Dict[str, Optional[str]]:
        description = cls._remote_text(entity.get("description"))
        name = cls._remote_text(entity.get("name"), 256) or cls._remote_text(entity.get("displayName"), 256)
        if name is None and description:
            name = description.splitlines()[0][:256]
        return {"name": name, "description": description}

    def discover(self, kind: str, owner: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Read one remote page for the authenticated account without registering tasks."""
        kind = self._management_kind(kind)
        owner = _required_text(owner, "owner")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")
        cluster_id, user = self._remote_account()
        page = self.client.list_remote_tasks(kind, user_id=user["id"], limit=limit, offset=offset)
        if not isinstance(page, Mapping) or not isinstance(page.get("tasks"), list):
            raise APIError("Remote task page is malformed", code="invalid_response")
        pagination = page.get("pagination")
        if (
            not isinstance(pagination, Mapping)
            or pagination.get("limit") != limit
            or pagination.get("offset") != offset
            or isinstance(pagination.get("total"), bool)
            or not isinstance(pagination.get("total"), int)
            or pagination["total"] < 0
            or (
                page["tasks"]
                and pagination["total"] < offset + len(page["tasks"])
            )
            or len(page["tasks"]) > limit
        ):
            raise APIError("Remote task pagination is malformed", code="invalid_response")
        tasks = []
        binding = self._cluster_identity()
        for entity in page["tasks"]:
            self._check_remote_owner(entity, user["id"])
            try:
                remote_id = self._management_remote_id(kind, entity.get("id"))
            except ValidationError as exc:
                raise APIError("Remote task identity is malformed", code="invalid_response") from exc
            local = self.store.lookup_remote(
                owner=owner, kind=kind, remote_id=remote_id,
                cluster_identity=binding, remote_cluster_id=cluster_id,
            )
            if (
                local is not None
                and local.origin == "adopted"
                and local.remote_user_id != user["id"]
            ):
                local = None
            tasks.append({
                "kind": kind,
                "remote_id": remote_id,
                **self._remote_metadata(entity),
                "remote_state": _remote_state(entity),
                "remote_user_id": user["id"],
                "remote_username": self._remote_text(entity.get("username"), 256),
                "resource_pool": self._remote_text(entity.get("resourcePool"), 256),
                "start_time": self._remote_text(entity.get("startTime"), 128),
                "local_task_id": local.task_id if local is not None else None,
            })
        next_offset = offset + len(tasks)
        return {
            "kind": kind,
            "remote_cluster_id": cluster_id,
            "account": user,
            "tasks": tasks,
            "pagination": {
                "offset": offset, "limit": limit, "total": pagination["total"],
                "next_offset": next_offset if tasks and next_offset < pagination["total"] else None,
            },
        }

    def adopt(self, kind: str, remote_id: str, owner: str) -> Dict[str, Any]:
        """Register an existing owned task locally; never submit or edit a remote task."""
        kind = self._management_kind(kind)
        remote_id = self._management_remote_id(kind, remote_id)
        owner = _required_text(owner, "owner")
        cluster_id, user = self._remote_account()
        entity = self.client.get_task(kind, remote_id)
        self._check_remote_entity(kind, remote_id, entity, user["id"])
        record, _created = self.store.adopt(
            owner=owner, kind=kind, remote_id=remote_id,
            remote_state=_remote_state(entity),
            cluster_identity=self._cluster_identity(),
            remote_cluster_id=cluster_id, remote_user_id=user["id"],
            profile_hash=self.profile.fingerprint,
            submission_marker=(
                entity.get("submissionMarker")
                if isinstance(entity.get("submissionMarker"), str)
                else None
            ),
            legacy_submission_markers=tuple(
                marker
                for description in self._entity_descriptions(entity)
                if (
                    marker := DeterminedAPIClient._valid_submission_marker(
                        description.splitlines()[0] if description else None
                    )
                ) is not None
            ),
            **self._remote_metadata(entity),
        )
        # A task launched through this owner/database retains its original binding.
        if record.origin == "submitted":
            self._validate_binding(record)
        if record.remote_state != _remote_state(entity):
            record = self.store.update_remote_state(record.task_id, _remote_state(entity))
        return self._public(record)

    def reconcile(self, task_id: str, owner: str, remote_id: str) -> Dict[str, Any]:
        """Bind an uncertain record only after verifying its unguessable remote marker."""

        remote_id = _required_text(remote_id, "remote_id")
        record = self.store.get_owned(task_id, owner)
        if record.origin == "adopted":
            raise ConflictError("an adopted task is not a submission to reconcile", code="invalid_operation")
        self._validate_binding(record)
        if record.remote_id is not None:
            if record.remote_id != remote_id:
                raise ConflictError("task is already bound to a different remote id")
            return self._public(record)
        if record.state not in {"pending", "submitting", "submission_uncertain"}:
            raise ConflictError("task is not eligible for reconciliation")
        entity = self.client.get_task(record.kind, remote_id)
        marker = entity.get("submissionMarker") if isinstance(entity, Mapping) else None
        legacy_match = (
            record.name is None
            and record.description is None
            and any(
                self._legacy_description_marker(
                    description, record.submission_marker
                )
                for description in self._entity_descriptions(entity)
            )
        )
        if marker != record.submission_marker and not legacy_match:
            raise ConflictError(
                "remote task identity marker does not match; refusing unsafe binding",
                code="identity_mismatch",
            )
        return self._public(
            self.store.bind_reconciled(record.task_id, remote_id, _remote_state(entity))
        )

    def _payload_hash(self, plan: Mapping[str, Any]) -> str:
        value = {
            "plan": plan,
            "profile_hash": self.profile.fingerprint,
            "cluster_identity": self._cluster_identity(),
        }
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError("request must contain JSON-compatible values") from exc
        return hashlib.sha256(encoded).hexdigest()

    def _cluster_identity(self) -> Optional[str]:
        label = self.profile.cluster_identity
        api_url = getattr(self.client, "api_url", None)
        if api_url is not None:
            parsed = urlsplit(str(api_url))
            if not parsed.hostname:
                raise ValidationError("client api_url has no host for cluster binding")
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError as exc:
                raise ValidationError("client api_url has an invalid port") from exc
            hostname = parsed.hostname.lower()
            if ":" in hostname:
                hostname = f"[{hostname}]"
            path = parsed.path.rstrip("/")
            endpoint = f"{parsed.scheme.lower()}://{hostname}{port}{path}"
        else:
            client_identity = getattr(self.client, "cluster_identity", None)
            endpoint = str(client_identity) if client_identity is not None else None
        if label is None and endpoint is None:
            return None
        # Keep the operator-facing label and the resolved endpoint in the binding.
        # A reused label must never authorize operations against a different master.
        return json.dumps(
            {"endpoint": endpoint, "label": label},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _validate_binding(self, record: TaskRecord, purpose: str = "mutate") -> Dict[str, Any]:
        """Check the record's binding before any remote task access.

        ``mutate`` requires the original profile and endpoint. ``observe`` also accepts a
        record from another profile on the same endpoint and label; the caller must then
        verify the remote owner and submission marker before reading task data.
        """
        if record.origin == "adopted":
            cluster_id, user = self._remote_account()
            if cluster_id != record.remote_cluster_id:
                raise ConflictError("task belongs to a different remote cluster", code="binding_mismatch")
            if user["id"] != record.remote_user_id:
                raise ConflictError("task belongs to a different authenticated account", code="ownership_mismatch")
            return {
                "mode": "adopted",
                "profile_matches": None,
                "cluster_identity_matches": True,
                "verified": ["remote_cluster", "remote_owner"],
                "mutations_allowed": True,
                "message": "adopted task verified against the live cluster ID and account",
            }
        identity_matches = record.cluster_identity == self._cluster_identity()
        profile_matches = record.profile_hash == self.profile.fingerprint
        if identity_matches and profile_matches:
            return {
                "mode": "profile",
                "profile_matches": True,
                "cluster_identity_matches": True,
                "verified": [],
                "mutations_allowed": True,
                "message": "bound to the current compute profile and endpoint",
            }
        if identity_matches and purpose == "observe":
            return {
                "mode": "cross_profile",
                "profile_matches": False,
                "cluster_identity_matches": True,
                "verified": [],
                "mutations_allowed": False,
                "message": _CROSS_PROFILE_MESSAGE,
            }
        if identity_matches:
            raise ConflictError(
                f"task was submitted with a different compute profile; {_CROSS_PROFILE_MESSAGE}. "
                "Read-only status, logs and usage remain available",
                code="binding_mismatch",
            )
        raise ConflictError(
            "task belongs to a different compute profile or cluster",
            code="binding_mismatch",
        )

    def _current_user_id(self) -> str:
        user = self.client.get_current_user()
        try:
            return DeterminedAPIClient.normalize_user_id(
                user.get("id") if isinstance(user, Mapping) else None
            )
        except ValueError as exc:
            raise APIError(
                "Remote account identity is unavailable", code="invalid_response"
            ) from exc

    def _verify_cross_profile(
        self, record: TaskRecord, entity: Any, user_id: str, binding: Dict[str, Any]
    ) -> None:
        """Prove that a remote entity is this record's submission and the caller owns it."""
        self._check_remote_entity(record.kind, record.remote_id, entity, user_id)
        marker = entity.get("submissionMarker")
        legacy_match = (
            record.name is None
            and record.description is None
            and any(
                self._legacy_description_marker(description, record.submission_marker)
                for description in self._entity_descriptions(entity)
            )
        )
        if marker != record.submission_marker and not legacy_match:
            raise ConflictError(
                "remote task identity marker does not match this task record",
                code="identity_mismatch",
            )
        binding["verified"] = ["remote_owner", "submission_marker"]

    @staticmethod
    def _public(record: TaskRecord) -> Dict[str, Any]:
        result = record.public_dict()
        if record.remote_id is None and record.state in {
            "pending",
            "submitting",
            "submission_uncertain",
        }:
            result["recovery"] = {
                "action": "reconcile",
                "safe_to_resubmit": False,
                "message": (
                    "The remote outcome is not bound. Inspect Determined and reconcile "
                    "a matching remote id; do not relaunch this request_id."
                ),
            }
        return result

    @staticmethod
    def _attach_task_details(exc: BaseException, record: TaskRecord) -> None:
        safe_details = {"task_id": record.task_id, "request_id": record.request_id}
        try:
            setattr(exc, "details", safe_details)
            setattr(exc, "task_id", record.task_id)
        except Exception:
            pass

    @staticmethod
    def _with_submission_marker(config: Mapping[str, Any], marker: str) -> Dict[str, Any]:
        result = copy.deepcopy(dict(config))
        environment = result.get("environment")
        if not isinstance(environment, Mapping):
            raise ValidationError("config environment must be an object")
        environment = copy.deepcopy(dict(environment))
        variables = environment.get("environment_variables")
        if not isinstance(variables, list) or not all(
            isinstance(item, str) for item in variables
        ):
            raise ValidationError(
                "config environment.environment_variables must be a list of strings"
            )
        for item in variables:
            if item.split("=", 1)[0] == _SUBMISSION_MARKER_VARIABLE:
                raise ValidationError(
                    f"{_SUBMISSION_MARKER_VARIABLE} is reserved for task identity"
                )
        environment["environment_variables"] = list(variables) + [
            f"{_SUBMISSION_MARKER_VARIABLE}={marker}"
        ]
        result["environment"] = environment
        return result

    @staticmethod
    def _legacy_description_marker(
        description: Optional[str], marker: str
    ) -> bool:
        if not description:
            return False
        # Backward compatibility only: pre-metadata jobs used the first line.
        return description.splitlines()[0] == marker

    @staticmethod
    def _entity_descriptions(entity: Any) -> Sequence[str]:
        if not isinstance(entity, Mapping):
            return ()
        candidates: Sequence[Any] = (
            entity.get("description"),
            entity.get("config", {}).get("description")
            if isinstance(entity.get("config"), Mapping)
            else None,
        )
        return tuple(candidate for candidate in candidates if isinstance(candidate, str))


__all__ = ["ComputeService"]
