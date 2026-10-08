"""Reliable, dependency-light access to the Determined REST API."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
import yaml
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from determined_compute.utils.secrets import load_secrets

ErrorCode = Union[int, str, None]
# Characters that YAML reads as line breaks, or refuses as unprintable, inside a quoted string.
_YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufffe\uffff]")
# How the research-cluster fork refuses a resource pool the user may not use; the launch
# routes prefix it with their own context.
_POOL_DENIED = re.compile(r'may not use resource pool "([^"\\]{1,256})"')


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


def _master_from(values: Mapping[str, str]) -> Optional[str]:
    names = ("DET_MASTER", "DET_MASTER_ADDR", "DET_MASTER_HOST")
    return next((values[name] for name in names if values.get(name)), None)


def _config_text(config: Mapping[str, Any]) -> str:
    """An experiment or generic task config as the YAML text the master reads: JSON, with every
    string quoted.

    The master's YAML 1.1 parser reads a plain ``y``, ``n`` or ``1e-3`` as a bool or a float,
    and PyYAML writes them plain. It refuses the surrogate pairs that JSON escapes characters
    outside the BMP with, so only the characters YAML itself would misread are escaped.
    """

    text = json.dumps(dict(config), ensure_ascii=False, allow_nan=False)
    return _YAML_UNSAFE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() in {"1", "true", "yes", "y", "on"}


# RFC 9209 error types with which a proxy reports that it never connected to the next hop,
# so the request cannot have reached Determined.
_PROXY_CONNECT_ERRORS = frozenset({
    "dns_error",
    "dns_timeout",
    "destination_not_found",
    "destination_unavailable",
    "connection_refused",
    "connection_timeout",
    "destination_ip_prohibited",
    "destination_ip_unroutable",
})
_SF_KEY = re.compile(r"[a-z*][a-z0-9_\-.*]*")
_SF_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")
_SF_NUMBER = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,3})?")
_SF_BYTES = re.compile(r":[A-Za-z0-9+/=]*:")


def _sf_bare_item(text: str, index: int) -> tuple[Any, int]:
    """Parse one RFC 8941 bare item at ``index``; return its value and the next index."""
    if index >= len(text):
        raise ValueError("missing item")
    head = text[index]
    if head == '"':
        value, index = [], index + 1
        while index < len(text):
            character = text[index]
            if character == "\\":
                if index + 1 >= len(text) or text[index + 1] not in '"\\':
                    raise ValueError("bad escape")
                value.append(text[index + 1])
                index += 2
            elif character == '"':
                return "".join(value), index + 1
            elif not " " <= character <= "~":
                raise ValueError("bad string character")
            else:
                value.append(character)
                index += 1
        raise ValueError("unterminated string")
    if head == "?":
        if text[index + 1:index + 2] not in {"0", "1"}:
            raise ValueError("bad boolean")
        return text[index + 1] == "1", index + 2
    for pattern in (_SF_BYTES, _SF_NUMBER, _SF_TOKEN):
        match = pattern.match(text, index)
        if match:
            return match.group(), match.end()
    raise ValueError("bad item")


def _sf_parameters(text: str, index: int) -> tuple[Dict[str, Any], int]:
    parameters: Dict[str, Any] = {}
    while index < len(text) and text[index] == ";":
        index += 1
        while index < len(text) and text[index] == " ":
            index += 1
        match = _SF_KEY.match(text, index)
        if not match:
            raise ValueError("bad parameter key")
        key, index = match.group(), match.end()
        value: Any = True
        if index < len(text) and text[index] == "=":
            value, index = _sf_bare_item(text, index + 1)
        parameters[key] = value
    return parameters, index


def _proxy_status_errors(value: str) -> List[str]:
    """Return the ``error`` parameter of each member of an RFC 9209 Proxy-Status field.

    The field is an RFC 8941 structured-field list; a field that does not parse is ignored
    as a whole, as RFC 8941 requires, and yields no errors.
    """
    errors: List[str] = []
    text, index = value.strip(" \t"), 0
    try:
        while index < len(text):
            if text[index] == "(":
                index += 1
                while True:
                    while index < len(text) and text[index] == " ":
                        index += 1
                    if index < len(text) and text[index] == ")":
                        index += 1
                        break
                    _item, index = _sf_bare_item(text, index)
                    _parameters, index = _sf_parameters(text, index)
                    if index >= len(text) or text[index] not in " )":
                        raise ValueError("bad inner list")
            else:
                _item, index = _sf_bare_item(text, index)
            parameters, index = _sf_parameters(text, index)
            error = parameters.get("error")
            if isinstance(error, str):
                errors.append(error)
            while index < len(text) and text[index] in " \t":
                index += 1
            if index < len(text):
                if text[index] != ",":
                    raise ValueError("expected a comma")
                index += 1
                while index < len(text) and text[index] in " \t":
                    index += 1
                if index >= len(text):
                    raise ValueError("trailing comma")
    except ValueError:
        return []
    return errors


def _header(response: requests.Response, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None) or {}
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _proxy_answer(response: requests.Response, payload: Any) -> Optional[Dict[str, Any]]:
    """Describe an error response that an HTTP proxy produced instead of Determined.

    A Proxy-Status error that means the proxy never connected upstream always counts. Any
    other response counts only when it is a 5xx whose body is empty or not JSON, so it is no
    Determined error, and it carries a proxy header: Proxy-Connection, Via or Proxy-Status.
    """
    status = response.status_code
    proxy_status = _header(response, "Proxy-Status")
    errors = _proxy_status_errors(proxy_status) if proxy_status else []
    connect_error = next((error for error in errors if error in _PROXY_CONNECT_ERRORS), None)
    if connect_error is not None:
        return {"source": "proxy", "status_code": status, "proxy_error": connect_error}
    indicated = any(
        _header(response, name) is not None for name in ("Proxy-Connection", "Via", "Proxy-Status")
    )
    if status >= 500 and payload is None and indicated:
        answer: Dict[str, Any] = {"source": "proxy", "status_code": status}
        if errors:
            answer["proxy_error"] = errors[-1]
        return answer
    return None


def _error_from_response(response: requests.Response) -> APIError:
    try:
        payload = response.json() if (getattr(response, "content", None) or response.text) else None
    except (ValueError, requests.exceptions.JSONDecodeError):
        payload = None
    status = response.status_code
    proxy = _proxy_answer(response, payload)
    if proxy is not None and "proxy_error" in proxy and proxy["proxy_error"] in _PROXY_CONNECT_ERRORS:
        return APIError(
            f"{status} An HTTP proxy could not connect to Determined "
            f"({proxy['proxy_error']}); the request did not reach the master",
            code="transport_error", details=proxy, retryable=True,
        )
    if proxy is not None:
        return APIError(
            f"{status} An HTTP proxy, not Determined, answered; the master was probably unreachable",
            code=status, details=proxy, retryable=status != 501,
        )
    if not isinstance(payload, dict):
        payload = {}
    error = payload.get("error")
    gateway = isinstance(error, dict)
    if gateway:
        # The gRPC gateway nests its message: {"error": {"code", "reason", "error"}}, or
        # {"error": {"grpcCode", "httpCode", "message"}} on a stream.
        error = error.get("error") or error.get("message") or error.get("reason")
    message = payload.get("message") or error or response.text or getattr(response, "reason", "API request failed")
    # Only Determined's own error body is its permission refusal; another 403 keeps its status.
    if status == 403 and (gateway or payload.get("message")):
        denied = _POOL_DENIED.search(str(message))
        if denied is not None:
            pool = denied.group(1)
            return APIError(
                f"resource pool {pool!r} is not available to you",
                code="permission_denied",
                details={"resource_pool": pool},
            )
        return APIError(
            f"{status} {message}", code="permission_denied", details=payload.get("details")
        )
    return APIError(
        f"{status} {message}", code=payload.get("code", status), details=payload.get("details"),
        # 501 means the master lacks the route; repeating the request cannot help.
        retryable=status == 429 or (status >= 500 and status != 501),
    )


def _connection_never_opened(exc: requests.RequestException) -> bool:
    """Return whether a request failed before a connection to the server (or proxy) was open.

    Refused connections, failed name resolution and connect timeouts happen before any byte of
    the request is sent. Everything later (read timeouts, dropped connections, TLS errors,
    responses from a proxy) may follow a request the server received.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError) or isinstance(
        exc, requests.exceptions.SSLError
    ):
        return False
    reason: Any = exc.args[0] if exc.args else None
    # requests wraps urllib3's MaxRetryError, whose reason may be a ProxyError that wraps the
    # failure to reach the proxy.
    for _ in range(3):
        reason = getattr(reason, "reason", None) or getattr(reason, "original_error", None) or reason
        if isinstance(reason, (NewConnectionError, ConnectTimeoutError)):
            return True
    return False


