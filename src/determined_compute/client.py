"""Determined REST access: transport, auth, the protocol gate, submissions and their reads.

Errors carry a stable ``code``. The gateway answers ``{"error": {"code", "reason", "error"}}``
with the gRPC code name in ``reason``; HTTP statuses are shared between codes (400 is both
InvalidArgument and FailedPrecondition, 409 both AlreadyExists and Aborted), so errors are
classified by ``reason`` and never by status alone.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
import yaml

from determined_compute.utils.secrets import load_secrets

# Submission protocol 1 is the job ledger: submit options, plan binding, and the durable
# Get/List/CancelSubmission reads. A later MCP that needs more raises this.
MIN_SUBMISSION_PROTOCOL = 1
KINDS = ("command", "shell", "experiment")
_ADMISSIONS = {"queue": "ADMISSION_QUEUE", "immediate": "ADMISSION_IMMEDIATE"}
_MESSAGE_LIMIT = 4096
_RETRY_SAME_KEY = (
    "; the job may have been created, so retry with the same idempotency key, which returns "
    "that job instead of creating another"
)
_DIGEST = re.compile(r"\b[0-9a-f]{64}\b")
_JOB_ID = re.compile(r"\bjob ([0-9A-Za-z-]+)")
# The reason a bare status stands for when a proxy, not the gateway, answered.
_STATUS_REASONS = {
    400: "InvalidArgument",
    401: "Unauthenticated",
    403: "PermissionDenied",
    404: "NotFound",
    429: "ResourceExhausted",
    500: "Internal",
    501: "Unimplemented",
    502: "Unavailable",
    503: "Unavailable",
    504: "DeadlineExceeded",
}
_RETRYABLE_REASONS = {"Unavailable", "DeadlineExceeded", "Aborted", "ResourceExhausted"}
# gRPC code numbers, for bodies that carry only the number, such as a log stream's error.
_GRPC_REASONS = {
    1: "Canceled",
    2: "Unknown",
    3: "InvalidArgument",
    4: "DeadlineExceeded",
    5: "NotFound",
    6: "AlreadyExists",
    7: "PermissionDenied",
    8: "ResourceExhausted",
    9: "FailedPrecondition",
    10: "Aborted",
    11: "OutOfRange",
    12: "Unimplemented",
    13: "Internal",
    14: "Unavailable",
    15: "DataLoss",
    16: "Unauthenticated",
}
STATES = ("queued", "running", "paused", "completed", "failed", "canceled", "deleted")


class APIError(RuntimeError):
    """A failed Determined call. ``code`` is stable; ``details`` is JSON-safe."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details: Dict[str, Any] = details or {}


def _malformed(what: str, *, retryable: bool = False) -> APIError:
    message = f"Determined returned a malformed {what}"
    return APIError(message, code="invalid_response", retryable=retryable)


@dataclass(frozen=True)
class _Call:
    """What an error means depends on the call: a dry run, a keyed create, or neither."""

    dry_run: bool = False
    keyed: bool = False
    immediate: bool = False


def _classify(reason: str, message: str, call: _Call) -> APIError:
    if reason == "FailedPrecondition" and message.startswith("plan_changed"):
        digests = _DIGEST.findall(message)
        details = (
            {"request_digest": digests[0], "expected_digest": digests[1]}
            if len(digests) == 2
            else {}
        )
        return APIError(message, code="plan_changed", details=details)
    if reason == "AlreadyExists":
        match = _JOB_ID.search(message)
        return APIError(
            message, code="key_conflict", details={"job_id": match.group(1)} if match else {}
        )
    if call.immediate and (
        reason == "Unimplemented"
        or (reason == "InvalidArgument" and "immediate admission" in message)
    ):
        return APIError(message, code="admission_unsupported")
    # Every experiment config error is Internal; before anything is created it is a plan error.
    if reason in {"InvalidArgument", "FailedPrecondition", "OutOfRange"} or (
        call.dry_run and reason in {"Internal", "Unknown"}
    ):
        return APIError(message, code="invalid_request")
    if reason == "NotFound":
        return APIError(message, code="not_found")
    if reason in {"Unauthenticated", "PermissionDenied"}:
        return APIError(message, code="permission_denied", details={"reason": reason})
    if reason == "Unimplemented":
        # The protocol gate promised every route the MCP calls.
        return APIError(message, code="protocol_unsupported")
    retryable = reason in _RETRYABLE_REASONS
    code = "unavailable" if retryable else "internal"
    # A keyed create whose outcome is unknown is safe to repeat: the master replays it.
    if call.keyed:
        return APIError(message + _RETRY_SAME_KEY, code=code, retryable=True)
    return APIError(message, code=code, retryable=retryable)


