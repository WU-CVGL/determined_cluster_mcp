"""Reliable, dependency-light access to the Determined REST API."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
import yaml

from determined_compute.utils.secrets import load_secrets

ErrorCode = Union[int, str, None]


class APIError(RuntimeError):
    def __init__(self, message: str, *, code: ErrorCode = None, details: Any = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.details, self.retryable = code, details, retryable


class SubmissionUncertainError(APIError):
    """A mutation failed after dispatch, so its server-side outcome is unknown."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, code="submission_uncertain", details=details, retryable=False)


def _normalize_api_url(api_url: Optional[str]) -> str:
    url = api_url or os.environ.get("DET_MASTER") or os.environ.get("DET_MASTER_ADDR") or os.environ.get("DET_MASTER_HOST")
    if not url:
        url = "http://localhost:8080"
    elif url.startswith(("http://", "https://")):
        return url.rstrip("/")
    else:
        if url.count(":") > 1 and not url.startswith("["):
            url = f"[{url}]"
        parts = urlsplit(f"http://{url}")
        netloc = parts.netloc if parts.port is not None else f"{parts.netloc}:8080"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url.rstrip("/")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() in {"1", "true", "yes", "y", "on"}


def _error_from_response(response: requests.Response) -> APIError:
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status = response.status_code
    error = payload.get("error")
    if isinstance(error, dict):
        # The gRPC gateway nests its message: {"error": {"code", "reason", "error"}}.
        error = error.get("error") or error.get("message") or error.get("reason")
    message = payload.get("message") or error or response.text or getattr(response, "reason", "API request failed")
    return APIError(
        f"{status} {message}", code=payload.get("code", status), details=payload.get("details"),
        # 501 means the master lacks the route; repeating the request cannot help.
        retryable=status == 429 or (status >= 500 and status != 501),
    )


def _login_for_token(api_url: str, username: str, password: str, verify_ssl: bool) -> str:
    endpoint = urljoin(api_url.rstrip("/") + "/", "api/v1/auth/login")
    try:
        response = requests.post(endpoint, headers={"Content-Type": "application/json"}, json={"username": username, "password": password}, timeout=15, verify=verify_ssl)
    except requests.RequestException as exc:
        raise APIError("Could not authenticate with Determined", code="transport_error", details={"endpoint": "api/v1/auth/login"}, retryable=True) from exc
    if response.status_code >= 400:
        raise _error_from_response(response)
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as exc:
        raise APIError("Determined login returned invalid JSON", code="invalid_response") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not token:
        raise APIError("Determined login response did not contain a token", code="invalid_response")
    return str(token)


# A strict generic-task config parse rejects unknown keys before the master persists
# anything; older masters do not know the optional display fields.
_GENERIC_METADATA_FIELDS = ("name", "description")
_UNKNOWN_GENERIC_METADATA = re.compile(r'unknown field \\?"(?:name|description)\\?"')
_GENERIC_STATE_PREFIX = "GENERIC_TASK_STATE_"


