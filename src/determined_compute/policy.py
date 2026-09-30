"""Per-request policy that is narrower than the master's RBAC.

The administrator's policy file sets the defaults a request falls back to, the pools a request
may name, the most slots one request may hold, whether storage transfers may overwrite files,
and the map from container paths to the host paths that the administrator mounts on every
agent. The master still validates everything it owns; this only narrows what the MCP sends.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

import yaml


class PolicyError(ValueError):
    """A request or policy file that the policy refuses. ``code`` is stable."""

    retryable = False

    def __init__(self, message: str, *, code: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details: Dict[str, Any] = details or {}


def _invalid(message: str) -> PolicyError:
    return PolicyError(message, code="invalid_policy")


def clean_path(value: Any, field: str) -> str:
    """Normalize an absolute POSIX path, rejecting relative paths and '..'."""

    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise PolicyError(f"{field} must be an absolute path", code="invalid_path")
    if ".." in PurePosixPath(value).parts:
        raise PolicyError(f"{field} must not contain '..'", code="invalid_path")
    # normpath keeps a leading '//', which names the same directory.
    return "/" + posixpath.normpath(value).lstrip("/")


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class Mount:
    """One administrator bind mount: ``host_path`` on every agent at ``container_path``."""

    host_path: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class MountMap:
    """Translate container paths to host paths through the administrator's mounts.

    Container roots never overlap, so a container path has at most one mount. Host roots may
    overlap as aliases of one another; for a host path the most specific root decides, and a
    read-only root wins a tie, so an alias can never write where another name is read-only.
    """

    mounts: Tuple[Mount, ...]

    def __post_init__(self) -> None:
        for index, first in enumerate(self.mounts):
            for second in self.mounts[index + 1 :]:
                if within(first.container_path, second.container_path) or within(
                    second.container_path, first.container_path
                ):
                    raise _invalid("mount container paths must not overlap")

    @property
    def container_roots(self) -> Tuple[str, ...]:
        return tuple(mount.container_path for mount in self.mounts)

    def to_host(self, container_path: Any, field: str = "path") -> Tuple[Mount, str]:
        """Return the mount holding ``container_path`` and the path on the host."""

        path = clean_path(container_path, field)
        for mount in self.mounts:
            if within(path, mount.container_path):
                relative = posixpath.relpath(path, mount.container_path)
                host = mount.host_path if relative == "." else posixpath.join(
                    mount.host_path, relative
                )
                return mount, host
        raise PolicyError(
            f"{field} {path} is not under a mounted root",
            code="path_not_mounted",
            details={"path": path, "roots": list(self.container_roots)},
        )

    def host_read_only(self, host_path: str) -> bool:
        """Whether ``host_path`` is read-only; a path under no host root counts as read-only."""

        matches = [mount for mount in self.mounts if within(host_path, mount.host_path)]
        if not matches:
            return True
        specific = max(len(mount.host_path) for mount in matches)
        return any(mount.read_only for mount in matches if len(mount.host_path) == specific)

    def read_only(self, container_path: Any, field: str = "path") -> bool:
        mount, host = self.to_host(container_path, field)
        return mount.read_only or self.host_read_only(host)


@dataclass(frozen=True)
class Resources:
    image: str
    pool: str
    slots: int


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string")
    return value


def _count(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(f"{field} must be an integer of at least {minimum}")
    return value


def _mount(value: Any, index: int) -> Mount:
    field = f"mounts[{index}]"
    if not isinstance(value, Mapping):
        raise _invalid(f"{field} must be an object")
    unknown = set(value) - {"host_path", "container_path", "read_only"}
    if unknown:
        raise _invalid(f"{field} has unknown fields: {sorted(unknown)}")
    read_only = value.get("read_only", False)
    if not isinstance(read_only, bool):
        raise _invalid(f"{field}.read_only must be a boolean")
    try:
        host = clean_path(value.get("host_path"), f"{field}.host_path")
        container = clean_path(value.get("container_path"), f"{field}.container_path")
    except PolicyError as exc:
        raise _invalid(str(exc)) from exc
    return Mount(host_path=host, container_path=container, read_only=read_only)


@dataclass(frozen=True)
class Policy:
    mounts: MountMap
    image: str
    pool: str
    slots: int = 1
    pools: FrozenSet[str] = frozenset()
    max_slots: Optional[int] = None
    allow_overwrite: bool = False

    def __post_init__(self) -> None:
        # An empty allow-list admits only the default pool.
        if not self.pools:
            object.__setattr__(self, "pools", frozenset({self.pool}))
        if self.pool not in self.pools:
            raise _invalid(f"defaults.pool {self.pool!r} is not in pools")
        if self.max_slots is not None and self.slots > self.max_slots:
            raise _invalid("defaults.slots exceeds max_slots")

    @classmethod
    def from_file(cls, path: Any) -> "Policy":
        policy_path = Path(path).expanduser()
        try:
            text = policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _invalid(f"cannot read the policy file: {exc.strerror}") from exc
        try:
            value = json.loads(text) if policy_path.suffix == ".json" else yaml.safe_load(text)
        except (ValueError, yaml.YAMLError) as exc:
            raise _invalid(f"the policy file is not valid YAML or JSON: {exc}") from exc
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: Any) -> "Policy":
        if not isinstance(value, Mapping):
            raise _invalid("the policy must be an object")
        unknown = set(value) - {"mounts", "defaults", "pools", "max_slots", "allow_overwrite"}
        if unknown:
            raise _invalid(f"the policy has unknown fields: {sorted(unknown)}")
        mounts = value.get("mounts")
        if not isinstance(mounts, list) or not mounts:
            raise _invalid("mounts must be a non-empty list")
        defaults = value.get("defaults")
        if not isinstance(defaults, Mapping):
            raise _invalid("defaults must be an object")
        unknown = set(defaults) - {"image", "pool", "slots"}
        if unknown:
            raise _invalid(f"defaults has unknown fields: {sorted(unknown)}")
        pools = value.get("pools", [])
        if not isinstance(pools, list):
            raise _invalid("pools must be a list of pool names")
        max_slots = value.get("max_slots")
        allow_overwrite = value.get("allow_overwrite", False)
        if not isinstance(allow_overwrite, bool):
            raise _invalid("allow_overwrite must be a boolean")
        return cls(
            mounts=MountMap(tuple(_mount(item, index) for index, item in enumerate(mounts))),
            image=_text(defaults.get("image"), "defaults.image"),
            pool=_text(defaults.get("pool"), "defaults.pool"),
            slots=_count(defaults.get("slots", 1), "defaults.slots", 0),
            pools=frozenset(_text(item, "pools[]") for item in pools),
            max_slots=None if max_slots is None else _count(max_slots, "max_slots", 0),
            allow_overwrite=allow_overwrite,
        )

    def resources(
        self,
        *,
        image: Optional[str] = None,
        pool: Optional[str] = None,
        slots: Optional[int] = None,
        trials: int = 1,
    ) -> Resources:
        """Fill in the defaults and check the pool and slot limits.

        ``trials`` is how many trials of an experiment may run at once: one for a command,
        shell or single-trial experiment, and ``searcher.max_concurrent_trials`` for a search.
        """

        resolved = Resources(
            image=image or self.image,
            pool=pool or self.pool,
            slots=self.slots if slots is None else slots,
        )
        self.check_pool(resolved.pool)
        self.check_slots(resolved.slots, trials)
        return resolved

    def check_pool(self, pool: str) -> None:
        if pool not in self.pools:
            raise PolicyError(
                f"pool {pool!r} is not allowed; allowed pools: {', '.join(sorted(self.pools))}",
                code="pool_not_allowed",
                details={"pool": pool, "allowed": sorted(self.pools)},
            )

    def check_slots(self, slots: int, trials: int = 1) -> None:
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise PolicyError("slots must be a non-negative integer", code="invalid_request")
        if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
            raise PolicyError("trials must be a positive integer", code="invalid_request")
        if self.max_slots is not None and slots * trials > self.max_slots:
            raise PolicyError(
                f"the request may hold {slots * trials} slots ({slots} per trial times "
                f"{trials} concurrent trials); the limit is {self.max_slots}",
                code="slots_exceed_limit",
                details={"slots": slots, "trials": trials, "max_slots": self.max_slots},
            )

    def check_overwrite(self, overwrite: bool) -> None:
        if overwrite and not self.allow_overwrite:
            raise PolicyError(
                "overwrite is not allowed by the policy; transfers keep existing files",
                code="overwrite_not_allowed",
            )


__all__ = ["Mount", "MountMap", "Policy", "PolicyError", "Resources", "clean_path", "within"]