def _gateway_error(error: Any, status: int, code_key: str, call: _Call) -> APIError:
    """Classify a gateway error object, falling back to its gRPC number, then the status."""

    error = error if isinstance(error, dict) else {}
    reason = error.get("reason")
    if not isinstance(reason, str) or not reason:
        number = error.get(code_key)
        reason = (_GRPC_REASONS.get(number) if isinstance(number, int) else None) or (
            _STATUS_REASONS.get(status, "Internal" if status >= 500 else "InvalidArgument")
        )
    message = error.get("error") or error.get("message")
    if not isinstance(message, str) or not message:
        message = f"Determined answered HTTP {status}"
    return _classify(reason, message[:_MESSAGE_LIMIT], call)


def _error_from_response(response: requests.Response, call: _Call) -> APIError:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    return _gateway_error(error, response.status_code, "code", call)


def _transport_error(exc: Exception, call: _Call) -> APIError:
    message = f"Determined did not answer ({type(exc).__name__})"
    if call.keyed:
        message += _RETRY_SAME_KEY
    return APIError(message, code="unavailable", retryable=True)


def normalize_api_url(api_url: Optional[str]) -> str:
    url = api_url or "http://localhost:8080"
    if not url.startswith(("http://", "https://")):
        if url.count(":") > 1 and not url.startswith("["):
            url = f"[{url}]"
        parts = urlsplit(f"http://{url}")
        netloc = parts.netloc if parts.port is not None else f"{parts.netloc}:8080"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        # Errors and logs may name the URL; credentials belong in the token or the secrets file.
        raise ValueError("the master URL must not carry credentials")
    return url.rstrip("/")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() in {"1", "true", "yes", "y", "on"}


def _master_from_environment() -> Optional[str]:
    for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST"):
        if os.environ.get(name):
            return os.environ[name]
    return None


# Redaction


def _sensitive(key: str) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    embedded = ("password", "passwd", "secret", "credential", "authorization", "cookie")
    return (
        normalized in {"auth", "token"}
        or any(part in normalized for part in embedded)
        or normalized.endswith(
            ("auth", "apikey", "accesskey", "sessionkey", "privatekey", "token")
        )
    )


def redact(value: Any) -> Any:
    """Drop credential-like keys, including a shell's private key, and hide environments.

    A config held as text (an experiment's original YAML) is dropped, because it cannot be
    redacted field by field.
    """

    if isinstance(value, Mapping):
        safe: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            if normalized == "originalconfig" or (normalized == "config" and isinstance(item, str)):
                continue
            if _sensitive(str(key)):
                continue
            if normalized in {"environmentvariables", "proxyenvironmentvariables"}:
                safe[key] = "[redacted]"
            else:
                safe[key] = redact(item)
        return safe
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# Parsing


def _enum(value: Any, prefix: str, what: str) -> Optional[str]:
    """``PREFIX_NAME`` as ``name``; the unspecified value is None."""

    if not isinstance(value, str) or not value.startswith(prefix):
        raise _malformed(what)
    name = value[len(prefix):].lower()
    return None if name == "unspecified" else name


