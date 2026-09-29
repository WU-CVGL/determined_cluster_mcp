"""Bounded checks and explicit creation of launch paths through a trusted local view."""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import StorageError
from .service import StorageService

Observation = Tuple[str, Optional[str]]


def _stat_directory(path: Path) -> Observation:
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return "missing", None
    except NotADirectoryError:
        return "missing", "parent_not_directory"
    except PermissionError:
        return "unverified", "permission_denied"
    except OSError as exc:
        return "unverified", f"os_error:{errno.errorcode.get(exc.errno, exc.errno)}"
    return ("present", None) if stat.S_ISDIR(info.st_mode) else ("not_directory", None)


class PathInspector:
    """Classify host paths as present, missing, not_directory, or unverified.

    A path is decided only through a local view that the storage configuration trusts:
    an explicit ``local_mounts`` entry, or, in ``auto`` or ``local`` mode, a compute-profile
    host root that exists locally. Anything else, including SSH-only access, a
    permission error, or a filesystem that does not answer in time, is ``unverified``.
    """

    def __init__(
        self,
        storage: Optional[StorageService],
        *,
        unavailable_reason: str = "storage_config_unavailable",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.storage = storage
        self.unavailable_reason = unavailable_reason
        self.timeout_seconds = timeout_seconds

    def inspect(self, host_paths: Sequence[str]) -> List[Observation]:
        if self.storage is None:
            return [("unverified", self.unavailable_reason) for _ in host_paths]
        if self.storage.config.mode == "ssh":
            return [("unverified", "ssh_only_access") for _ in host_paths]
        results: List[Optional[Observation]] = [None] * len(host_paths)

        def probe(index: int, host_path: str) -> None:
            try:
                results[index] = self._probe(host_path)
            except Exception as exc:  # classify, never fail the plan on an observation
                results[index] = ("unverified", getattr(exc, "code", type(exc).__name__))

        # Daemon threads: a network filesystem can block a stat for minutes, and a
        # stuck probe must neither delay the answer beyond the deadline nor process exit.
        threads = [
            threading.Thread(target=probe, args=(index, path), daemon=True)
            for index, path in enumerate(host_paths)
        ]
        deadline = time.monotonic() + self.timeout_seconds
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return [
            result if result is not None and not thread.is_alive() else ("unverified", "timeout")
            for result, thread in zip(results, threads)
        ]

    def _probe(self, host_path: str) -> Observation:
        storage = self.storage
        assert storage is not None
        mount = storage._host_mount(host_path)
        if mount is None:
            return "unverified", "outside_profile_roots"
        mapping = storage._local_mapping(host_path, mount, write=False)
        if mapping is None:
            return "unverified", "not_locally_visible"
        try:
            target, _root = storage._safe_mapped_path(mapping, host_path)
        except StorageError as exc:
            return "unverified", exc.code
        observation = _stat_directory(target)
        if observation[0] == "missing":
            # Listing the parent refreshes cached negative lookups on network filesystems.
            try:
                os.listdir(target.parent)
            except OSError:
                pass
            observation = _stat_directory(target)
        return observation

    def ensure_directories(self, entries: Sequence[Mapping[str, str]]) -> List[Dict[str, Any]]:
        if self.storage is None:
            raise StorageError(
                "creating directories requires a readable storage access configuration",
                code="configuration_required",
            )
        return self.storage.ensure_directories(entries)


__all__ = ["PathInspector"]