class DeterminedAPIClient:
    _TASK_KINDS = {"command", "shell", "experiment", "generic"}
    # Kinds whose remote listing can be filtered by the owning account.
    _LISTABLE_KINDS = {"command", "shell", "experiment"}
    _REMOTE_TASK_FIELDS = (
        "id",
        "userId",
        "username",
        "name",
        "displayName",
        "description",
        "state",
        "resourcePool",
        "startTime",
        "endTime",
    )

    def __init__(self, api_url: Optional[str] = None, api_token: Optional[str] = None, secrets_path: Optional[Path] = None, verify_ssl: Optional[bool] = None) -> None:
        secrets = load_secrets(secrets_path)
        secret_master = secrets.get("DET_MASTER") or secrets.get("DET_MASTER_ADDR") or secrets.get("DET_MASTER_HOST")
        environment_master = (
            os.environ.get("DET_MASTER")
            or os.environ.get("DET_MASTER_ADDR")
            or os.environ.get("DET_MASTER_HOST")
        )
        self.api_url = _normalize_api_url(api_url or environment_master or secret_master)
        self.verify_ssl = _bool_env("DET_VERIFY_SSL", False) if verify_ssl is None else verify_ssl
        self.api_token = self._resolve_token(api_token, secrets)
        self.headers: Dict[str, str] = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    def _url(self, endpoint: str) -> str:
        return urljoin(self.api_url.rstrip("/") + "/", endpoint)

    @staticmethod
    def _json_response(response: requests.Response, *, mutation: bool = False) -> Dict[str, Any]:
        if response.status_code >= 400:
            error = _error_from_response(response)
            if mutation and response.status_code >= 500:
                raise SubmissionUncertainError(
                    "Determined mutation outcome is unknown after a server error",
                    details={"status_code": response.status_code, "error": str(error)},
                ) from error
            raise error
        if not getattr(response, "content", None) and not response.text:
            return {}
        try:
            data = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as exc:
            if mutation:
                raise SubmissionUncertainError("Determined returned invalid JSON after accepting the request", details={"status_code": response.status_code}) from exc
            raise APIError("Determined returned invalid JSON", code="invalid_response") from exc
        if not isinstance(data, dict):
            if mutation:
                raise SubmissionUncertainError("Determined returned a non-object JSON response")
            raise APIError("Determined returned a non-object JSON response", code="invalid_response")
        return data

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = requests.get(self._url(endpoint), headers=self.headers, params=params, timeout=30, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise APIError("Determined request failed", code="transport_error", details={"endpoint": endpoint}, retryable=True) from exc
        return self._json_response(response)

    def _post(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            response = requests.post(self._url(endpoint), headers={**self.headers, "Content-Type": "application/json"}, json=data, timeout=60, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise SubmissionUncertainError("Determined mutation outcome is unknown", details={"endpoint": endpoint}) from exc
        return self._json_response(response, mutation=True)


    def _stream_logs(self, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            response = requests.get(self._url(endpoint), headers=self.headers, params=params, timeout=30, verify=self.verify_ssl, stream=True)
        except requests.RequestException as exc:
            raise APIError("Determined log request failed", code="transport_error", details={"endpoint": endpoint}, retryable=True) from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        logs: List[Dict[str, Any]] = []
        try:
            try:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        item = json.loads(raw_line)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise APIError("Determined log stream contained invalid JSON", code="invalid_response") from exc
                    if not isinstance(item, dict):
                        raise APIError("Determined log stream contained a non-object item", code="invalid_response")
                    if item.get("error"):
                        error = item["error"]
                        if isinstance(error, dict):
                            http_code = error.get("httpCode") or 0
                            raise APIError(str(error.get("message") or "Determined log stream failed"), code=error.get("grpcCode") or http_code, details=error.get("details"), retryable=http_code >= 500)
                        raise APIError(str(error))
                    result = item.get("result")
                    if not isinstance(result, dict):
                        raise APIError("Determined log stream item had no result", code="invalid_response")
                    logs.append(result)
            except requests.RequestException as exc:
                raise APIError(
                    "Determined log stream failed", code="transport_error",
                    details={"endpoint": endpoint}, retryable=True,
                ) from exc
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        return logs

    def _resolve_token(self, api_token: Optional[str], secrets: Dict[str, str]) -> Optional[str]:
        if api_token:
            return api_token
        if os.environ.get("DET_API_TOKEN"):
            return os.environ["DET_API_TOKEN"]
        if secrets.get("DET_API_TOKEN"):
            return secrets["DET_API_TOKEN"]
        username = secrets.get("DET_USERNAME") or os.environ.get("DET_USERNAME")
        password = secrets.get("DET_PASSWORD") or os.environ.get("DET_PASSWORD")
        return _login_for_token(self.api_url, username, password, self.verify_ssl) if username and password else None

    @classmethod
    def _kind(cls, kind: str) -> str:
        normalized = kind.lower().rstrip("s")
        if normalized not in cls._TASK_KINDS:
            raise ValueError("kind must be one of: command, shell, generic, experiment")
        return normalized

    @staticmethod
    def normalize_user_id(value: Any) -> str:
        """Return a canonical positive numeric Determined user ID."""
        if isinstance(value, bool):
            raise ValueError("user_id must be a positive numeric ID")
        if isinstance(value, int):
            user_id = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            user_id = int(value)
        else:
            raise ValueError("user_id must be a positive numeric ID")
        if user_id <= 0:
            raise ValueError("user_id must be a positive numeric ID")
        return str(user_id)

    def get_current_user(self) -> Dict[str, str]:
        response = self._get("api/v1/me")
        user = response.get("user")
        if not isinstance(user, Mapping):
            raise APIError("Current-user response is malformed", code="invalid_response")
        try:
            user_id = self.normalize_user_id(user.get("id"))
        except ValueError as exc:
            raise APIError("Current-user response is malformed", code="invalid_response") from exc
        username = user.get("username")
        if not isinstance(username, str) or not username.strip():
            raise APIError("Current-user response is malformed", code="invalid_response")
        return {"id": user_id, "username": username.strip()}

    def get_cluster_id(self) -> str:
        response = self._get("info")
        cluster_id = response.get("cluster_id")
        if not isinstance(cluster_id, str):
            raise APIError("Cluster-info response is malformed", code="invalid_response")
        cluster_id = cluster_id.strip()
        if not cluster_id or len(cluster_id.encode("utf-8")) > 256:
            raise APIError("Cluster-info response is malformed", code="invalid_response")
        return cluster_id

    def list_remote_tasks(
        self,
        kind: str,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if kind not in self._LISTABLE_KINDS:
            raise ValueError("kind must be one of: command, shell, experiment")
        normalized_user_id = self.normalize_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        response = self._get(
            f"api/v1/{kind}s",
            params={
                "userIds": [int(normalized_user_id)],
                "limit": limit,
                "offset": offset,
                "orderBy": "ORDER_BY_DESC",
                "sortBy": "SORT_BY_START_TIME",
            },
        )
        collection = response.get(f"{kind}s")
        if not isinstance(collection, list):
            raise APIError("Remote-task response is malformed", code="invalid_response")
        tasks: List[Dict[str, Any]] = []
        for item in collection:
            if not isinstance(item, Mapping):
                raise APIError("Remote-task response is malformed", code="invalid_response")
            summary: Dict[str, Any] = {}
            for key in self._REMOTE_TASK_FIELDS:
                if key not in item:
                    continue
                value = item[key]
                if key in {"id", "userId"}:
                    valid = not isinstance(value, bool) and isinstance(value, (int, str))
                else:
                    valid = value is None or isinstance(value, str)
                if not valid:
                    raise APIError("Remote-task response is malformed", code="invalid_response")
                summary[key] = value
            tasks.append(summary)
        pagination = response.get("pagination")
        if not isinstance(pagination, Mapping):
            raise APIError("Remote-task pagination is malformed", code="invalid_response")
        pagination_fields = ("limit", "offset", "startIndex", "endIndex", "total")
        normalized_pagination: Dict[str, int] = {}
        for field in pagination_fields:
            value = pagination.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise APIError("Remote-task pagination is malformed", code="invalid_response")
            normalized_pagination[field] = value
        return {"tasks": tasks, "pagination": normalized_pagination}

    @staticmethod
    def _entity(response: Dict[str, Any], kind: str, *, mutation: bool = False) -> Dict[str, Any]:
        entity = response.get(kind)
        if not isinstance(entity, dict) or entity.get("id") is None:
            message = f"Determined response did not contain a {kind} with an id"
            if mutation:
                raise SubmissionUncertainError(message, details=response)
            raise APIError(message, code="invalid_response", details=response)
        return entity

    @staticmethod
    def _safe_shell(entity: Dict[str, Any]) -> Dict[str, Any]:
        safe = DeterminedAPIClient._redact_secrets(entity)
        safe.setdefault("reconnectCommand", f"det shell show_ssh_command {safe['id']}")
        return safe

    @staticmethod
    def _redact_secrets(value: Any) -> Any:
        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            for key, item in value.items():
                normalized = "".join(char for char in key.lower() if char.isalnum())
                # Determined experiment entities may carry the submitted YAML in
                # originalConfig.  A string-valued config is equally opaque to
                # recursive redaction; get_task adds a parsed, redacted mapping
                # from the response-level config separately when one is available.
                if normalized == "originalconfig" or (
                    normalized == "config" and isinstance(item, str)
                ):
                    continue
                embedded = (
                    "password",
                    "passwd",
                    "secret",
                    "credential",
                    "authorization",
                    "cookie",
                )
                sensitive_key = (
                    normalized in {"auth", "token"}
                    or any(part in normalized for part in embedded)
                    or any(
                        normalized.endswith(suffix)
                        for suffix in ("apikey", "accesskey", "sessionkey", "privatekey")
                    )
                    or normalized.endswith("token")
                )
                if sensitive_key:
                    continue
                if normalized in {"environmentvariables", "proxyenvironmentvariables"}:
                    safe[key] = "[redacted]"
                else:
                    safe[key] = DeterminedAPIClient._redact_secrets(item)
            return safe
        if isinstance(value, list):
            return [DeterminedAPIClient._redact_secrets(item) for item in value]
        return value

    @staticmethod
    def _valid_submission_marker(value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value.startswith("determined-compute:"):
            return None
        identifier = value.partition(":")[2]
        try:
            parsed = uuid.UUID(identifier)
        except (ValueError, AttributeError):
            return None
        canonical = f"determined-compute:{parsed}"
        return canonical if value == canonical else None

    @classmethod
    def _marker_from_environment_variables(cls, value: Any) -> Optional[str]:
        if isinstance(value, str):
            name, separator, marker = value.partition("=")
            if separator and name == "COMPUTE_SUBMISSION_MARKER":
                return cls._valid_submission_marker(marker)
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                marker = cls._marker_from_environment_variables(item)
                if marker is not None:
                    return marker
            return None
        if isinstance(value, Mapping):
            direct = value.get("COMPUTE_SUBMISSION_MARKER")
            marker = cls._valid_submission_marker(direct)
            if marker is not None:
                return marker
            # Determined may return platform sections such as cpu/cuda/rocm.
            for item in value.values():
                marker = cls._marker_from_environment_variables(item)
                if marker is not None:
                    return marker
        return None

    @classmethod
    def _submission_marker_from_config(cls, config: Any) -> Optional[str]:
        if isinstance(config, str):
            try:
                config = yaml.safe_load(config)
            except yaml.YAMLError:
                return None
        if not isinstance(config, Mapping):
            return None
        environment = config.get("environment")
        nested_variables = (
            environment.get("environment_variables")
            if isinstance(environment, Mapping)
            else None
        )
        for variables in (nested_variables, config.get("environment_variables")):
            marker = cls._marker_from_environment_variables(variables)
            if marker is not None:
                return marker
        return None

    @staticmethod
    def _upload_fields(value: Any, path: str = "config") -> List[str]:
        forbidden = {
            "context",
            "contextdir",
            "contextpath",
            "data",
            "files",
            "includes",
            "modeldefinition",
            "projectroot",
            "upload",
            "uploadcontext",
            "uploads",
        }
        found: List[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                nested_path = f"{path}.{key}"
                normalized = "".join(char for char in str(key).lower() if char.isalnum())
                if normalized in forbidden:
                    found.append(nested_path)
                found.extend(DeterminedAPIClient._upload_fields(item, nested_path))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                found.extend(DeterminedAPIClient._upload_fields(item, f"{path}[{index}]"))
        return found

    def launch_task(
        self, kind: str, config: Dict[str, Any], options: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        kind = self._kind(kind)
        if not isinstance(config, dict):
            raise TypeError("config must be a dictionary")
        present = self._upload_fields(config)
        if present:
            raise ValueError("Code/data uploads are not supported; remove: " + ", ".join(present))
        if kind == "generic":
            return self._launch_generic_task(config, options or {})
        if options:
            raise ValueError("launch options apply only to generic tasks")
        body = {"config": yaml.safe_dump(config, sort_keys=False), "activate": True} if kind == "experiment" else {"config": config}
        entity = self._entity(self._post(f"api/v1/{kind}s", data=body), kind, mutation=True)
        return self._safe_shell(entity) if kind == "shell" else entity

    _GENERIC_OPTIONS = {"parentId": str, "inheritContext": bool, "noPause": bool}

    def _launch_generic_task(
        self, config: Dict[str, Any], options: Mapping[str, Any]
    ) -> Dict[str, Any]:
        for key, value in options.items():
            expected = self._GENERIC_OPTIONS.get(key)
            if expected is None or not isinstance(value, expected):
                raise ValueError(f"unsupported generic task launch option: {key}")
        # Code and data stay on shared mounts, so the context directory is always empty.
        body: Dict[str, Any] = {"contextDirectory": [], **options}
        warnings: List[Dict[str, str]] = []
        try:
            response = self._post(
                "api/v1/generic-tasks",
                data={**body, "config": yaml.safe_dump(config, sort_keys=False)},
            )
        except APIError as exc:
            stripped = {
                key: value for key, value in config.items() if key not in _GENERIC_METADATA_FIELDS
            }
            if stripped == config or not self._rejected_generic_metadata(exc):
                raise
            # The rejection happened while parsing the config, before the master stored
            # anything, so this second request is the only possible submission.
            response = self._post(
                "api/v1/generic-tasks",
                data={**body, "config": yaml.safe_dump(stripped, sort_keys=False)},
            )
            warnings.append({
                "code": "generic_task_metadata_unsupported",
                "message": (
                    "The Determined master does not accept generic task name and description; "
                    "the task was submitted without them and they are kept only locally."
                ),
            })
        task_id = response.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise SubmissionUncertainError(
                "Determined response did not contain a generic task id", details=response
            )
        launch_warnings = response.get("warnings") or []
        if isinstance(launch_warnings, list):
            warnings.extend(
                {"code": "launch_warning", "message": item}
                for item in launch_warnings
                if isinstance(item, str) and item
            )
        return {"id": task_id, "warnings": warnings}

    @staticmethod
    def _rejected_generic_metadata(exc: APIError) -> bool:
        """Return whether the master rejected only the optional generic display fields."""
        # Masters report the strict config parse failure as HTTP 500 (an untyped error)
        # or, in some builds, HTTP 400; a 500 reaches us as an uncertain mutation.
        rejection = exc.__cause__ if isinstance(exc, SubmissionUncertainError) else exc
        return (
            isinstance(rejection, APIError)
            and not isinstance(rejection, SubmissionUncertainError)
            and rejection.code in {400, 500}
            and _UNKNOWN_GENERIC_METADATA.search(str(rejection)) is not None
        )

    def _get_generic_task(self, task_id: str) -> Dict[str, Any]:
        path = f"api/v1/tasks/{quote(str(task_id), safe='')}"
        task = self._get(path).get("task")
        if not isinstance(task, Mapping) or task.get("taskId") != task_id:
            raise APIError("Determined response did not contain the task", code="invalid_response")
        if task.get("taskType") != "TASK_TYPE_GENERIC":
            raise APIError("Determined task is not a generic task", code="kind_mismatch")
        entity = self._redact_secrets(dict(task))
        entity.pop("submissionMarker", None)
        entity["id"] = task_id
        raw_state = task.get("taskState")
        entity["state"] = (
            "STATE_" + raw_state[len(_GENERIC_STATE_PREFIX):]
            if isinstance(raw_state, str) and raw_state.startswith(_GENERIC_STATE_PREFIX)
            else None
        )
        config: Any = self._get(f"{path}/config").get("config")
        if isinstance(config, str):
            try:
                config = yaml.safe_load(config) if config else None
            except yaml.YAMLError:
                config = None
        if isinstance(config, Mapping):
            marker = self._submission_marker_from_config(config)
            if marker is not None:
                entity["submissionMarker"] = marker
            entity["config"] = self._redact_secrets(dict(config))
            resources = config.get("resources")
            pool = resources.get("resource_pool") if isinstance(resources, Mapping) else None
            if isinstance(pool, str) and pool:
                entity["resourcePool"] = pool
            for field in _GENERIC_METADATA_FIELDS:
                if isinstance(config.get(field), str):
                    entity[field] = config[field]
        return entity

    def get_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        kind = self._kind(kind)
        if kind == "generic":
            return self._get_generic_task(task_id)
        response = self._get(f"api/v1/{kind}s/{task_id}")
        entity = self._redact_secrets(dict(self._entity(response, kind)))
        entity.pop("submissionMarker", None)
        config = response.get("config")
        marker = self._submission_marker_from_config(config)
        if marker is not None:
            entity["submissionMarker"] = marker
        if isinstance(config, str):
            try:
                config = yaml.safe_load(config)
            except yaml.YAMLError:
                config = None
        if isinstance(config, Mapping):
            entity["config"] = self._redact_secrets(dict(config))
        return self._safe_shell(entity) if kind == "shell" else entity

    def task_logs(self, kind: str, task_id: str, tail: int = 100) -> List[Dict[str, Any]]:
        kind = self._kind(kind)
        if tail < 0:
            raise ValueError("tail must be non-negative")
        if kind == "experiment":
            trial = self.get_latest_trial(task_id)["trial"]
            if trial is None:
                return []
            return self.get_trial_logs(str(trial["id"]), limit=tail)
        entries = self._stream_logs(f"api/v1/tasks/{task_id}/logs", {"limit": tail, "follow": False, "orderBy": "ORDER_BY_DESC"})
        entries.reverse()
        return entries

    def _generic_control(self, task_id: str, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._post(
                f"api/v1/tasks/{quote(str(task_id), safe='')}/{action}", data=body
            )
        except SubmissionUncertainError as exc:
            # The master reports rejected preconditions, such as a task that is already
            # paused, as untyped server errors; keep that message visible to the caller.
            if isinstance(exc.__cause__, APIError):
                raise SubmissionUncertainError(
                    f"Determined {action} outcome is unknown after a server error: {exc.__cause__}",
                    details=exc.details,
                ) from exc.__cause__
            raise
        return {"id": str(task_id), "acknowledged": True, **response}

    _PAUSABLE_KINDS = ("experiment", "generic")

    def pause_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        """Pause an experiment, or a generic task and its pausable descendants."""
        kind = self._kind(kind)
        if kind == "experiment":
            return self._experiment_control(task_id, "pause")
        if kind != "generic":
            raise ValueError("only experiments and generic tasks can be paused")
        return self._generic_control(task_id, "pause", {"taskId": str(task_id)})

    def unpause_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        """Resume a paused experiment, or a paused generic task in a new allocation."""
        kind = self._kind(kind)
        if kind == "experiment":
            # Determined calls resuming an experiment activating it.
            return self._experiment_control(task_id, "activate")
        if kind != "generic":
            raise ValueError("only experiments and generic tasks can be resumed")
        return self._generic_control(task_id, "unpause", {"taskId": str(task_id)})

    def _experiment_control(self, experiment_id: str, action: str) -> Dict[str, Any]:
        response = self._post(
            f"api/v1/experiments/{quote(str(experiment_id), safe='')}/{action}", data={}
        )
        # Like cancel, the response is empty; acknowledge without claiming a remote state.
        return {"id": str(experiment_id), "acknowledged": True, **response}

    def cancel_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        kind = self._kind(kind)
        if kind == "generic":
            # Killing a generic task also kills its descendants, never its ancestors.
            return self._generic_control(
                task_id, "kill", {"taskId": str(task_id), "killFromRoot": False}
            )
        action = "cancel" if kind == "experiment" else "kill"
        response = self._post(f"api/v1/{kind}s/{task_id}/{action}", data={})
        if kind == "experiment":
            # The schema intentionally defines an empty response.  Preserve
            # successful acknowledgement without claiming a remote state.
            return {"id": str(task_id), "acknowledged": True, **response}
        entity = self._entity(response, kind, mutation=True)
        return self._safe_shell(entity) if kind == "shell" else entity

    # Experiment log retrieval.


    def get_trials(self, experiment_id: str) -> List[Dict[str, Any]]:
        value = self._get(f"api/v1/experiments/{experiment_id}/trials").get("trials")
        if not isinstance(value, list):
            raise APIError("Trial list response had no trials array", code="invalid_response")
        return value

    @staticmethod
    def _trial_key(trial: Mapping[str, Any]) -> tuple:
        try:
            return (1, int(trial.get("id")))
        except (TypeError, ValueError):
            return (0, str(trial.get("id") or ""))

    def get_latest_trial(self, experiment_id: str) -> Dict[str, Any]:
        """Return the highest-id trial, or None, and the reported trial count."""
        # An unbounded trial listing is capped at 100 rows in ascending order.
        response = self._get(
            f"api/v1/experiments/{experiment_id}/trials",
            params={"sortBy": "SORT_BY_ID", "orderBy": "ORDER_BY_DESC", "limit": 1},
        )
        trials = response.get("trials")
        if not isinstance(trials, list) or not all(isinstance(item, Mapping) for item in trials):
            raise APIError("Trial list response had no trials array", code="invalid_response")
        pagination = response.get("pagination")
        total = pagination.get("total") if isinstance(pagination, Mapping) else None
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            total = None
        latest = max(trials, key=self._trial_key) if trials else None
        return {"trial": dict(latest) if latest is not None else None, "total": total}

    def get_trial(self, trial_id: str) -> Dict[str, Any]:
        trial = self._get(f"api/v1/trials/{quote(str(trial_id), safe='')}").get("trial")
        if not isinstance(trial, Mapping) or trial.get("id") is None:
            raise APIError("Trial response did not contain a trial", code="invalid_response")
        return dict(trial)

    def get_trial_logs(self, trial_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        entries = self._stream_logs(f"api/v1/trials/{trial_id}/logs", {"limit": limit, "follow": False, "orderBy": "ORDER_BY_DESC"})
        entries.reverse()
        return entries

    # Task resource measurements (Determined fork 0.40.1 and later).

    _TASK_RESOURCE_LABELS = (("allocationId", "allocation_id"), ("node", "node"), ("gpuUuid", "gpu_uuid"))

    def task_resources_enabled(self) -> bool:
        """Return whether the master serves task resource measurements."""
        try:
            response = self._get("api/v1/task-resources/capability")
        except APIError as exc:
            # A master without the route answers 501 through the gateway, or 404 behind a proxy.
            if exc.code in {404, 501}:
                raise APIError(
                    "Determined master does not provide the task resources API",
                    code="task_resources_unsupported",
                ) from exc
            raise
        enabled = response.get("enabled")
        if not isinstance(enabled, bool):
            raise APIError("Task-resources capability response is malformed", code="invalid_response")
        return enabled

    @staticmethod
    def _optional_text(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise APIError("Task response is malformed", code="invalid_response")
        return value

    def get_task_info(self, task_id: str) -> Dict[str, Any]:
        """Return the lifetime and allocations of one Determined task ID."""
        task = self._get(f"api/v1/tasks/{quote(task_id, safe='')}").get("task")
        if not isinstance(task, Mapping) or task.get("taskId") != task_id:
            raise APIError("Task response is malformed", code="invalid_response")
        allocations = task.get("allocations") or []
        if not isinstance(allocations, list):
            raise APIError("Task response is malformed", code="invalid_response")
        summaries: List[Dict[str, Any]] = []
        for item in allocations:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("allocationId"), str)
                or not item["allocationId"]
            ):
                raise APIError("Task response is malformed", code="invalid_response")
            is_ready = item.get("isReady")
            summaries.append({
                "allocation_id": item["allocationId"],
                "state": self._optional_text(item.get("state")),
                "is_ready": is_ready if isinstance(is_ready, bool) else None,
                "start_time": self._optional_text(item.get("startTime")),
                "end_time": self._optional_text(item.get("endTime")),
            })
        return {
            "task_id": task_id,
            "start_time": self._optional_text(task.get("startTime")),
            "end_time": self._optional_text(task.get("endTime")),
            "allocations": summaries,
        }

    def get_allocation(self, allocation_id: str) -> Dict[str, Any]:
        """Return the slot count and exit details of one allocation."""
        allocation = self._get(f"api/v1/allocations/{quote(allocation_id, safe='')}").get("allocation")
        malformed = APIError("Allocation response is malformed", code="invalid_response")
        if not isinstance(allocation, Mapping) or allocation.get("allocationId") != allocation_id:
            raise malformed
        slots, reason, status = (
            allocation.get(key) for key in ("slots", "exitReason", "statusCode")
        )
        if (
            isinstance(slots, bool)
            or not isinstance(slots, int)
            or slots < 0
            or (reason is not None and not isinstance(reason, str))
            or (status is not None and (isinstance(status, bool) or not isinstance(status, int)))
        ):
            raise malformed
        return {
            "allocation_id": allocation_id,
            "slots": slots,
            "exit_reason": reason[:1024] if reason else None,
            "status_code": status,
        }

    def list_resource_pools(self) -> List[Dict[str, Optional[str]]]:
        """Return each resource pool's name and operator-written description."""
        pools = self._get("api/v1/resource-pools", params={"limit": 0}).get("resourcePools")
        malformed = APIError("Resource-pool response is malformed", code="invalid_response")
        if not isinstance(pools, list):
            raise malformed
        result: List[Dict[str, Optional[str]]] = []
        for pool in pools:
            if not isinstance(pool, Mapping) or not isinstance(pool.get("name"), str):
                raise malformed
            description = pool.get("description")
            result.append({
                "name": pool["name"],
                "description": description if isinstance(description, str) and description else None,
            })
        return result

    def list_gpu_devices(self) -> Dict[str, str]:
        """Map each visible accelerator UUID to its model name."""
        agents = self._get("api/v1/agents", params={"limit": 0}).get("agents")
        if not isinstance(agents, list) or not all(isinstance(item, Mapping) for item in agents):
            raise APIError("Agent response is malformed", code="invalid_response")
        models: Dict[str, str] = {}
        for agent in agents:
            slots = agent.get("slots")
            if isinstance(slots, Mapping):
                entries = list(slots.values())
            else:
                entries = slots if isinstance(slots, list) else []
            for slot in entries:
                device = slot.get("device") if isinstance(slot, Mapping) else None
                if not isinstance(device, Mapping) or device.get("type") not in {"TYPE_CUDA", "TYPE_ROCM"}:
                    continue
                uuid_text, brand = device.get("uuid"), device.get("brand")
                # RBAC without sensitive-agent access replaces UUIDs with asterisks.
                if (
                    isinstance(uuid_text, str)
                    and uuid_text.strip("*")
                    and isinstance(brand, str)
                    and brand
                ):
                    models[uuid_text] = brand[:256]
        return models

    def get_task_resources(
        self,
        task_id: str,
        *,
        start: int,
        end: int,
        step: int,
        allocation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return validated measurement series for one Determined task ID."""
        for name, value in (("start", start), ("end", end), ("step", step)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if allocation_id is not None and (not isinstance(allocation_id, str) or not allocation_id):
            raise ValueError("allocation_id must be a non-empty string")
        # Send only these keys: the gateway lets an unfiltered taskId override the path.
        params: Dict[str, Any] = {"start": start, "end": end, "step": step}
        if allocation_id is not None:
            params["allocationId"] = allocation_id
        response = self._get(f"api/v1/tasks/{quote(task_id, safe='')}/resources", params=params)
        return self._task_resources(response)

    @classmethod
    def _task_resources(cls, response: Mapping[str, Any]) -> Dict[str, Any]:
        def number(value: Any) -> bool:
            return (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
            )

        malformed = APIError("Task-resources response is malformed", code="invalid_response")
        enabled, series, warnings = (response.get(key) for key in ("enabled", "series", "warnings"))
        if not isinstance(enabled, bool) or not isinstance(series, list) or not isinstance(warnings, list):
            raise malformed
        parsed: List[Dict[str, Any]] = []
        for item in series:
            if not isinstance(item, Mapping) or not isinstance(item.get("metric"), str) or not item["metric"]:
                raise malformed
            labels = item.get("labels") or {}
            samples = item.get("samples")
            if not isinstance(labels, Mapping) or not isinstance(samples, list):
                raise malformed
            parsed_labels: Dict[str, Optional[str]] = {}
            for wire, name in cls._TASK_RESOURCE_LABELS:
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
                    not number(stamp)
                    or not 0 <= stamp < 253402300800  # representable before year 10000
                    or (value is not None and not number(value))
                ):
                    raise malformed
                points.append([stamp, value])
            parsed.append({"metric": item["metric"], "labels": parsed_labels, "samples": points})
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


__all__ = ["APIError", "SubmissionUncertainError", "DeterminedAPIClient"]
