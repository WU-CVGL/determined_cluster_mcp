"""Safe local and SSH-backed access to profile-authorized shared storage."""

from __future__ import annotations

import os
import posixpath
import shlex
import signal
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from determined_compute.compute.profile import ComputeProfile, SharedMount
from determined_compute.compute.models import ValidationError
from determined_compute.utils.secrets import default_secrets_path

from .config import LocalMount, SSHConfig, StorageAccessConfig, StorageError, _within

_OUTPUT_LIMIT = 16_384
_SYNC_EXCLUDES = (
    ".git/",
    ".local/",
    ".cache/",
    "cache/",
    ".ssh/",
    ".aws/",
    ".config/gcloud/",
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".env*",
    "*.env",
    "*.env.*",
    ".secrets*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.pyc",
    "*.pem",
    "*.key",
    "id_ed25519",
    "id_rsa",
    ".credentials/",
    "credentials/",
)


class StorageService:
    """Translate container paths through the compute profile, then access them safely."""

    def __init__(
        self,
        profile: ComputeProfile,
        config: StorageAccessConfig,
        secrets_path: Optional[Path] = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.secrets_path = Path(secrets_path).expanduser() if secrets_path else None
        for mapping in config.local_mounts:
            if not any(_within(mapping.host_path, mount.host_path) for mount in profile.mounts):
                raise StorageError(
                    "local_mounts host_path is outside compute profile host roots",
                    code="invalid_storage_config",
                )
        if config.mode == "ssh" and config.ssh is None:
            raise StorageError("ssh mode requires ssh configuration", code="invalid_storage_config")
        if config.snapshots is not None:
            self._snapshot_mount()

    def check(self, path: str) -> Dict[str, Any]:
        mount, host_path = self._translate(path, "path")
        read_only = self._read_only(path)
        backend, local = self._select_backend(host_path, mount)
        if backend == "local":
            assert local is not None
            target, _root = self._safe_mapped_path(local, host_path)
            exists = target.exists() or target.is_symlink()
            if target.is_symlink():
                kind = "symlink"
            elif target.is_dir():
                kind = "directory"
            elif target.is_file():
                kind = "file"
            elif exists:
                kind = "other"
            else:
                kind = "missing"
            return {
                "backend": "local",
                "path": path,
                "host_path": host_path,
                "local_path": str(target),
                "exists": exists,
                "type": kind,
                "readable": exists and os.access(target, os.R_OK),
                "writable": not read_only and exists and os.access(target, os.W_OK),
                "read_only": read_only,
            }
        output = self._ssh_check(host_path)
        return {
            "backend": "ssh",
            "path": path,
            "host_path": host_path,
            "ssh_host": self._require_ssh().host,
            **output,
            "writable": not read_only and output["writable"],
            "read_only": read_only,
        }

    def sync(self, local_dir: str, shared_dir: str, dry_run: bool = True) -> Dict[str, Any]:
        mount, host_path = self._translate(shared_dir, "shared_dir")
        if self._read_only(shared_dir):
            raise StorageError("shared_dir is under a read-only shared mount", code="read_only_storage")
        source = self._existing_local_directory(local_dir, "local_dir")
        if host_path == mount.host_path:
            raise StorageError(
                "shared_dir must be a task subdirectory, not a shared mount root",
                code="invalid_storage_path",
            )
        backend, local = self._select_backend(host_path, mount, write=True)
        excludes = list(_SYNC_EXCLUDES)
        secret_exclude = self._secret_exclude(source)
        if secret_exclude:
            excludes.append(secret_exclude)
        if backend == "local":
            assert local is not None
            destination, root = self._safe_mapped_path(local, host_path)
            if not dry_run:
                self._mkdir_beneath_root(destination, root)
            argv = self._rsync_args(
                source,
                destination,
                dry_run,
                excludes,
                preserve_permissions=self.config.preserve_permissions,
            )
            result = self._run(argv, timeout=self.config.timeout_seconds)
            return self._transfer_result(
                "sync",
                backend,
                source,
                shared_dir,
                dry_run,
                result,
                host_path=host_path,
                local_path=str(destination),
                excludes=excludes,
                preserve_permissions=self.config.preserve_permissions,
            )
        destination = self._remote_spec(host_path)
        result = self._run_remote_rsync(source, destination, dry_run, excludes)
        return self._transfer_result(
            "sync",
            backend,
            source,
            shared_dir,
            dry_run,
            result,
            host_path=host_path,
            ssh_host=self._require_ssh().host,
            excludes=excludes,
            preserve_permissions=self.config.preserve_permissions,
        )

    def fetch(self, shared_dir: str, local_dir: str, dry_run: bool = True) -> Dict[str, Any]:
        mount, host_path = self._translate(shared_dir, "shared_dir")
        output = self._local_output_directory(local_dir, dry_run)
        backend, local = self._select_backend(host_path, mount)
        if backend == "local":
            assert local is not None
            source, _root = self._safe_mapped_path(local, host_path)
            if not source.exists() or not source.is_dir():
                raise StorageError("shared_dir is not an accessible directory", code="storage_not_found")
            argv = self._rsync_args(
                source,
                output,
                dry_run,
                (),
                preserve_permissions=self.config.preserve_permissions,
            )
            result = self._run(argv, timeout=self.config.timeout_seconds)
            return self._transfer_result(
                "fetch",
                backend,
                shared_dir,
                output,
                dry_run,
                result,
                host_path=host_path,
                local_path=str(source),
                local_output_path=str(output),
                excludes=(),
                preserve_permissions=self.config.preserve_permissions,
            )
        source = self._remote_spec(host_path)
        result = self._run_remote_rsync(source, output, dry_run, ())
        return self._transfer_result(
            "fetch",
            backend,
            shared_dir,
            output,
            dry_run,
            result,
            host_path=host_path,
            ssh_host=self._require_ssh().host,
            local_output_path=str(output),
            excludes=(),
            preserve_permissions=self.config.preserve_permissions,
        )

    def snapshot(
        self,
        repo_dir: str,
        revision: str = "HEAD",
        include: Optional[Sequence[str]] = None,
        exclude: Optional[Sequence[str]] = None,
        dry_run: bool = True,
        verify: bool = False,
    ) -> Dict[str, Any]:
        """Publish an exact git revision as a read-only, content-addressed workdir."""
        from .snapshot import create_snapshot

        return create_snapshot(self, repo_dir, revision, include, exclude, dry_run, verify)

    def _snapshot_mount(self) -> Tuple[str, SharedMount, str]:
        """Validate the configured snapshot root against the compute profile."""
        snapshots = self.config.snapshots
        if snapshots is None:
            raise StorageError(
                "snapshots.root is not configured in the storage access configuration",
                code="configuration_required",
            )
        try:
            container_root = self.profile.validate_writable_container_path(
                snapshots.root, "snapshots.root"
            )
        except (ValidationError, ValueError) as exc:
            raise StorageError(str(exc), code="invalid_storage_config") from exc
        mount, host_path = self._translate(container_root, "snapshots.root")
        if host_path == mount.host_path:
            raise StorageError(
                "snapshots.root must be a subdirectory, not a shared mount root",
                code="invalid_storage_config",
            )
        return container_root, mount, host_path

    def _host_mount(self, host_path: str) -> Optional[SharedMount]:
        """Return the most specific profile mount containing a host path."""
        matches = [mount for mount in self.profile.mounts if _within(host_path, mount.host_path)]
        return max(matches, key=lambda mount: len(mount.host_path)) if matches else None

    def ensure_directories(self, entries: Sequence[Mapping[str, str]]) -> List[Dict[str, Any]]:
        """Create each host directory (with parents) through a local view or SSH; never delete."""
        results = []
        for entry in entries:
            field, host_path = entry["field"], entry["host_path"]
            try:
                host_path = self.profile.validate_writable_host_path(host_path, field)
            except (ValidationError, ValueError) as exc:
                raise StorageError(str(exc), code="read_only_storage") from exc
            mount = self._host_mount(host_path)
            assert mount is not None
            if host_path == mount.host_path:
                raise StorageError(
                    f"{field} is a shared mount root and cannot be created",
                    code="invalid_storage_path",
                )
            backend, local = self._select_backend(host_path, mount, write=True)
            if backend == "local":
                assert local is not None
                target, root = self._safe_mapped_path(local, host_path)
                existed = target.is_dir()
                try:
                    self._mkdir_beneath_root(target, root)
                except OSError as exc:
                    raise StorageError(
                        f"cannot create {field} directory: {exc.strerror or exc}",
                        code="storage_operation_failed",
                    ) from exc
                created = not existed
            else:
                created = self._ssh_mkdir(host_path)
            results.append({"field": field, "host_path": host_path, "created": created})
        return results

    def _ssh_mkdir(self, host_path: str) -> bool:
        ssh = self._require_ssh()
        from determined_compute.storage.auth import ssh_auth

        script = 'if [ -d "$1" ]; then echo existed; else mkdir -p -- "$1" && echo created; fi'
        remote_command = "sh -c " + shlex.quote(script) + " sh " + shlex.quote(host_path)
        with ssh_auth(ssh, self.secrets_path) as (env_overrides, auth_options):
            argv = [
                "ssh",
                *auth_options,
                "-o",
                f"ConnectTimeout={self.config.connect_timeout_seconds}",
                ssh.host,
                remote_command,
            ]
            result = self._run(
                argv, timeout=self.config.timeout_seconds, env_overrides=env_overrides
            )
        outcome = result["output"].strip().splitlines()[-1:] or [""]
        if outcome[0] not in {"created", "existed"}:
            raise StorageError(
                "SSH directory creation returned an invalid response",
                code="storage_operation_failed",
            )
        return outcome[0] == "created"

    def _translate(self, value: Any, field: str) -> Tuple[SharedMount, str]:
        if not isinstance(value, str) or ".." in PurePosixPath(value).parts:
            raise StorageError(f"{field} must be an absolute shared path without traversal", code="invalid_storage_path")
        try:
            container_path = self.profile.validate_container_path(value, field)
        except (ValidationError, ValueError) as exc:
            raise StorageError(str(exc), code="invalid_storage_path") from exc
        for mount in self.profile.mounts:
            if _within(container_path, mount.container_path):
                relative = posixpath.relpath(container_path, mount.container_path)
                host_path = mount.host_path if relative == "." else posixpath.join(mount.host_path, relative)
                return mount, host_path
        raise StorageError(f"{field} is outside configured shared roots", code="invalid_storage_path")

    def _read_only(self, container_path: str) -> bool:
        try:
            self.profile.validate_writable_container_path(container_path, "path")
        except (ValidationError, ValueError):
            return True
        return False

    def _validate_local_write(self, path: Path) -> None:
        """Apply shared-mount policy when a download destination is a local shared view."""
        mappings = list(self.config.local_mounts)
        for mount in self.profile.mounts:
            local_root = Path(mount.host_path)
            if local_root != Path(local_root.anchor) and local_root.is_dir():
                mappings.append(LocalMount(mount.host_path, local_root))
        matches = [
            (mapping, mapping.local_path.resolve(strict=False))
            for mapping in mappings
            if path.is_relative_to(mapping.local_path.resolve(strict=False))
        ]
        specificity = max((len(root.parts) for _, root in matches), default=0)
        for mapping, root in matches:
            if len(root.parts) != specificity:
                continue
            relative = path.relative_to(root).as_posix()
            host_path = posixpath.join(mapping.host_path, relative)
            try:
                self.profile.validate_writable_host_path(host_path, "local_dir")
            except (ValidationError, ValueError) as exc:
                raise StorageError("local_dir maps to read-only shared storage", code="read_only_storage") from exc

    def _select_backend(
        self, host_path: str, profile_mount: SharedMount, *, write: bool = False
    ) -> Tuple[str, Optional[LocalMount]]:
        local = self._local_mapping(host_path, profile_mount, write=write)
        if self.config.mode == "local":
            if local is None:
                raise StorageError(
                    "shared storage is not locally accessible; configure local_mounts",
                    code="configuration_required",
                )
            return "local", local
        if self.config.mode == "ssh":
            return "ssh", None
        if local is not None:
            return "local", local
        if self.config.ssh is not None:
            return "ssh", None
        raise StorageError(
            "shared storage is not locally accessible; configure local_mounts or ssh",
            code="configuration_required",
        )

    def _local_mapping(
        self, host_path: str, profile_mount: SharedMount, *, write: bool
    ) -> Optional[LocalMount]:
        access = os.R_OK | os.X_OK | (os.W_OK if write else 0)
        for mapping in self.config.local_mounts:
            if (
                _within(host_path, mapping.host_path)
                and mapping.local_path.is_dir()
                and os.access(mapping.local_path, access)
            ):
                return mapping
        host_root = Path(profile_mount.host_path)
        if host_root != Path(host_root.anchor) and host_root.is_dir() and os.access(host_root, access):
            return LocalMount(host_path=profile_mount.host_path, local_path=host_root)
        return None

    @staticmethod
    def _safe_mapped_path(mapping: LocalMount, host_path: str) -> Tuple[Path, Path]:
        try:
            root = mapping.local_path.resolve(strict=True)
        except OSError as exc:
            raise StorageError("configured local shared root is unavailable", code="configuration_required") from exc
        if not root.is_dir():
            raise StorageError("configured local shared root is not a directory", code="configuration_required")
        relative = posixpath.relpath(host_path, mapping.host_path)
        candidate = root if relative == "." else root.joinpath(*PurePosixPath(relative).parts)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise StorageError("local shared path escapes its configured root", code="invalid_storage_path")
        return resolved, root

    @staticmethod
    def _mkdir_beneath_root(path: Path, root: Path) -> None:
        if not root.exists() or not root.is_dir():
            raise StorageError("configured local shared root is unavailable", code="configuration_required")
        parent = path.parent.resolve(strict=False)
        if not parent.is_relative_to(root):
            raise StorageError("local destination parent escapes its shared root", code="invalid_storage_path")
        path.mkdir(parents=True, exist_ok=True)
        if not path.resolve(strict=True).is_relative_to(root):
            raise StorageError("local destination escapes its shared root", code="invalid_storage_path")

    @staticmethod
    def _clean_absolute_local(value: Any, field: str) -> Path:
        if not isinstance(value, str) or not value:
            raise StorageError(f"{field} must be a non-empty absolute path", code="invalid_storage_path")
        path = Path(value).expanduser()
        if not path.is_absolute() or ".." in path.parts:
            raise StorageError(f"{field} must be absolute and contain no traversal", code="invalid_storage_path")
        if path == Path(path.anchor):
            raise StorageError(f"{field} must not be a filesystem root", code="invalid_storage_path")
        return path

    def _existing_local_directory(self, value: Any, field: str) -> Path:
        path = self._clean_absolute_local(value, field)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise StorageError(f"{field} does not exist", code="storage_not_found") from exc
        if not resolved.is_dir():
            raise StorageError(f"{field} must be a directory", code="invalid_storage_path")
        return resolved

    def _local_output_directory(self, value: Any, dry_run: bool) -> Path:
        path = self._clean_absolute_local(value, "local_dir")
        if path.exists() or path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise StorageError("local_dir is not an accessible directory", code="storage_not_found") from exc
            if not resolved.is_dir():
                raise StorageError("local_dir must be a directory", code="invalid_storage_path")
            self._validate_local_write(resolved)
            return resolved
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise StorageError("local_dir parent must already exist", code="storage_not_found") from exc
        if not parent.is_dir():
            raise StorageError("local_dir parent must be a directory", code="invalid_storage_path")
        output = parent / path.name
        self._validate_local_write(output)
        if not dry_run:
            output.mkdir()
        return output

    def _secret_exclude(self, source: Path) -> Optional[str]:
        secret_path = self.secrets_path or default_secrets_path()
        resolved = secret_path.expanduser().resolve(strict=False)
        try:
            relative = resolved.relative_to(source)
        except ValueError:
            return None
        return "/" + _rsync_literal(relative.as_posix())

    @staticmethod
    def _rsync_args(
        source: Any,
        destination: Any,
        dry_run: bool,
        excludes: Iterable[str],
        *,
        preserve_permissions: bool,
    ) -> list[str]:
        argv = ["rsync", "-a", "--safe-links", "--mkpath", "--itemize-changes"]
        if not preserve_permissions:
            argv.extend(("--no-owner", "--no-group", "--no-perms", "--omit-dir-times"))
        if dry_run:
            argv.append("--dry-run")
        for pattern in excludes:
            argv.append(f"--exclude={pattern}")
        argv.extend(["--", _directory_contents(source), _directory_contents(destination)])
        return argv

    def _run_remote_rsync(
        self, source: Any, destination: Any, dry_run: bool, excludes: Iterable[str]
    ) -> Dict[str, Any]:
        ssh = self._require_ssh()
        from determined_compute.storage.auth import ssh_auth

        with ssh_auth(ssh, self.secrets_path) as (env_overrides, auth_options):
            ssh_options = [*auth_options, "-o", f"ConnectTimeout={self.config.connect_timeout_seconds}"]
            argv = [
                "rsync",
                "-a",
                "--safe-links",
                "--mkpath",
                "--itemize-changes",
                "-s",
                "-e",
                shlex.join(["ssh", *ssh_options]),
            ]
            if not self.config.preserve_permissions:
                argv.extend(("--no-owner", "--no-group", "--no-perms", "--omit-dir-times"))
            if dry_run:
                argv.append("--dry-run")
            for pattern in excludes:
                argv.append(f"--exclude={pattern}")
            argv.extend(["--", _directory_contents(source), _directory_contents(destination)])
            return self._run(argv, timeout=self.config.timeout_seconds, env_overrides=env_overrides)

    def _remote_spec(self, host_path: str) -> str:
        host = self._require_ssh().host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{host_path}"

    def _require_ssh(self) -> SSHConfig:
        if self.config.ssh is None:
            raise StorageError("SSH storage access is not configured", code="configuration_required")
        return self.config.ssh

    def _ssh_check(self, host_path: str) -> Dict[str, Any]:
        ssh = self._require_ssh()
        from determined_compute.storage.auth import ssh_auth

        script = (
            'if [ -e "$1" ] || [ -L "$1" ]; then e=1; else e=0; fi; '
            'if [ -L "$1" ]; then t=symlink; elif [ -d "$1" ]; then t=directory; '
            'elif [ -f "$1" ]; then t=file; elif [ "$e" = 1 ]; then t=other; else t=missing; fi; '
            'if [ "$e" = 1 ] && [ -r "$1" ]; then r=1; else r=0; fi; '
            'if [ "$e" = 1 ] && [ -w "$1" ]; then w=1; else w=0; fi; '
            'printf "exists=%s\\ntype=%s\\nreadable=%s\\nwritable=%s\\n" "$e" "$t" "$r" "$w"'
        )
        remote_command = "sh -c " + shlex.quote(script) + " sh " + shlex.quote(host_path)
        with ssh_auth(ssh, self.secrets_path) as (env_overrides, auth_options):
            argv = [
                "ssh",
                *auth_options,
                "-o",
                f"ConnectTimeout={self.config.connect_timeout_seconds}",
                ssh.host,
                remote_command,
            ]
            result = self._run(argv, timeout=self.config.timeout_seconds, env_overrides=env_overrides)
        parsed: Dict[str, str] = {}
        for line in result["output"].splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                parsed[key] = value
        if set(parsed) != {"exists", "type", "readable", "writable"}:
            raise StorageError("SSH storage check returned an invalid response", code="storage_check_failed")
        return {
            "exists": parsed["exists"] == "1",
            "type": parsed["type"],
            "readable": parsed["readable"] == "1",
            "writable": parsed["writable"] == "1",
        }

    @staticmethod
    def _transfer_result(
        operation: str,
        backend: str,
        source: Any,
        destination: Any,
        dry_run: bool,
        result: Mapping[str, Any],
        **details: Any,
    ) -> Dict[str, Any]:
        response: Dict[str, Any] = {
            "operation": operation,
            "backend": backend,
            "source": str(source),
            "destination": str(destination),
            "dry_run": dry_run,
            "completed": True,
            "output": result.get("output", ""),
            "truncated": bool(result.get("truncated", False)),
        }
        response.update(details)
        if "excludes" in response:
            response["excludes"] = list(response["excludes"])
        return response

    @staticmethod
    def _run(
        argv: Sequence[str],
        *,
        timeout: int,
        env_overrides: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        env = {**os.environ, **dict(env_overrides or {})}
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise StorageError("required storage transfer executable is unavailable", code="dependency_missing") from exc
        chunks: Dict[str, list[str]] = {"stdout": [], "stderr": []}
        was_truncated = {"stdout": False, "stderr": False}

        def drain(name: str, stream: Any) -> None:
            retained = 0
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                if retained < _OUTPUT_LIMIT:
                    piece = chunk[: _OUTPUT_LIMIT - retained]
                    chunks[name].append(piece)
                    retained += len(piece)
                    if len(piece) < len(chunk):
                        was_truncated[name] = True
                else:
                    was_truncated[name] = True

        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            finally:
                process.wait()
            raise StorageError("storage operation timed out", code="storage_timeout") from exc
        finally:
            for reader in readers:
                reader.join(timeout=0.25)
            if any(reader.is_alive() for reader in readers):
                _kill_process_group(process.pid, signal.SIGTERM)
                for reader in readers:
                    reader.join(timeout=0.25)
            if any(reader.is_alive() for reader in readers):
                _kill_process_group(process.pid, signal.SIGKILL)
                for reader in readers:
                    reader.join(timeout=0.25)
        if returncode != 0:
            raise StorageError(
                f"storage operation failed with exit code {returncode}",
                code="storage_operation_failed",
            )
        return {"output": "".join(chunks["stdout"]), "truncated": was_truncated["stdout"]}


def _directory_contents(value: Any) -> str:
    text = str(value)
    return text.rstrip("/") + "/"


def _rsync_literal(value: str) -> str:
    return "".join("\\" + character if character in "\\*?[]" else character for character in value)


def _kill_process_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


__all__ = ["StorageService"]
