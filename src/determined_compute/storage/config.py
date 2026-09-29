"""Client-side shared-storage access configuration."""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

import yaml


class StorageError(ValueError):
    """Safe, structured storage configuration or operation failure."""

    def __init__(self, message: str, *, code: str = "storage_error") -> None:
        super().__init__(message)
        self.code = code
        self.retryable = False


def _host_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise StorageError(f"{field} must be a non-empty absolute path", code="invalid_storage_config")
    if ".." in PurePosixPath(value).parts:
        raise StorageError(f"{field} must not contain '..' traversal", code="invalid_storage_config")
    return posixpath.normpath(value)


def _local_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise StorageError(f"{field} must be a non-empty absolute path", code="invalid_storage_config")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise StorageError(f"{field} must be absolute and contain no '..' traversal", code="invalid_storage_config")
    return path


@dataclass(frozen=True)
class LocalMount:
    host_path: str
    local_path: Path

    @classmethod
    def from_mapping(cls, value: Any, index: int) -> "LocalMount":
        if not isinstance(value, Mapping):
            raise StorageError(f"local_mounts[{index}] must be an object", code="invalid_storage_config")
        unknown = set(value) - {"host_path", "local_path"}
        if unknown:
            raise StorageError(
                f"local_mounts[{index}] has unknown fields: {sorted(unknown)}",
                code="invalid_storage_config",
            )
        local_path = _local_path(value.get("local_path"), f"local_mounts[{index}].local_path")
        if local_path == Path(local_path.anchor):
            raise StorageError(
                f"local_mounts[{index}].local_path must not be a filesystem root",
                code="invalid_storage_config",
            )
        return cls(
            host_path=_host_path(value.get("host_path"), f"local_mounts[{index}].host_path"),
            local_path=local_path,
        )


_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    identity_file: Optional[Path] = None
    config_file: Optional[Path] = None
    auth: str = "openssh"
    keyring_service: Optional[str] = None

    @property
    def username(self) -> Optional[str]:
        """Compatibility alias used by authentication helpers."""
        return self.user

    @classmethod
    def from_mapping(cls, value: Any) -> "SSHConfig":
        if not isinstance(value, Mapping):
            raise StorageError("ssh must be an object", code="invalid_storage_config")
        allowed = {
            "host",
            "user",
            "username",
            "port",
            "identity_file",
            "config_file",
            "auth",
            "keyring_service",
        }
        unknown = set(value) - allowed
        if unknown:
            raise StorageError(f"ssh has unknown fields: {sorted(unknown)}", code="invalid_storage_config")
        host = value.get("host")
        if not isinstance(host, str) or not host or host.startswith("-") or not _HOST_RE.fullmatch(host):
            raise StorageError("ssh.host is invalid", code="invalid_storage_config")
        if ":" in host:
            candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
            try:
                host = str(ipaddress.IPv6Address(candidate))
            except ValueError as exc:
                raise StorageError("ssh.host is invalid", code="invalid_storage_config") from exc
        user = value.get("user", value.get("username"))
        if "user" in value and "username" in value and value["user"] != value["username"]:
            raise StorageError("ssh.user and ssh.username disagree", code="invalid_storage_config")
        if user is not None and (
            not isinstance(user, str) or not user or user.startswith("-") or not _USER_RE.fullmatch(user)
        ):
            raise StorageError("ssh.user is invalid", code="invalid_storage_config")
        port = value.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
            raise StorageError("ssh.port must be between 1 and 65535", code="invalid_storage_config")
        auth = value.get("auth", "openssh")
        if auth not in {"openssh", "password", "keyring"}:
            raise StorageError("ssh.auth must be openssh, password, or keyring", code="invalid_storage_config")
        service = value.get("keyring_service")
        if service is not None and (not isinstance(service, str) or not service):
            raise StorageError("ssh.keyring_service must be a non-empty string", code="invalid_storage_config")
        return cls(
            host=host,
            user=user,
            port=port,
            identity_file=_local_path(value["identity_file"], "ssh.identity_file") if value.get("identity_file") else None,
            config_file=_local_path(value["config_file"], "ssh.config_file") if value.get("config_file") else None,
            auth=auth,
            keyring_service=service,
        )


SNAPSHOT_LINK_MODES = ("auto", "reflink", "hardlink", "copy")


