"""Deployment profile and shared-storage path policy."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import yaml

from .models import ValidationError


def _absolute_clean_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty absolute path")
    if not value.startswith("/"):
        raise ValidationError(f"{field} must be an absolute path")
    if ".." in PurePosixPath(value).parts:
        raise ValidationError(f"{field} must not contain '..' traversal")
    normalized = posixpath.normpath(value)
    if not normalized.startswith("/"):
        raise ValidationError(f"{field} must be an absolute path")
    return normalized


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class SharedMount:
    host_path: str
    container_path: str
    read_only: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "SharedMount":
        if not isinstance(value, Mapping):
            raise ValidationError(f"mounts[{index}] must be an object")
        unknown = set(value) - {"host_path", "container_path", "read_only"}
        if unknown:
            raise ValidationError(f"mounts[{index}] has unknown fields: {sorted(unknown)}")
        read_only = value.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ValidationError(f"mounts[{index}].read_only must be a boolean")
        return cls(
            host_path=_absolute_clean_path(value.get("host_path"), f"mounts[{index}].host_path"),
            container_path=_absolute_clean_path(
                value.get("container_path"), f"mounts[{index}].container_path"
            ),
            read_only=read_only,
        )

    def as_config(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "host_path": self.host_path,
            "container_path": self.container_path,
        }
        if self.read_only:
            result["read_only"] = True
        return result


@dataclass(frozen=True)
class ComputeProfile:
    mounts: Tuple[SharedMount, ...]
    default_image: str
    default_pool: str
    default_slots: int = 1
    shell_inactivity_seconds: Optional[int] = None
    cluster_identity: Optional[str] = None

    @classmethod
    def from_file(cls, path: Any) -> "ComputeProfile":
        profile_path = Path(path)
        try:
            text = profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"cannot read compute profile: {exc}") from exc
        try:
            if profile_path.suffix.lower() == ".json":
                value = json.loads(text)
            else:
                value = yaml.safe_load(text)
        except (ValueError, yaml.YAMLError) as exc:
            raise ValidationError(f"invalid compute profile: {exc}") from exc
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: Any) -> "ComputeProfile":
        if not isinstance(value, Mapping):
            raise ValidationError("compute profile must be an object")
        allowed = {
            "mounts",
            "shared_mounts",
            "defaults",
            "shell_inactivity_seconds",
            "cluster_identity",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValidationError(f"compute profile has unknown fields: {sorted(unknown)}")

        mounts_value = value.get("mounts", value.get("shared_mounts"))
        if not isinstance(mounts_value, list) or not mounts_value:
            raise ValidationError("compute profile mounts must be a non-empty list")
        mounts = tuple(SharedMount.from_mapping(item, i) for i, item in enumerate(mounts_value))
        cls._validate_non_overlapping_mounts(mounts)

        defaults = value.get("defaults")
        if not isinstance(defaults, Mapping):
            raise ValidationError("compute profile defaults must be an object")
        defaults_unknown = set(defaults) - {"image", "pool", "slots"}
        if defaults_unknown:
            raise ValidationError(
                f"profile defaults has unknown fields: {sorted(defaults_unknown)}"
            )
        image = defaults.get("image")
        pool = defaults.get("pool")
        slots = defaults.get("slots", 1)
        if not isinstance(image, str) or not image:
            raise ValidationError("profile defaults.image must be a non-empty string")
        if not isinstance(pool, str) or not pool:
            raise ValidationError("profile defaults.pool must be a non-empty string")
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValidationError("profile defaults.slots must be a non-negative integer")

        timeout = value.get("shell_inactivity_seconds")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise ValidationError("shell_inactivity_seconds must be a positive integer or null")
        cluster_identity = value.get("cluster_identity")
        if cluster_identity is not None and (
            not isinstance(cluster_identity, str) or not cluster_identity
        ):
            raise ValidationError("cluster_identity must be a non-empty string or null")
        return cls(
            mounts=mounts,
            default_image=image,
            default_pool=pool,
            default_slots=slots,
            shell_inactivity_seconds=timeout,
            cluster_identity=cluster_identity,
        )

    @staticmethod
    def _validate_non_overlapping_mounts(mounts: Iterable[SharedMount]) -> None:
        mounts = tuple(mounts)
        for index, first in enumerate(mounts):
            for second in mounts[index + 1 :]:
                if _is_within(first.container_path, second.container_path) or _is_within(
                    second.container_path, first.container_path
                ):
                    raise ValidationError("profile container mount roots must not overlap")

    def validate_container_path(self, value: Any, field: str) -> str:
        path = _absolute_clean_path(value, field)
        mount = next(
            (mount for mount in self.mounts if _is_within(path, mount.container_path)),
            None,
        )
        if mount is None:
            roots = ", ".join(mount.container_path for mount in self.mounts)
            raise ValidationError(f"{field} is outside configured shared container roots: {roots}")
        return path

    def validate_writable_container_path(self, value: Any, field: str) -> str:
        path = self.validate_container_path(value, field)
        mount = next(
            mount for mount in self.mounts if _is_within(path, mount.container_path)
        )
        if mount.read_only:
            raise ValidationError(f"{field} is under a read-only shared mount")
        relative = posixpath.relpath(path, mount.container_path)
        host_path = mount.host_path if relative == "." else posixpath.join(mount.host_path, relative)
        self.validate_writable_host_path(host_path, field)
        return path

    def host_path_for(self, value: Any, field: str = "path") -> str:
        """Translate a container path to the cluster-agent host path of its mount."""
        path = self.validate_container_path(value, field)
        mount = next(
            mount for mount in self.mounts if _is_within(path, mount.container_path)
        )
        relative = posixpath.relpath(path, mount.container_path)
        return mount.host_path if relative == "." else posixpath.join(mount.host_path, relative)

    def validate_host_path(self, value: Any, field: str) -> str:
        path = _absolute_clean_path(value, field)
        if not any(_is_within(path, mount.host_path) for mount in self.mounts):
            roots = ", ".join(mount.host_path for mount in self.mounts)
            raise ValidationError(f"{field} is outside configured shared host roots: {roots}")
        return path

    def validate_writable_host_path(self, value: Any, field: str) -> str:
        path = self.validate_host_path(value, field)
        matches = [mount for mount in self.mounts if _is_within(path, mount.host_path)]
        specificity = max(len(mount.host_path) for mount in matches)
        if any(mount.read_only for mount in matches if len(mount.host_path) == specificity):
            raise ValidationError(f"{field} is under a read-only shared mount")
        return path

    @property
    def fingerprint(self) -> str:
        value = {
            "mounts": [mount.as_config() for mount in self.mounts],
            "defaults": {
                "image": self.default_image,
                "pool": self.default_pool,
                "slots": self.default_slots,
            },
            "shell_inactivity_seconds": self.shell_inactivity_seconds,
            "cluster_identity": self.cluster_identity,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["ComputeProfile", "SharedMount"]
