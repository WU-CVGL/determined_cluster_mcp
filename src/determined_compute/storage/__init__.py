"""Safe access to profile-authorized shared storage."""

from .config import LocalMount, SSHConfig, SnapshotConfig, StorageAccessConfig, StorageError
from .auth import SSHAuthError, ssh_auth
from .service import StorageService
from .paths import PathInspector

__all__ = [
    "LocalMount",
    "PathInspector",
    "SSHConfig",
    "SSHAuthError",
    "SnapshotConfig",
    "StorageAccessConfig",
    "StorageError",
    "StorageService",
    "ssh_auth",
]