def _text(value: Any, what: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _malformed(what)
    return value


def _integer(value: Any, what: str, *, optional: bool = False) -> Optional[int]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(what)
    return value


def _allocation(value: Any) -> Dict[str, Any]:
    what = "allocation"
    if not isinstance(value, Mapping):
        raise _malformed(what)
    placements = value.get("placements") or []
    detail = value.get("exitDetail")
    is_ready = value.get("isReady")
    if (
        not isinstance(placements, list)
        or (detail is not None and not isinstance(detail, Mapping))
        or (is_ready is not None and not isinstance(is_ready, bool))
    ):
        raise _malformed(what)
    parsed_placements = []
    for placement in placements:
        if not isinstance(placement, Mapping):
            raise _malformed(what)
        uuids = placement.get("acceleratorUuids") or []
        if not isinstance(uuids, list) or not all(isinstance(item, str) for item in uuids):
            raise _malformed(what)
        parsed_placements.append(
            {"node": _text(placement.get("node"), what), "accelerator_uuids": uuids}
        )
    allocation_id = _text(value.get("allocationId"), what)
    if not allocation_id:
        raise _malformed(what)
    return {
        "allocation_id": allocation_id,
        # The ledger reports pending and assigned allocations as queued, and an allocation that
        # ended before it started has no end time, so its state says whether it ended.
        "state": _enum(value.get("state"), "STATE_", what),
        "is_ready": is_ready,
        "start_time": _text(value.get("startTime"), what, optional=True),
        "end_time": _text(value.get("endTime"), what, optional=True),
        "slots": _integer(value.get("slots"), what),
        "resource_pool": _text(value.get("resourcePool"), what) or None,
        "exit_class": _enum(value.get("exitClass"), "EXIT_CLASS_", what),
        "exit_reason": _text(value.get("exitReason"), what, optional=True) or None,
        "exit_detail": dict(detail) if detail is not None else None,
        "status_code": _integer(value.get("statusCode"), what, optional=True),
        "placements": parsed_placements,
    }


def parse_submission(value: Any) -> Dict[str, Any]:
    """A ``Submission`` in snake case, with enums as short lowercase names."""

    what = "submission"
    if not isinstance(value, Mapping):
        raise _malformed(what)
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise _malformed(what)
    parsed_tasks = []
    for task in tasks:
        allocations = task.get("allocations") if isinstance(task, Mapping) else None
        if not isinstance(allocations, list):
            raise _malformed(what)
        parsed_tasks.append(
            {
                "task_id": _text(task.get("taskId"), what),
                "trial_id": _integer(task.get("trialId"), what, optional=True),
                "allocations": [_allocation(item) for item in allocations],
            }
        )
    job_id = _text(value.get("jobId"), what)
    if not job_id:
        raise _malformed(what)
    return {
        "job_id": job_id,
        "kind": _enum(value.get("kind"), "SUBMISSION_KIND_", what),
        "entity_id": _text(value.get("entityId"), what) or None,
        "name": _text(value.get("name"), what),
        "owner_id": _integer(value.get("ownerId"), what),
        "owner": _text(value.get("owner"), what),
        "workspace_id": _integer(value.get("workspaceId"), what),
        "project_id": _integer(value.get("projectId"), what, optional=True),
        "idempotency_key": _text(value.get("idempotencyKey"), what, optional=True),
        "request_digest": _text(value.get("requestDigest"), what, optional=True),
        "admission": _enum(value.get("admission"), "ADMISSION_", what),
        "submitted_at": _text(value.get("submittedAt"), what, optional=True),
        "ended_at": _text(value.get("endedAt"), what, optional=True),
        "state": _enum(value.get("state"), "SUBMISSION_STATE_", what),
        "exit_class": _enum(value.get("exitClass"), "EXIT_CLASS_", what),
        "exit_reason": _text(value.get("exitReason"), what) or None,
        "tasks": parsed_tasks,
    }


def _submit_result(response: Mapping[str, Any], dry_run: bool, keyed: bool) -> Dict[str, Any]:
    # A malformed answer to a keyed create leaves its outcome unknown; a replay resolves it.
    malformed = _malformed("submit result", retryable=keyed)
    result = response.get("submission")
    warnings = response.get("warnings") or []
    if not isinstance(result, Mapping) or not isinstance(warnings, list):
        raise malformed
    job_id, digest = result.get("jobId") or None, result.get("requestDigest")
    replayed, config = result.get("replayed"), result.get("effectiveConfig")
    if (
        not isinstance(digest, str)
        or not digest
        or not isinstance(replayed, bool)
        or (job_id is None) != dry_run
        or (job_id is not None and not isinstance(job_id, str))
        or (config is not None and not isinstance(config, Mapping))
    ):
        raise malformed
    try:
        outcome = _enum(result.get("outcome"), "ADMISSION_OUTCOME_", "submit result")
        parsed_warnings = [_enum(item, "LAUNCH_WARNING_", "warning") for item in warnings]
    except APIError:
        raise malformed from None
    return {
        "job_id": job_id,
        "replayed": replayed,
        "request_digest": digest,
        "outcome": outcome,
        "effective_config": redact(config) if config is not None else None,
        "warnings": [item for item in parsed_warnings if item is not None],
    }


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _pool(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise _malformed("resource pool")
    slot_type = value.get("slotType")
    pool_type = value.get("type")
    description = value.get("description")
    return {
        "name": value["name"],
        "description": description if isinstance(description, str) and description else None,
        "type": _enum(pool_type, "RESOURCE_POOL_TYPE_", "resource pool")
        if pool_type is not None
        else None,
        "num_agents": _count(value.get("numAgents")),
        "slots_available": _count(value.get("slotsAvailable")),
        "slots_used": _count(value.get("slotsUsed")),
        "slot_type": _enum(slot_type, "TYPE_", "resource pool") if slot_type is not None else None,
        # -1 means the pool has no agent to take the count from.
        "slots_per_agent": _count(value.get("slotsPerAgent")),
        "aux_container_capacity": _count(value.get("auxContainerCapacity")),
        "aux_containers_running": _count(value.get("auxContainersRunning")),
    }


def _agent(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
        raise _malformed("agent")
    slots = value.get("slots")
    entries = list(slots.values()) if isinstance(slots, Mapping) else slots or []
    pools = value.get("resourcePools") or []
    if not isinstance(entries, list) or not isinstance(pools, list):
        raise _malformed("agent")
    devices = []
    for slot in entries:
        device = slot.get("device") if isinstance(slot, Mapping) else None
        if not isinstance(device, Mapping):
            continue
        uuid, brand, kind = device.get("uuid"), device.get("brand"), device.get("type")
        devices.append(
            {
                "type": kind[len("TYPE_"):].lower() if isinstance(kind, str) else None,
                "brand": brand[:256] if isinstance(brand, str) and brand else None,
                # RBAC without sensitive-agent access replaces UUIDs with asterisks.
                "uuid": uuid if isinstance(uuid, str) and uuid.strip("*") else None,
            }
        )
    return {
        "id": value["id"],
        "resource_pools": [item for item in pools if isinstance(item, str)],
        "enabled": value.get("enabled") is True,
        "draining": value.get("draining") is True,
        "devices": devices,
    }


_TASK_RESOURCE_LABELS = (
    ("allocationId", "allocation_id"),
    ("node", "node"),
    ("gpuUuid", "gpu_uuid"),
)


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _task_resources(response: Mapping[str, Any]) -> Dict[str, Any]:
    malformed = _malformed("task-resources response")
    enabled, series, warnings = (response.get(key) for key in ("enabled", "series", "warnings"))
    if (
        not isinstance(enabled, bool)
        or not isinstance(series, list)
        or not isinstance(warnings, list)
    ):
        raise malformed
    parsed: List[Dict[str, Any]] = []
    for item in series:
        metric = item.get("metric") if isinstance(item, Mapping) else None
        if not isinstance(metric, str) or not metric:
            raise malformed
        labels = item.get("labels") or {}
        samples = item.get("samples")
        if not isinstance(labels, Mapping) or not isinstance(samples, list):
            raise malformed
        parsed_labels: Dict[str, Optional[str]] = {}
        for wire, name in _TASK_RESOURCE_LABELS:
            value = labels.get(wire)
            if value is not None and not isinstance(value, str):
                raise malformed
            parsed_labels[name] = value or None
        points: List[List[Any]] = []
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise malformed
            # A null or absent value is an unavailable sample, never zero.
            stamp, value = sample.get("timestampSeconds"), sample.get("value")
            if (
                not _finite(stamp)
                or not 0 <= stamp < 253402300800  # representable before year 10000
                or (value is not None and not _finite(value))
            ):
                raise malformed
            points.append([stamp, value])
        parsed.append({"metric": metric, "labels": parsed_labels, "samples": points})
    parsed_warnings: List[Dict[str, str]] = []
    for warning in warnings:
        if (
            not isinstance(warning, Mapping)
            or not isinstance(warning.get("code"), str)
            or not isinstance(warning.get("message"), str)
        ):
            raise malformed
        parsed_warnings.append({"code": warning["code"], "message": warning["message"]})
    return {"enabled": enabled, "series": parsed, "warnings": parsed_warnings}


def _log_entry(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _malformed("log line")
    text = value.get("log")
    if not isinstance(text, str):
        text = value.get("message") if isinstance(value.get("message"), str) else ""
    level = value.get("level")
    return {
        "timestamp": value.get("timestamp") if isinstance(value.get("timestamp"), str) else None,
        "level": level[len("LOG_LEVEL_"):].lower() if isinstance(level, str) else None,
        "source": value.get("source") if isinstance(value.get("source"), str) else None,
        "stdtype": value.get("stdtype") if isinstance(value.get("stdtype"), str) else None,
        "allocation_id": value.get("allocationId") if isinstance(value.get("allocationId"), str)
        else None,
        "rank_id": _count(value.get("rankId")),
        "log": text,
    }


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError(f"{name} must be a non-empty printable string")
    return quote(value, safe="")


class Client:
    """One Determined master, reached as one authenticated user.

    Construction touches no network. The first authenticated call checks the submission
    protocol and then logs in if only a username and password are configured, so no request
    reaches a master that would not understand the submit options.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_token: Optional[str] = None,
        secrets_path: Optional[Path] = None,
        verify_ssl: Optional[bool] = None,
    ) -> None:
        secrets = load_secrets(secrets_path)
        secret_master = next(
            (secrets[name] for name in ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST")
             if secrets.get(name)),
            None,
        )
        self.api_url = normalize_api_url(api_url or _master_from_environment() or secret_master)
        self.verify_ssl = _bool_env("DET_VERIFY_SSL", False) if verify_ssl is None else verify_ssl
        self._token = api_token or os.environ.get("DET_API_TOKEN") or secrets.get("DET_API_TOKEN")
        username = secrets.get("DET_USERNAME") or os.environ.get("DET_USERNAME")
        password = secrets.get("DET_PASSWORD") or os.environ.get("DET_PASSWORD")
        self._login = (username, password) if not self._token and username and password else None
        self._protocol: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Client({self.api_url!r})"

    # Transport

    def _url(self, endpoint: str) -> str:
        return urljoin(self.api_url + "/", endpoint)

    def _headers(self) -> Dict[str, str]:
        with self._lock:
            if self._protocol is None:
                self._protocol = self._check_protocol()
            if self._token is None and self._login is not None:
                self._token = self._log_in(*self._login)
            token = self._token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _log_in(self, username: str, password: str) -> str:
        try:
            response = requests.post(
                self._url("api/v1/auth/login"),
                json={"username": username, "password": password},
                timeout=15,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise _transport_error(exc, _Call()) from None
        if response.status_code >= 400:
            raise _error_from_response(response, _Call())
        token = self._json(response, _Call()).get("token")
        if not isinstance(token, str) or not token:
            raise _malformed("login response")
        return token

    @staticmethod
    def _json(response: requests.Response, call: _Call) -> Dict[str, Any]:
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise _malformed("response", retryable=call.keyed)
        return data

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = self._headers()
        try:
            response = requests.get(
                self._url(endpoint), headers=headers, params=params, timeout=30,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise _transport_error(exc, _Call()) from None
        if response.status_code >= 400:
            raise _error_from_response(response, _Call())
        return self._json(response, _Call())

    def _post(self, endpoint: str, body: Mapping[str, Any], call: _Call) -> Dict[str, Any]:
        headers = self._headers()
        try:
            response = requests.post(
                self._url(endpoint), headers=headers, json=body, timeout=60,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise _transport_error(exc, call) from None
        if response.status_code >= 400:
            raise _error_from_response(response, call)
        return self._json(response, call)

    # Protocol gate

    def check_protocol(self) -> Dict[str, Any]:
        """Read the master's submission protocol without logging in, and refuse an old one.

        Returns ``{"submission_protocol"}``. The release string is never the gate, and appears
        only in the refusal: local builds report the previous tag and release candidates the
        next one.
        """

        with self._lock:
            self._protocol = self._check_protocol()
        return self._protocol

    def _check_protocol(self) -> Dict[str, Any]:
        try:
            response = requests.get(self._url("api/v1/master"), timeout=15, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise _transport_error(exc, _Call()) from None
        if response.status_code >= 400:
            raise _error_from_response(response, _Call())
        info = self._json(response, _Call())
        version = info.get("version") if isinstance(info.get("version"), str) else "unknown"
        protocol = info.get("submissionProtocol")
        minimum = MIN_SUBMISSION_PROTOCOL
        if isinstance(protocol, bool) or not isinstance(protocol, int):
            raise APIError(
                f"the Determined master (release {version}) has no submission protocol; this "
                f"MCP needs protocol {minimum} or later",
                code="protocol_unsupported",
                details={"required": minimum, "version": version},
            )
        if protocol < minimum:
            raise APIError(
                f"the Determined master (release {version}) speaks submission protocol "
                f"{protocol}; this MCP needs protocol {minimum} or later",
                code="protocol_unsupported",
                details={"required": minimum, "found": protocol, "version": version},
            )
        return {"submission_protocol": protocol}

    # Submissions

    def submit(
        self,
        kind: str,
        config: Mapping[str, Any],
        *,
        files: Sequence[Mapping[str, Any]] = (),
        workspace_id: Optional[int] = None,
        project_id: Optional[int] = None,
        dry_run: bool = False,
        idempotency_key: Optional[str] = None,
        expected_digest: Optional[str] = None,
        admission: str = "queue",
    ) -> Dict[str, Any]:
        """Create a command, shell or experiment, or check one with ``dry_run``.

        A dry run never carries a key: the master would replay the keyed job instead of
        checking the request. Anything else needs one, so a repeat after an unknown outcome
        replays rather than duplicates. ``files`` is the task context in the wire form
        ``code.plan_context`` builds; for an experiment it is the model definition.

        Returns ``job_id`` (None for a dry run), ``replayed``, ``request_digest``, ``outcome``
        (None for a dry run), ``effective_config`` (redacted; only for a dry run) and
        ``warnings``.
        """

        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping")
        if admission not in _ADMISSIONS:
            raise ValueError(f"admission must be one of {', '.join(_ADMISSIONS)}")
        if dry_run and idempotency_key is not None:
            raise ValueError("a dry run must not carry an idempotency key")
        if not dry_run and not idempotency_key:
            raise ValueError("a create needs an idempotency key")
        if kind == "experiment" and workspace_id is not None:
            raise ValueError("an experiment takes project_id, not workspace_id")
        if kind != "experiment" and project_id is not None:
            raise ValueError(f"a {kind} takes workspace_id, not project_id")
        options: Dict[str, Any] = {"admission": _ADMISSIONS[admission]}
        if dry_run:
            options["dry_run"] = True
        else:
            options["idempotency_key"] = idempotency_key
        if expected_digest is not None:
            options["expected_digest"] = expected_digest
        body: Dict[str, Any]
        if kind == "experiment":
            # The experiment route takes YAML text; the digest is over the parsed config.
            body = {"config": yaml.safe_dump(dict(config), sort_keys=False), "activate": True}
            if files:
                body["model_definition"] = list(files)
            if project_id is not None:
                body["project_id"] = project_id
        else:
            body = {"config": dict(config)}
            if files:
                body["files"] = list(files)
            if workspace_id is not None:
                body["workspace_id"] = workspace_id
        body["submit"] = options
        call = _Call(dry_run=dry_run, keyed=not dry_run, immediate=admission == "immediate")
        # The response's entity is never returned: a shell's carries its private key.
        response = self._post(f"api/v1/{kind}s", body, call)
        return _submit_result(response, dry_run, call.keyed)

    def get_submission(self, job_id: str) -> Dict[str, Any]:
        response = self._get(f"api/v1/submissions/{_identifier(job_id, 'job_id')}")
        return parse_submission(response.get("submission"))

    def list_submissions(
        self,
        *,
        kind: Optional[str] = None,
        state: Optional[str] = None,
        submitted_after: Optional[str] = None,
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The caller's jobs, newest first, and the token of the next page or None."""

        params: Dict[str, Any] = {}
        if kind is not None:
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            params["kind"] = f"SUBMISSION_KIND_{kind.upper()}"
        if state is not None:
            if state not in STATES:
                raise ValueError(f"state must be one of {', '.join(STATES)}")
            params["state"] = f"SUBMISSION_STATE_{state.upper()}"
        if submitted_after is not None:
            if not isinstance(submitted_after, str) or not submitted_after:
                raise ValueError("submitted_after must be an RFC 3339 timestamp")
            params["submittedAfter"] = submitted_after
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ValueError("limit must be an integer from 1 to 1000")
            params["limit"] = limit
        if page_token:
            params["pageToken"] = page_token
        response = self._get("api/v1/submissions", params=params)
        submissions = response.get("submissions")
        token = response.get("nextPageToken") or None
        if not isinstance(submissions, list) or (token is not None and not isinstance(token, str)):
            raise _malformed("submission list")
        return {
            "submissions": [parse_submission(item) for item in submissions],
            "next_page_token": token,
        }

    def cancel_submission(self, job_id: str) -> Dict[str, Any]:
        """Record a cancel and return the job as it stands; it ends shortly after."""

        response = self._post(
            f"api/v1/submissions/{_identifier(job_id, 'job_id')}/cancel", {}, _Call()
        )
        return parse_submission(response.get("submission"))

    # Logs and trials

    def task_logs(self, task_id: str, tail: int = 200) -> List[Dict[str, Any]]:
        """The last ``tail`` log lines of a task, oldest first."""

        if isinstance(tail, bool) or not isinstance(tail, int) or not 0 <= tail <= 10000:
            raise ValueError("tail must be an integer from 0 to 10000")
        if tail == 0:
            return []
        endpoint = f"api/v1/tasks/{_identifier(task_id, 'task_id')}/logs"
        params = {"limit": tail, "follow": False, "orderBy": "ORDER_BY_DESC"}
        entries = [_log_entry(item) for item in self._stream(endpoint, params)]
        entries.reverse()
        return entries

    def _stream(self, endpoint: str, params: Dict[str, Any]) -> List[Any]:
        headers = self._headers()
        try:
            response = requests.get(
                self._url(endpoint), headers=headers, params=params, timeout=30,
                verify=self.verify_ssl, stream=True,
            )
        except requests.RequestException as exc:
            raise _transport_error(exc, _Call()) from None
        results: List[Any] = []
        try:
            if response.status_code >= 400:
                raise _error_from_response(response, _Call())
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    raise _malformed("log stream") from None
                if not isinstance(item, dict):
                    raise _malformed("log stream")
                error = item.get("error")
                if error:
                    status = error.get("httpCode") if isinstance(error, dict) else None
                    status = status if isinstance(status, int) else 500
                    raise _gateway_error(error, status, "grpcCode", _Call())
                results.append(item.get("result"))
        except requests.RequestException as exc:
            raise _transport_error(exc, _Call()) from None
        finally:
            response.close()
        return results

    def get_trial(self, trial_id: int) -> Dict[str, Any]:
        if isinstance(trial_id, bool) or not isinstance(trial_id, int) or trial_id < 1:
            raise ValueError("trial_id must be a positive integer")
        trial = self._get(f"api/v1/trials/{trial_id}").get("trial")
        if not isinstance(trial, Mapping) or trial.get("id") != trial_id:
            raise _malformed("trial")
        return dict(trial)

    # Task resources

    def task_resources_enabled(self) -> bool:
        enabled = self._get("api/v1/task-resources/capability").get("enabled")
        if not isinstance(enabled, bool):
            raise _malformed("task-resources capability")
        return enabled

    def get_task_resources(
        self,
        task_id: str,
        *,
        start: int,
        end: int,
        step: int,
        allocation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Measurement series of one task between ``start`` and ``end`` Unix seconds."""

        for name, value in (("start", start), ("end", end), ("step", step)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        # Send only these keys: the gateway lets an unfiltered taskId override the path.
        params: Dict[str, Any] = {"start": start, "end": end, "step": step}
        if allocation_id is not None:
            if not isinstance(allocation_id, str) or not allocation_id:
                raise ValueError("allocation_id must be a non-empty string")
            params["allocationId"] = allocation_id
        endpoint = f"api/v1/tasks/{_identifier(task_id, 'task_id')}/resources"
        return _task_resources(self._get(endpoint, params=params))

    # Cluster

    def list_resource_pools(self) -> List[Dict[str, Any]]:
        pools = self._get("api/v1/resource-pools", params={"limit": 0}).get("resourcePools")
        if not isinstance(pools, list):
            raise _malformed("resource-pool list")
        return [_pool(item) for item in pools]

    def list_agents(self) -> List[Dict[str, Any]]:
        agents = self._get("api/v1/agents", params={"limit": 0}).get("agents")
        if not isinstance(agents, list):
            raise _malformed("agent list")
        return [_agent(item) for item in agents]


__all__ = [
    "APIError",
    "Client",
    "KINDS",
    "MIN_SUBMISSION_PROTOCOL",
    "STATES",
    "normalize_api_url",
    "parse_submission",
    "redact",
]
