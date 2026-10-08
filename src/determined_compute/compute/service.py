"""Transport-independent planning and stateless control of Determined tasks."""

from __future__ import annotations

import copy
import math
import posixpath
import re
import shlex
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

from determined_compute.core.api_client import DeterminedAPIClient

from .models import (
    APIError,
    ConflictError,
    NotFoundError,
    SubmissionUncertainError,
    ValidationError,
)
from .profile import ComputeProfile


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
    "parent",
    "inherit_context",
    "pausable",
    "preemption_timeout",
}
# Request fields that only a generic task accepts.
_GENERIC_FIELDS = ("parent", "inherit_context", "pausable", "preemption_timeout")
_TASK_KINDS = {"command", "shell", "experiment", "generic"}
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
    """Plan and launch tasks, and act on Determined tasks owned by the account.

    The service keeps no task records: Determined's own task IDs address every task.
    """

    def __init__(
        self,
        client: Any,
        profile: ComputeProfile,
        inspector: Any = None,
    ) -> None:
        self.client = client
        self.profile = profile
        self.inspector = inspector
        self._user: Optional[Dict[str, str]] = None
        self._user_lock = threading.Lock()

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize a request without contacting Determined."""

        if not isinstance(request, Mapping):
            raise ValidationError("request must be an object")
        unknown = set(request) - _REQUEST_FIELDS
        if unknown:
            raise ValidationError(f"request has unknown fields: {sorted(unknown)}")
        _reject_upload_fields(request)

        raw_kind = request.get("kind", "auto")
        if raw_kind != "auto" and raw_kind not in _TASK_KINDS:
            raise ValidationError("kind must be auto, command, shell, generic, or experiment")
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
        task_options = self._generic_options(request) if kind == "generic" else None
        if kind != "generic":
            present = [field for field in _GENERIC_FIELDS if field in request]
            if present:
                raise ValidationError(f"{', '.join(present)} require generic kind")

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
            )
            if task_options is not None and task_options["preemption_timeout"] is not None:
                config["preemption_timeout"] = task_options["preemption_timeout"]

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
        if task_options is not None and task_options["pausable"]:
            advisories.append(
                {
                    "code": "generic_restart_safety",
                    "message": (
                        "A generic task is never restarted automatically, and resuming a "
                        "paused one runs the command again from the start in a new "
                        "container. Make the command skip completed outputs and resume or "
                        "clean up partial ones."
                    ),
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
        # Only generic plans carry this key.
        if task_options is not None:
            result["task_options"] = {
                key: task_options[key] for key in ("parent", "inherit_context", "pausable")
            }
        return result

    @staticmethod
    def _generic_options(request: Mapping[str, Any]) -> Dict[str, Any]:
        parent = request.get("parent")
        if parent is not None:
            if not isinstance(parent, str):
                raise ValidationError("parent must be a generic task ID (a UUID) or null")
            try:
                parent = str(uuid.UUID(parent))
            except ValueError as exc:
                raise ValidationError(
                    "parent must be a generic task ID (a UUID) or null"
                ) from exc
        inherit_context = request.get("inherit_context", False)
        pausable = request.get("pausable", False)
        if not isinstance(inherit_context, bool) or not isinstance(pausable, bool):
            raise ValidationError("inherit_context and pausable must be booleans")
        if inherit_context and parent is None:
            raise ValidationError("inherit_context requires parent")
        timeout = request.get("preemption_timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0
        ):
            raise ValidationError("preemption_timeout must be a non-negative integer of seconds")
        return {
            "parent": parent,
            "inherit_context": inherit_context,
            "pausable": pausable,
            "preemption_timeout": timeout,
        }

    def _base_config(
        self,
        kind: str,
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
    ) -> Dict[str, Any]:
        variables = [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
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
    ) -> Dict[str, Any]:
        config = self._base_config(
            kind, workdir, output_dir, slots, pool, image, code_revision
        )
        if kind == "generic":
            # Generic tasks have native display fields; masters without them are
            # handled when the task is submitted.
            config = {
                "name": name,
                **({"description": description} if description is not None else {}),
                **config,
            }
        else:
            config["description"] = name + (
                ("\n" + description) if description is not None else ""
            )
        command = request.get("command")
        if kind in {"command", "generic"}:
            config["entrypoint"] = [
                "/bin/bash",
                "-lc",
                self._render_entrypoint(command, workdir, output_dir),
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
        managed_names = {
            "COMPUTE_WORKDIR",
            "COMPUTE_OUTPUT_DIR",
            "COMPUTE_CODE_REVISION",
            _SUBMISSION_MARKER_VARIABLE,
        }
        for item in variables:
            if item.split("=", 1)[0] in managed_names:
                raise ValidationError("compute-managed environment variables cannot be overridden")
        variables = list(variables) + [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
        environment["environment_variables"] = variables
        config["environment"] = environment
        config["bind_mounts"] = [mount.as_config() for mount in self.profile.mounts]
        if command is not None:
            if "entrypoint" in config:
                raise ValidationError(
                    "provide command or experiment_config.entrypoint, not both"
                )
            config["entrypoint"] = self._render_entrypoint(command, workdir, output_dir)
        elif "entrypoint" in config:
            config["entrypoint"] = self._render_entrypoint(
                config["entrypoint"], workdir, output_dir
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
    def _render_entrypoint(command: Any, workdir: str, output_dir: str) -> str:
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
        # The prelude ends in "|| exit $?" on its own line, so a failed mkdir or cd exits with
        # its status before the shell reads any statement of the command, whatever its form.
        prelude = f"mkdir -p {shlex.quote(output_dir)} && cd {shlex.quote(workdir)}"
        return f"{prelude} || exit $?\n{rendered}"

    def launch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a request once and return Determined's own task ID.

        Nothing is recorded locally. A launch whose outcome is unknown is never
        retried; the error carries the submission marker for compute_list.
        """
        plan = self.plan(request)
        kind = plan["kind"]
        if kind == "generic":
            # Refuse before anything is created when the master could not report the new
            # task's owner, since such a task could not be managed afterwards.
            self.client.require_generic_task_list()
        launch_options = (
            self._generic_launch_options(plan["task_options"]) if kind == "generic" else None
        )
        if not plan["allow_queue"]:
            self._inspector().require_capacity(kind, plan["config"])
        marker = f"determined-compute:{uuid.uuid4()}"
        launch_config = self._with_submission_marker(plan["config"], marker)
        try:
            if launch_options is None:
                entity = self.client.launch_task(kind, launch_config)
            else:
                entity = self.client.launch_task(kind, launch_config, launch_options)
            remote_id = self._launched_id(kind, entity)
        except SubmissionUncertainError as exc:
            raise self._uncertain(kind, marker, str(exc), exc.details) from exc
        except APIError:
            # The master answered with a definite rejection, so nothing was submitted.
            raise
        except Exception as exc:
            raise self._uncertain(kind, marker, "the launch failed unexpectedly") from exc
        result: Dict[str, Any] = {
            "kind": kind,
            "id": self._public_id(kind, remote_id),
            "name": plan["name"],
            "description": plan["description"],
            "state": _remote_state(entity),
            "submission_marker": marker,
            "advisories": plan["advisories"],
        }
        if kind == "shell" and isinstance(entity.get("reconnectCommand"), str):
            result["reconnect_command"] = entity["reconnectCommand"]
        warnings = entity.get("warnings") if launch_options is not None else None
        if isinstance(warnings, list) and warnings:
            result["warnings"] = warnings
        return result

    @classmethod
    def _launched_id(cls, kind: str, entity: Any) -> str:
        if not isinstance(entity, Mapping) or entity.get("id") is None:
            raise SubmissionUncertainError("launch response did not contain a task id")
        try:
            return cls._canonical_id(kind, entity["id"])
        except ValidationError as exc:
            raise SubmissionUncertainError("launch response contained a malformed task id") from exc

    @staticmethod
    def _uncertain(
        kind: str, marker: str, reason: str, cause: Any = None
    ) -> SubmissionUncertainError:
        error = SubmissionUncertainError(
            f"The {kind} submission is unconfirmed ({reason}); Determined may or may not have "
            f"created it, and it may still appear. Look for it with compute_list(kind={kind!r}, "
            f"marker={marker!r}). An empty result does not prove that the submission failed. "
            "Do not launch again automatically: whether to resubmit is the user's decision "
            "after checking."
        )
        details: Dict[str, Any] = {"kind": kind, "submission_marker": marker}
        if isinstance(cause, Mapping) and cause.get("source") == "proxy":
            # An HTTP proxy, not Determined, answered; keep that label for the caller.
            details.update(
                (key, cause[key]) for key in ("source", "status_code", "proxy_error") if key in cause
            )
        error.details = details
        return error

    def _generic_launch_options(self, options: Mapping[str, Any]) -> Dict[str, Any]:
        """Verify a generic task's parent before launching under it."""
        # Resuming a paused generic task reruns it from the start, so a task is pausable only
        # when the request asks for it. Always send the value: masters differ in how they treat
        # an unset noPause.
        result: Dict[str, Any] = {"noPause": not options["pausable"]}
        if options["parent"] is not None:
            _kind, parent_id, _entity = self._owned("generic", options["parent"])
            result["parentId"] = parent_id
            if options["inherit_context"]:
                result["inheritContext"] = True
        return result

    def _inspector(self) -> Any:
        if self.inspector is None:
            from .admission import ResourceInspector

            self.inspector = ResourceInspector(self.client)
        return self.inspector

    def status(self, kind: str, task_id: Any) -> Dict[str, Any]:
        """Return the current remote state of one task owned by the account."""
        kind, remote_id, entity = self._owned(kind, task_id)
        result = self._summary(kind, remote_id, entity)
        marker = entity.get("submissionMarker")
        if isinstance(marker, str):
            result["submission_marker"] = marker
        result["remote"] = entity
        return result

    def logs(self, kind: str, task_id: Any, tail: int = 100) -> List[Any]:
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValidationError("tail must be a positive integer")
        kind, remote_id, _entity = self._owned(kind, task_id)
        result = self.client.task_logs(kind, remote_id, tail)
        if not isinstance(result, list):
            raise APIError("task log response was not a list", code="invalid_api_response")
        return result

    def usage(
        self,
        kind: str,
        task_id: Any,
        window_seconds: int = 3600,
        allocation_id: Optional[str] = None,
        trial_id: Optional[int] = None,
        metrics: Optional[Sequence[str]] = None,
        include_samples: bool = False,
    ) -> Dict[str, Any]:
        """Summarize measured CPU, memory, and GPU use of one owned task."""
        kind = self._task_kind(kind)
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
        if trial_id is not None and kind != "experiment":
            raise ValidationError("trial_id applies only to experiment tasks")

        kind, remote_id, entity = self._owned(kind, task_id)
        if not self.client.task_resources_enabled():
            raise APIError(
                "task resource monitoring is not enabled on this Determined master",
                code="task_resources_disabled",
            )
        trial = self._usage_trial(remote_id, trial_id) if kind == "experiment" else None
        determined_task_id = trial["task_id"] if trial is not None else remote_id
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

        pool_name = self._remote_text(entity.get("resourcePool"), 256)
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
            "kind": kind,
            "id": self._public_id(kind, remote_id),
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

    def cancel(self, kind: str, task_id: Any) -> Dict[str, Any]:
        kind, remote_id, entity = self._owned(kind, task_id)
        response = self.client.cancel_task(kind, remote_id)
        result = self._summary(kind, remote_id, entity)
        state = _remote_state(response)
        if state is not None:
            result["state"] = state
        result["cancellation_acknowledged"] = True
        result["remote"] = response
        return result

    def pause(self, kind: str, task_id: Any) -> Dict[str, Any]:
        """Pause an experiment or a generic task; its containers stop and it keeps its ID."""
        return self._pause_control(kind, task_id, "pause")

    def resume(self, kind: str, task_id: Any) -> Dict[str, Any]:
        """Resume a paused experiment or generic task."""
        return self._pause_control(kind, task_id, "resume")

    def _pause_control(self, kind: str, task_id: Any, action: str) -> Dict[str, Any]:
        kind = self._task_kind(kind)
        if kind not in {"experiment", "generic"}:
            raise ValidationError(
                f"{action} applies only to experiments and generic tasks; this task is a {kind}",
                code="unsupported_kind",
            )
        kind, remote_id, entity = self._owned(kind, task_id)
        operation = self.client.pause_task if action == "pause" else self.client.unpause_task
        response = operation(kind, remote_id)
        result = self._summary(kind, remote_id, entity)
        result[f"{action}_acknowledged"] = True
        result["remote"] = response
        return result

    def list_tasks(
        self,
        kind: str,
        limit: int = 50,
        offset: int = 0,
        marker: Optional[str] = None,
        states: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """List one page of the account's tasks of one kind, newest first.

        With states, Determined lists only experiments or generic tasks in those states, and
        every returned task is checked against them.
        With a marker, each task of that page is read once and every task whose stored config
        carries the marker is returned. A marker is a correlation label, not an identity: a
        config copied outside this service carries the same one, so several tasks can match.
        """
        kind = self._task_kind(kind)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")
        if marker is not None:
            marker = DeterminedAPIClient._valid_submission_marker(marker)
            if marker is None:
                raise ValidationError(
                    "marker must be a submission marker of the form determined-compute:<uuid>"
                )
        try:
            states = DeterminedAPIClient.validate_list_states(kind, states)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        user = self._account()
        page = self.client.list_remote_tasks(
            kind, user_id=user["id"], limit=limit, offset=offset, states=states
        )
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
        tasks: List[Dict[str, Any]] = []
        for listed in page["tasks"]:
            self._check_remote_owner(kind, listed, user["id"])
            try:
                remote_id = self._canonical_id(kind, listed.get("id"))
            except ValidationError as exc:
                raise APIError("Remote task identity is malformed", code="invalid_response") from exc
            if marker is None:
                tasks.append(self._summary(kind, remote_id, listed))
                continue
            entity = self.client.get_task(kind, remote_id)
            self._check_remote_entity(kind, remote_id, entity, user["id"])
            if entity.get("submissionMarker") == marker:
                tasks.append({
                    **self._summary(kind, remote_id, {**listed, **entity}),
                    "submission_marker": marker,
                })
        next_offset = offset + len(page["tasks"])
        result: Dict[str, Any] = {
            "kind": kind,
            "account": user,
            "tasks": tasks,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": pagination["total"],
                "next_offset": (
                    next_offset
                    if next_offset > offset and next_offset < pagination["total"]
                    else None
                ),
            },
        }
        if states is not None:
            result["filters"] = {"states": states}
        if marker is not None:
            result["marker"] = marker
            result["searched"] = len(page["tasks"])
        return result

    @staticmethod
    def _task_kind(kind: Any) -> str:
        if not isinstance(kind, str) or kind not in _TASK_KINDS:
            raise ValidationError("kind must be command, shell, generic, or experiment")
        return kind

    @staticmethod
    def _canonical_id(kind: str, value: Any) -> str:
        if kind == "experiment":
            try:
                return DeterminedAPIClient.normalize_user_id(value)
            except ValueError as exc:
                raise ValidationError("experiment id must be a positive integer") from exc
        if not isinstance(value, str):
            raise ValidationError("command, shell, and generic task id must be a UUID")
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValidationError("command, shell, and generic task id must be a UUID") from exc

    @staticmethod
    def _public_id(kind: str, remote_id: str) -> Any:
        return int(remote_id) if kind == "experiment" else remote_id

    def _account(self) -> Dict[str, str]:
        """Return the authenticated account; credentials are fixed for the process."""
        if self._user is None:
            with self._user_lock:
                if self._user is None:
                    user = self.client.get_current_user()
                    if (
                        not isinstance(user, Mapping)
                        or not isinstance(user.get("username"), str)
                        or not user["username"].strip()
                    ):
                        raise APIError("Remote account identity is unavailable", code="invalid_response")
                    try:
                        user_id = DeterminedAPIClient.normalize_user_id(user.get("id"))
                    except ValueError as exc:
                        raise APIError(
                            "Remote account identity is unavailable", code="invalid_response"
                        ) from exc
                    self._user = {"id": user_id, "username": user["username"].strip()}
        return dict(self._user)

    def _owned(self, kind: Any, task_id: Any) -> tuple[str, str, Dict[str, Any]]:
        """Fetch one remote task and verify that the authenticated account owns it."""
        kind = self._task_kind(kind)
        remote_id = self._canonical_id(kind, task_id)
        user = self._account()
        entity = self.client.get_task(kind, remote_id)
        self._check_remote_entity(kind, remote_id, entity, user["id"])
        return kind, remote_id, entity

    @staticmethod
    def _check_remote_owner(kind: str, entity: Any, user_id: str) -> None:
        if not isinstance(entity, Mapping):
            raise APIError("Remote task response is malformed", code="invalid_response")
        try:
            task_user_id = DeterminedAPIClient.normalize_user_id(entity.get("userId"))
        except ValueError as exc:
            hint = (
                "; a generic task's owner needs a master with the generic task list "
                "(WU-CVGL/determined#27)"
                if kind == "generic"
                else ""
            )
            raise APIError(
                "Determined did not report the owner of this task, so it cannot be checked "
                "against the authenticated account" + hint,
                code="ownership_unavailable",
            ) from exc
        if task_user_id != user_id:
            raise ConflictError(
                "task is not owned by the authenticated account",
                code="ownership_mismatch",
            )

    def _check_remote_entity(self, kind: str, remote_id: str, entity: Any, user_id: str) -> None:
        self._check_remote_owner(kind, entity, user_id)
        try:
            actual_id = self._canonical_id(kind, entity.get("id"))
        except ValidationError as exc:
            raise APIError("Remote task identity is malformed", code="invalid_response") from exc
        if actual_id != remote_id:
            raise APIError("Remote task identity does not match the request", code="invalid_response")

    @staticmethod
    def _remote_text(value: Any, limit: int = 4096) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text[:limit] if text else None

    @classmethod
    def _summary(cls, kind: str, remote_id: str, entity: Mapping[str, Any]) -> Dict[str, Any]:
        """Whitelisted, display-oriented fields of one remote task."""
        description = cls._remote_text(entity.get("description"))
        name = cls._remote_text(entity.get("name"), 256) or cls._remote_text(
            entity.get("displayName"), 256
        )
        if name is None and description:
            # Commands and shells carry the name on the first description line.
            name = description.splitlines()[0][:256]
        return {
            "kind": kind,
            "id": cls._public_id(kind, remote_id),
            "name": name,
            "description": description,
            "state": _remote_state(entity),
            "username": cls._remote_text(entity.get("username"), 256),
            "resource_pool": cls._remote_text(entity.get("resourcePool"), 256),
            "start_time": cls._remote_text(entity.get("startTime"), 128),
            "end_time": cls._remote_text(entity.get("endTime"), 128),
        }

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
                    f"{_SUBMISSION_MARKER_VARIABLE} is reserved for the submission marker"
                )
        environment["environment_variables"] = list(variables) + [
            f"{_SUBMISSION_MARKER_VARIABLE}={marker}"
        ]
        result["environment"] = environment
        return result


__all__ = ["ComputeService"]