def _transport_message(message: str, exc: requests.RequestException) -> str:
    """Add what failed when TLS or an HTTP proxy stopped a request to ``message``.

    Only a fixed phrase and the short reason of the ``ssl`` exception are added: the text of
    the wrapping exceptions carries the URL, with its query string, and the proxy's address.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        failure = (
            "the proxy could not be reached" if _connection_never_opened(exc)
            else "the proxy refused or could not reach the master"
        )
        return (
            f"{message}: {failure}; "
            "see docs/troubleshooting.md#the-master-is-unreachable-through-a-proxy"
        )
    if not isinstance(exc, requests.exceptions.SSLError):
        return message
    # requests wraps urllib3's MaxRetryError, whose reason is urllib3's SSLError around ssl's.
    cause: Any = exc.args[0] if exc.args else None
    for _ in range(3):
        if isinstance(cause, ssl.SSLError):
            break
        cause = getattr(cause, "reason", None) or next(iter(getattr(cause, "args", ())), None)
    if isinstance(cause, ssl.SSLCertVerificationError):
        failure, reason = "TLS verification of the master failed", getattr(cause, "verify_message", None)
    else:
        failure = "the TLS connection to the master failed"
        reason = getattr(cause, "reason", None) if isinstance(cause, ssl.SSLError) else None
    if isinstance(reason, str) and reason:
        failure = f"{failure} ({reason})"
    return f"{message}: {failure}; see docs/troubleshooting.md#tls-certificate-verification-fails"


def _login_for_token(api_url: str, username: str, password: str, verify_ssl: bool) -> str:
    endpoint = urljoin(api_url.rstrip("/") + "/", "api/v1/auth/login")
    try:
        response = requests.post(endpoint, headers={"Content-Type": "application/json"}, json={"username": username, "password": password}, timeout=15, verify=verify_ssl)
    except requests.RequestException as exc:
        raise APIError(
            _transport_message("Could not authenticate with Determined", exc),
            code="transport_error", details={"endpoint": "api/v1/auth/login"}, retryable=True,
        ) from exc
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


# Display fields of a generic task config; every master with the generic task list
# (WU-CVGL/determined#27) accepts them.
_GENERIC_METADATA_FIELDS = ("name", "description")
_GENERIC_STATE_PREFIX = "GENERIC_TASK_STATE_"


class DeterminedAPIClient:
    _TASK_KINDS = {"command", "shell", "experiment", "generic"}
    # Kinds whose remote listing can be filtered by the owning account. Generic tasks need a
    # master with the generic task list (WU-CVGL/determined#27).
    _LISTABLE_KINDS = {"command", "shell", "generic", "experiment"}
    _REMOTE_TASK_FIELDS = (
        "id",
        "userId",
        "username",
        "name",
        "description",
        "state",
        "resourcePool",
        "startTime",
        "endTime",
    )

    def __init__(self, api_url: Optional[str] = None, api_token: Optional[str] = None, secrets_path: Optional[Path] = None, verify_ssl: Optional[bool] = None) -> None:
        secrets = load_secrets(secrets_path)
        file_master, ambient_master = _master_from(secrets), _master_from(os.environ)
        if file_master:
            # A secrets file that names its master supplies the URL and the credentials
            # together, so its credentials reach only that master and no ambient credential
            # does. An explicit api_url, or else the environment's master, that names another
            # master is refused here, before a login or any other request.
            override, source = (api_url, "--api-url") if api_url else (ambient_master, "DET_MASTER")
            if override and _normalize_api_url(override) != _normalize_api_url(file_master):
                raise ValueError(
                    f"{source} names a different master than the secrets file, whose credentials "
                    "belong to its own master; unset the override or use a secrets file for that master"
                )
            ambient: Mapping[str, str] = {}
        else:
            ambient = os.environ
        self.api_url = _normalize_api_url(api_url or file_master or ambient_master)
        self.verify_ssl = _bool_env("DET_VERIFY_SSL", False) if verify_ssl is None else verify_ssl
        self.api_token = self._resolve_token(api_token, secrets, ambient)
        self.headers: Dict[str, str] = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    def _url(self, endpoint: str) -> str:
        return urljoin(self.api_url.rstrip("/") + "/", endpoint)

    @staticmethod
    def _json_response(response: requests.Response, *, mutation: bool = False) -> Dict[str, Any]:
        if response.status_code >= 400:
            error = _error_from_response(response)
            if mutation and response.status_code >= 500 and error.code != "transport_error":
                details = error.details if isinstance(error.details, dict) else {}
                if details.get("source") == "proxy":
                    raise SubmissionUncertainError(
                        f"an HTTP proxy, not Determined, answered with HTTP {response.status_code}; "
                        "the master was probably unreachable, but the outcome is unknown",
                        details=details,
                    ) from error
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
            raise APIError(
                _transport_message("Determined request failed", exc),
                code="transport_error", details={"endpoint": endpoint}, retryable=True,
            ) from exc
        return self._json_response(response)

    def _post(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            # A redirect is never followed: a failure on the way to its target would look like
            # a request that was never sent, although this one was.
            response = requests.post(
                self._url(endpoint), headers={**self.headers, "Content-Type": "application/json"},
                json=data, timeout=60, verify=self.verify_ssl, allow_redirects=False,
            )
        except requests.RequestException as exc:
            if _connection_never_opened(exc):
                # Nothing reached the master, so the mutation certainly did not happen.
                raise APIError(
                    _transport_message("Could not connect to Determined; the request was not sent", exc),
                    code="transport_error", details={"endpoint": endpoint}, retryable=True,
                ) from exc
            raise SubmissionUncertainError(
                _transport_message("Determined mutation outcome is unknown", exc), details={"endpoint": endpoint}
            ) from exc
        except ValueError as exc:
            # requests parses a 3xx's Location even when it does not follow it, after this
            # request was sent; an unparseable one fails there.
            raise SubmissionUncertainError(
                "Determined mutation outcome is unknown", details={"endpoint": endpoint}
            ) from exc
        if 300 <= response.status_code < 400:
            # The target is left out of the error: its path or query can carry a session.
            raise SubmissionUncertainError(
                f"Determined mutation outcome is unknown after HTTP {response.status_code}, "
                "a redirect that was not followed",
                details={"endpoint": endpoint, "status_code": response.status_code},
            )
        return self._json_response(response, mutation=True)


    def _stream_logs(self, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            response = requests.get(self._url(endpoint), headers=self.headers, params=params, timeout=30, verify=self.verify_ssl, stream=True)
        except requests.RequestException as exc:
            raise APIError(
                _transport_message("Determined log request failed", exc),
                code="transport_error", details={"endpoint": endpoint}, retryable=True,
            ) from exc
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

    def _resolve_token(
        self, api_token: Optional[str], secrets: Dict[str, str], ambient: Mapping[str, str]
    ) -> Optional[str]:
        if api_token:
            return api_token
        if ambient.get("DET_API_TOKEN"):
            return ambient["DET_API_TOKEN"]
        if secrets.get("DET_API_TOKEN"):
            return secrets["DET_API_TOKEN"]
        username = secrets.get("DET_USERNAME") or ambient.get("DET_USERNAME")
        password = secrets.get("DET_PASSWORD") or ambient.get("DET_PASSWORD")
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

    def list_remote_tasks(
        self,
        kind: str,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if kind not in self._LISTABLE_KINDS:
            raise ValueError("kind must be one of: command, shell, generic, experiment")
        normalized_user_id = self.normalize_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if kind == "generic":
            # The generic task list is always newest first.
            response = self._generic_task_list(
                {"userIds": [int(normalized_user_id)], "limit": limit, "offset": offset}
            )
            collection = response.get("tasks")
            if isinstance(collection, list):
                collection = [
                    self._generic_summary(item) if isinstance(item, Mapping) else item
                    for item in collection
                ]
        else:
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
        body = {"config": _config_text(config), "activate": True} if kind == "experiment" else {"config": config}
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
        body: Dict[str, Any] = {"contextDirectory": [], "config": _config_text(config), **options}
        response = self._post("api/v1/generic-tasks", data=body)
        task_id = response.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise SubmissionUncertainError(
                "Determined response did not contain a generic task id", details=response
            )
        launch_warnings = response.get("warnings") or []
        warnings = [
            {"code": "launch_warning", "message": item}
            for item in (launch_warnings if isinstance(launch_warnings, list) else [])
            if isinstance(item, str) and item
        ]
        return {"id": task_id, "warnings": warnings}

    def require_generic_task_list(self) -> None:
        """Fail with ``unsupported`` unless the master lists generic tasks with their owners.

        Without that list (WU-CVGL/determined#27) a generic task could be created but its
        owner never verified, so it could not be managed afterwards.
        """
        self._generic_task_list({"limit": 1})

    def _generic_task_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._get("api/v1/generic-tasks", params=params)
        except APIError as exc:
            # A master without the list answers the GET of the create route with 404/405/501.
            if exc.code in {404, 405, 501}:
                raise APIError(
                    "This Determined master cannot list generic tasks with their owners; "
                    "it needs the research-cluster fork with WU-CVGL/determined#27",
                    code="unsupported",
                ) from exc
            raise

    @staticmethod
    def _generic_summary(item: Mapping[str, Any]) -> Dict[str, Any]:
        """Map a listed generic task to the fields of other kinds' remote entities."""
        summary: Dict[str, Any] = {
            "id": item.get("taskId"),
            "jobId": item.get("jobId") or None,
            "userId": item.get("userId"),
            "username": item.get("username"),
            "name": item.get("name"),
            "description": item.get("description") or None,
            "resourcePool": item.get("resourcePool"),
            "startTime": item.get("startTime"),
            "endTime": item.get("endTime"),
        }
        raw_state = item.get("state")
        if isinstance(raw_state, str) and raw_state.startswith(_GENERIC_STATE_PREFIX):
            summary["state"] = "STATE_" + raw_state[len(_GENERIC_STATE_PREFIX):]
        return {key: value for key, value in summary.items() if value is not None}

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
        # The owner comes from the generic task list; a master without it leaves the entity
        # ownerless, so ownership checks fail instead of guessing.
        try:
            listed = self._generic_task_list({"taskIds": [task_id]}).get("tasks")
        except APIError as exc:
            if exc.code != "unsupported":
                raise
            listed = None
        if isinstance(listed, list):
            for item in listed:
                if isinstance(item, Mapping) and item.get("taskId") == task_id:
                    summary = self._generic_summary(item)
                    for field in ("userId", "username", "name", "jobId"):
                        if field in summary:
                            entity[field] = summary[field]
                    # The task record has no pool; the config's pool, when set, wins.
                    if "resourcePool" not in entity and "resourcePool" in summary:
                        entity["resourcePool"] = summary["resourcePool"]
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
            # A proxy's answer is already labelled as such.
            proxy = isinstance(exc.details, dict) and exc.details.get("source") == "proxy"
            if isinstance(exc.__cause__, APIError) and not proxy:
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

    def job_queue(self, pool: str, limit: int = 1000) -> Dict[str, Any]:
        """Return the jobs among the first ``limit`` of one pool's queue shown in full.

        Entries the account may see only in limited form are skipped. ``placement`` is
        ``None`` when the master does not report it (before the fork 0.42.0).
        """
        if not isinstance(pool, str) or not pool:
            # An empty pool would quietly query the default pool.
            raise ValueError("pool must be a non-empty string")
        response = self._get("api/v1/job-queues-v2", params={"resourcePool": pool, "limit": limit})
        malformed = APIError("Job-queue response is malformed", code="invalid_response")

        def count(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        entries, pagination = response.get("jobs"), response.get("pagination")
        if (
            not isinstance(entries, list)
            or not isinstance(pagination, Mapping)
            or not count(pagination.get("total"))
        ):
            raise malformed
        jobs: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or len(entry) != 1:
                raise malformed
            if "limited" in entry:
                continue
            job = entry.get("full")
            summary = job.get("summary") if isinstance(job, Mapping) else None
            jobs_ahead = summary.get("jobsAhead") if isinstance(summary, Mapping) else None
            if (
                not isinstance(summary, Mapping)
                or not isinstance(job.get("jobId"), str)
                or not job["jobId"]
                or not isinstance(job.get("resourcePool"), str)
                or not isinstance(summary.get("state"), str)
                # A scheduler that does not rank jobs (fair share) reports -1.
                or not (count(jobs_ahead) or (jobs_ahead == -1 and type(jobs_ahead) is int))
                or not count(job.get("requestedSlots"))
                or not count(job.get("allocatedSlots"))
            ):
                raise malformed
            placement: Optional[List[Dict[str, Any]]] = None
            if "placement" in job:
                if not isinstance(job["placement"], list):
                    raise malformed
                placement = []
                for item in job["placement"]:
                    if (
                        not isinstance(item, Mapping)
                        or not isinstance(item.get("agentId"), str)
                        or not isinstance(item.get("deviceIds"), list)
                        or not all(count(device) for device in item["deviceIds"])
                    ):
                        raise malformed
                    placement.append(
                        {"agent_id": item["agentId"], "device_ids": list(item["deviceIds"])}
                    )
            jobs.append({
                "job_id": job["jobId"],
                "resource_pool": job["resourcePool"],
                "state": summary["state"],
                "jobs_ahead": None if jobs_ahead == -1 else jobs_ahead,
                "requested_slots": job["requestedSlots"],
                "allocated_slots": job["allocatedSlots"],
                "placement": placement,
            })
        return {"jobs": jobs, "total": pagination["total"]}

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