@dataclass(frozen=True)
class SnapshotConfig:
    """Where content-addressed code snapshots live; root is a container path."""

    root: str
    link_mode: str = "auto"

    @classmethod
    def from_mapping(cls, value: Any) -> "SnapshotConfig":
        if not isinstance(value, Mapping):
            raise StorageError("snapshots must be an object", code="invalid_storage_config")
        unknown = set(value) - {"root", "link_mode"}
        if unknown:
            raise StorageError(
                f"snapshots has unknown fields: {sorted(unknown)}", code="invalid_storage_config"
            )
        link_mode = value.get("link_mode", "auto")
        if link_mode not in SNAPSHOT_LINK_MODES:
            raise StorageError(
                f"snapshots.link_mode must be one of: {', '.join(SNAPSHOT_LINK_MODES)}",
                code="invalid_storage_config",
            )
        return cls(root=_host_path(value.get("root"), "snapshots.root"), link_mode=link_mode)


@dataclass(frozen=True)
class StorageAccessConfig:
    mode: str = "auto"
    local_mounts: Tuple[LocalMount, ...] = ()
    ssh: Optional[SSHConfig] = None
    connect_timeout_seconds: int = 10
    timeout_seconds: int = 120
    preserve_permissions: bool = True
    snapshots: Optional[SnapshotConfig] = None

    @classmethod
    def from_file(cls, path: Any) -> "StorageAccessConfig":
        config_path = Path(path).expanduser()
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError("cannot read storage config", code="invalid_storage_config") from exc
        try:
            value = json.loads(text) if config_path.suffix.lower() == ".json" else yaml.safe_load(text)
        except (ValueError, yaml.YAMLError) as exc:
            raise StorageError("invalid storage config", code="invalid_storage_config") from exc
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: Any) -> "StorageAccessConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise StorageError("storage config must be an object", code="invalid_storage_config")
        allowed = {
            "mode",
            "local_mounts",
            "ssh",
            "connect_timeout_seconds",
            "timeout_seconds",
            "preserve_permissions",
            "snapshots",
        }
        unknown = set(value) - allowed
        if unknown:
            raise StorageError(f"storage config has unknown fields: {sorted(unknown)}", code="invalid_storage_config")
        mode = value.get("mode", "auto")
        if mode not in {"auto", "local", "ssh"}:
            raise StorageError("mode must be auto, local, or ssh", code="invalid_storage_config")
        local_values = value.get("local_mounts", [])
        if not isinstance(local_values, list):
            raise StorageError("local_mounts must be a list", code="invalid_storage_config")
        mounts = tuple(LocalMount.from_mapping(item, index) for index, item in enumerate(local_values))
        for index, first in enumerate(mounts):
            for second in mounts[index + 1 :]:
                if _within(first.host_path, second.host_path) or _within(second.host_path, first.host_path):
                    raise StorageError("local_mount host roots must not overlap", code="invalid_storage_config")
        ssh_value = value.get("ssh")
        ssh = SSHConfig.from_mapping(ssh_value) if ssh_value is not None else None
        connect = _bounded_int(value.get("connect_timeout_seconds", 10), "connect_timeout_seconds", 1, 120)
        timeout = _bounded_int(value.get("timeout_seconds", 120), "timeout_seconds", 1, 3600)
        preserve_permissions = value.get("preserve_permissions", True)
        if not isinstance(preserve_permissions, bool):
            raise StorageError(
                "preserve_permissions must be a boolean",
                code="invalid_storage_config",
            )
        snapshots_value = value.get("snapshots")
        snapshots = (
            SnapshotConfig.from_mapping(snapshots_value) if snapshots_value is not None else None
        )
        return cls(
            mode=mode,
            local_mounts=mounts,
            ssh=ssh,
            connect_timeout_seconds=connect,
            timeout_seconds=timeout,
            preserve_permissions=preserve_permissions,
            snapshots=snapshots,
        )


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError(f"{field} must be between {minimum} and {maximum}", code="invalid_storage_config")
    return value


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


__all__ = [
    "LocalMount",
    "SNAPSHOT_LINK_MODES",
    "SSHConfig",
    "SnapshotConfig",
    "StorageAccessConfig",
    "StorageError",
]
