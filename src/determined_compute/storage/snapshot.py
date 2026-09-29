"""Content-addressed, add-only code snapshots on mapped shared storage.

A snapshot materializes the exact tracked content of one git revision, plus explicitly
included working-tree files, as a read-only tree named by its content. Identical files
are stored once in an object store and linked into each tree when the filesystem
supports it. Existing objects, trees, and manifests are never modified or deleted.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from determined_compute.utils.secrets import default_secrets_path

from .config import StorageError
from .service import _SYNC_EXCLUDES

SCHEMA_VERSION = "determined-compute-snapshot-v1"
_FICLONE = 0x40049409
_CHUNK = 1 << 20
_MAX_PATTERNS = 64
_MAX_INCLUDES = 256
_GIT_TIMEOUT_SECONDS = 300
_MAX_SYMLINK_HOPS = 40
_LFS_POINTER = b"version https://git-lfs.github.com/spec/"
# Rules from the transfer exclusions that describe caches rather than secrets. They drop
# tracked files and expanded include directories, but never block an explicit include.
_CACHE_RULES = {".git/", ".cache/", "cache/", ".venv/", "__pycache__/", ".pytest_cache/", "*.pyc"}
_LINK_FALLBACK_ERRNOS = {
    errno.EMLINK,
    errno.EXDEV,
    errno.EPERM,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.ENOSYS,
}


@dataclass(frozen=True)
class _Blob:
    sha256: str
    size: int
    oid: Optional[str] = None
    source: Optional[Path] = None


@dataclass(frozen=True)
class _File:
    blob: _Blob
    executable: bool

    @property
    def mode(self) -> str:
        return "100755" if self.executable else "100644"


def _invalid(message: str, code: str = "invalid_request") -> StorageError:
    return StorageError(message, code=code)


def _corrupt(message: str) -> StorageError:
    return StorageError(
        f"{message}; existing snapshot content is never repaired automatically",
        code="snapshot_corrupt",
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pattern_matches(pattern: str, path: str) -> bool:
    """Match an rsync-style pattern component-wise against a relative path.

    A trailing ``/`` matches directories only, a leading ``/`` anchors the pattern at the
    repository root, and otherwise the pattern may match at any depth. Each component
    uses case-sensitive ``fnmatch`` rules.
    """
    parts = path.split("/")
    wanted = pattern.strip("/").split("/")
    limit = len(parts) - 1 if pattern.endswith("/") else len(parts)
    starts = [0] if pattern.startswith("/") else range(limit - len(wanted) + 1)
    return any(
        start + len(wanted) <= limit
        and all(fnmatchcase(part, want) for part, want in zip(parts[start:], wanted))
        for start in starts
    )


def _secret_rule(path: str, secrets_file: Optional[str]) -> Optional[str]:
    if secrets_file is not None and path == secrets_file:
        return "configured secrets file"
    for pattern in _SYNC_EXCLUDES:
        if pattern not in _CACHE_RULES and _pattern_matches(pattern, path):
            return pattern
    for part in path.split("/"):
        lowered = part.lower()
        if "credential" in lowered:
            return "*credential*"
        if "secret" in lowered:
            return "*secret*"
        if lowered in {"token", ".token"} or lowered.endswith(".token"):
            return "*.token"
    return None


def _cache_rule(path: str) -> Optional[str]:
    return next((rule for rule in sorted(_CACHE_RULES) if _pattern_matches(rule, path)), None)


def _string_list(value: Any, field: str, limit: int, length: int) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or len(value) > limit:
        raise _invalid(f"{field} must be a list of at most {limit} strings")
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > length
            or not item.isprintable()
        ):
            raise _invalid(f"{field} entries must be printable strings of 1 to {length} characters")
    return list(value)


def _revision(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isprintable()
        or value.startswith("-")
        or any(character.isspace() for character in value)
    ):
        raise _invalid("revision must be a git revision without whitespace or a leading '-'")
    return value


def _safe_relative(path: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise _invalid(f"unsafe snapshot path: {path!r}", "unsafe_path")
    return path


def _unsafe_symlink(path: str) -> StorageError:
    return _invalid(
        f"symlink {path} points outside the snapshot tree; only relative links that "
        "stay inside it are allowed",
        "unsafe_symlink",
    )


def _check_symlink(path: str, target: str) -> None:
    """Reject a target that leaves the tree on its own; chains are checked later."""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if (
        not target
        or "\0" in target
        or target.startswith("/")
        or resolved == ".."
        or resolved.startswith("../")
    ):
        raise _unsafe_symlink(path)


def _check_symlink_chains(symlinks: Dict[str, str]) -> None:
    """Resolve every link through the snapshot's own links and require it to stay inside.

    A component naming another snapshot link is replaced by that link's target, relative
    to the directory being walked, exactly as path lookup does. Any other component is
    applied lexically, which is at least as strict as a lookup: a missing or regular-file
    component would fail there. The caller guarantees that no link is also a parent
    directory of another entry, so each link's own directory is a real directory.
    """
    for path in sorted(symlinks):
        directory = path.split("/")[:-1]
        pending = list(reversed(symlinks[path].split("/")))
        hops = 1
        while pending:
            part = pending.pop()
            if part in {"", "."}:
                continue
            if part == "..":
                if not directory:
                    raise _unsafe_symlink(path)
                directory.pop()
                continue
            target = symlinks.get("/".join([*directory, part]))
            if target is None:
                directory.append(part)
                continue
            if target.startswith("/"):
                raise _unsafe_symlink(path)
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                raise _invalid(
                    f"symlink {path} does not resolve inside the snapshot tree within "
                    f"{_MAX_SYMLINK_HOPS} links",
                    "unsafe_symlink",
                )
            pending.extend(reversed(target.split("/")))


def _check_staged_symlinks(staging: Path, symlinks: Dict[str, str]) -> None:
    """Second guard on the materialized tree: every link must resolve beneath it."""
    root = Path(os.path.realpath(staging))
    for path in symlinks:
        resolved = Path(os.path.realpath(staging.joinpath(*path.split("/"))))
        if not resolved.is_relative_to(root):
            raise _unsafe_symlink(path)


def _git_environment() -> Dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return environment


def _git(repo: Path, *args: str, code: str = "snapshot_failed") -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise StorageError("git is unavailable", code="dependency_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise StorageError(f"git {args[0]} timed out", code="storage_timeout") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        raise StorageError(
            f"git {args[0]} failed" + (f": {detail[0][:200]}" if detail else ""), code=code
        )
    return completed.stdout


class _CatFile:
    """Stream blobs through one ``git cat-file --batch`` process."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.process: Optional[subprocess.Popen] = None

    def _start(self) -> subprocess.Popen:
        if self.process is None:
            try:
                self.process = subprocess.Popen(
                    ["git", "-C", str(self.repo), "cat-file", "--batch"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=_git_environment(),
                )
            except FileNotFoundError as exc:
                raise StorageError("git is unavailable", code="dependency_missing") from exc
        return self.process

    def read(self, oid: str, sink: Callable[[bytes], None]) -> int:
        process = self._start()
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(oid.encode("ascii") + b"\n")
        process.stdin.flush()
        header = process.stdout.readline().split()
        if len(header) != 3 or header[0].decode("ascii", "replace") != oid or header[1] != b"blob":
            raise StorageError(f"git object {oid} is unavailable", code="snapshot_failed")
        size = int(header[2])
        remaining = size
        while remaining:
            chunk = process.stdout.read(min(_CHUNK, remaining))
            if not chunk:
                raise StorageError("git object stream ended early", code="snapshot_failed")
            sink(chunk)
            remaining -= len(chunk)
        if process.stdout.read(1) != b"\n":
            raise StorageError("git object stream is malformed", code="snapshot_failed")
        return size

    def close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()

    def __enter__(self) -> "_CatFile":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _copy_blob(blob: _Blob, handle: Any, cat: _CatFile) -> None:
    """Write a blob and prove that the bytes still match the recorded digest."""
    digest = hashlib.sha256()
    written = 0

    def sink(chunk: bytes) -> None:
        nonlocal written
        digest.update(chunk)
        handle.write(chunk)
        written += len(chunk)

    if blob.oid is not None:
        cat.read(blob.oid, sink)
    else:
        assert blob.source is not None
        with open(blob.source, "rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                sink(chunk)
    if written != blob.size or digest.hexdigest() != blob.sha256:
        raise StorageError(
            "a snapshot source changed while it was copied; run the snapshot again",
            code="snapshot_source_changed",
        )


def _hash_include(path: Path) -> _Blob:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return _Blob(digest.hexdigest(), size, source=path)


def _expand_include(
    repo: Path, value: str, skip: Callable[[str, str], None]
) -> Iterator[Tuple[str, Path, bool]]:
    """Yield (relative path, local path, explicitly named) for one include argument.

    A ``.git`` entry of any type found while walking an included directory (a repository,
    worktree, or submodule checkout) is reported through ``skip`` and never copied;
    naming a path inside ``.git`` explicitly is an error.
    """
    candidate = Path(value).expanduser()
    if ".." in candidate.parts:
        raise _invalid(f"include {value} must not contain '..'", "invalid_include")
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = Path(os.path.normpath(candidate))
    if not candidate.is_relative_to(repo):
        raise _invalid(f"include {value} is outside repo_dir", "invalid_include")
    if ".git" in candidate.relative_to(repo).parts:
        raise _invalid(f"include {value} reaches into .git", "invalid_include")
    try:
        info = os.lstat(candidate)
    except OSError as exc:
        raise _invalid(f"include {value} does not exist", "invalid_include") from exc
    if stat.S_ISLNK(info.st_mode) or candidate.resolve() != candidate:
        raise _invalid(f"include {value} must not be or traverse a symlink", "invalid_include")

    def relative(path: Path) -> str:
        text = path.relative_to(repo).as_posix()
        if ".git" in text.split("/"):
            raise _invalid(f"include {value} reaches into .git", "invalid_include")
        return _safe_relative(text)

    if stat.S_ISREG(info.st_mode):
        yield relative(candidate), candidate, True
        return
    if not stat.S_ISDIR(info.st_mode):
        raise _invalid(f"include {value} is not a regular file or directory", "invalid_include")
    for directory, dirnames, filenames in os.walk(candidate):
        if ".git" in dirnames or ".git" in filenames:
            skip(Path(directory, ".git").relative_to(repo).as_posix(), "git_metadata")
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        filenames = [name for name in filenames if name != ".git"]
        for name in dirnames:
            if os.path.islink(os.path.join(directory, name)):
                raise _invalid(
                    f"include {value} contains the symlink {name}", "invalid_include"
                )
        for name in sorted(filenames):
            path = Path(directory, name)
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise _invalid(
                    f"include {value} contains {name}, which is not a regular file",
                    "invalid_include",
                )
            yield relative(path), path, False


def _reflink(source: Path, destination: Path) -> None:
    import fcntl

    with open(source, "rb") as src, open(destination, "xb") as dst:
        fcntl.ioctl(dst.fileno(), _FICLONE, src.fileno())


def _resolve_link_mode(configured: str, tmp_dir: Path) -> str:
    if configured != "auto":
        return configured
    token = uuid.uuid4().hex
    probe = tmp_dir / f"probe-{token}"
    clone = tmp_dir / f"probe-{token}.clone"
    link = tmp_dir / f"probe-{token}.link"
    probe.write_bytes(b"probe\n")
    try:
        try:
            _reflink(probe, clone)
            return "reflink"
        except OSError:
            pass
        try:
            os.link(probe, link)
            return "hardlink"
        except OSError:
            return "copy"
    finally:
        for path in (probe, clone, link):
            path.unlink(missing_ok=True)


def _object_path(objects: Path, blob: _Blob, executable: bool) -> Path:
    # Hard links share one inode and therefore one mode, so executables are separate.
    return objects / blob.sha256[:2] / (blob.sha256 + (".x" if executable else ""))


def _check_existing_file(path: Path, blob: _Blob, verify: bool, label: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _corrupt(f"{label} is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != blob.size:
        raise _corrupt(f"{label} does not have the recorded size")
    if verify and _file_sha256(path) != blob.sha256:
        raise _corrupt(f"{label} does not have the recorded content")


def _store_object(
    objects: Path, tmp_dir: Path, file: _File, cat: _CatFile, verify: bool
) -> bool:
    """Add one object without clobbering; return whether this call created it."""
    final = _object_path(objects, file.blob, file.executable)
    label = f"object {final.name}"
    if final.exists():
        _check_existing_file(final, file.blob, verify, label)
        return False
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_dir / uuid.uuid4().hex
    try:
        with open(temporary, "xb") as handle:
            _copy_blob(file.blob, handle, cat)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o555 if file.executable else 0o444)
        try:
            os.link(temporary, final)
        except FileExistsError:
            _check_existing_file(final, file.blob, verify, label)
            return False
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK_ERRNOS:
                raise
            if final.exists():
                _check_existing_file(final, file.blob, verify, label)
                return False
            # A concurrent writer of the same name wrote identical content.
            os.rename(temporary, final)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _materialize(source: Path, destination: Path, mode: str) -> bool:
    """Link or clone an object into a tree; return True when it fell back to a copy."""
    if mode == "hardlink":
        try:
            os.link(source, destination)
            return False
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK_ERRNOS:
                raise
    elif mode == "reflink":
        try:
            _reflink(source, destination)
            return False
        except OSError:
            destination.unlink(missing_ok=True)
    shutil.copyfile(source, destination)
    return True


def _seal_below(tree: Path) -> None:
    """Make every directory below the tree root read-only, deepest first.

    The root stays writable until it is renamed into place, because moving a directory
    to another parent updates its ``..`` entry and therefore needs write permission.
    """
    for directory, _dirnames, _filenames in os.walk(tree, topdown=False):
        if Path(directory) != tree:
            os.chmod(directory, 0o555)


def _remove_tree(tree: Path) -> None:
    for directory, _dirnames, _filenames in os.walk(tree):
        os.chmod(directory, 0o755)
    shutil.rmtree(tree)


def _verify_tree(
    tree: Path, files: Dict[str, _File], symlinks: Dict[str, str], verify: bool
) -> None:
    if tree.is_symlink() or not tree.is_dir():
        raise _corrupt(f"snapshot tree {tree.name} is not a directory")
    for path, file in files.items():
        target = tree.joinpath(*path.split("/"))
        _check_existing_file(target, file.blob, verify, f"snapshot file {path}")
    for path, target_text in symlinks.items():
        link = tree.joinpath(*path.split("/"))
        if not link.is_symlink() or os.readlink(link) != target_text:
            raise _corrupt(f"snapshot symlink {path} does not match")


def _publish_manifest(path: Path, tmp_dir: Path, manifest: Dict[str, Any]) -> bytes:
    """Write the manifest once; an existing manifest keeps its original bytes."""
    if path.exists():
        return path.read_bytes()
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = tmp_dir / f"manifest-{uuid.uuid4().hex}"
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return path.read_bytes()
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK_ERRNOS:
                raise
            if path.exists():
                return path.read_bytes()
            os.rename(temporary, path)
        return payload
    finally:
        temporary.unlink(missing_ok=True)


def create_snapshot(
    storage: Any,
    repo_dir: str,
    revision: str = "HEAD",
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    dry_run: bool = True,
    verify: bool = False,
) -> Dict[str, Any]:
    if not isinstance(dry_run, bool) or not isinstance(verify, bool):
        raise _invalid("dry_run and verify must be booleans")
    revision = _revision(revision)
    includes = _string_list(include, "include", _MAX_INCLUDES, 4096)
    exclude_patterns = sorted(set(_string_list(exclude, "exclude", _MAX_PATTERNS, 256)))
    container_root, mount, host_root = storage._snapshot_mount()
    local = (
        storage._local_mapping(host_root, mount, write=True)
        if storage.config.mode != "ssh"
        else None
    )
    if local is None:
        raise StorageError(
            "snapshots need a local, writable view of snapshots.root; SSH-only access "
            "is not supported",
            code="configuration_required",
        )
    root, mount_root = storage._safe_mapped_path(local, host_root)
    repo = storage._existing_local_directory(repo_dir, "repo_dir")
    toplevel = _git(repo, "rev-parse", "--show-toplevel", code="invalid_repository")
    if Path(toplevel.decode("utf-8", "surrogateescape").strip()).resolve() != repo:
        raise _invalid("repo_dir must be the top level of a git work tree", "invalid_repository")
    commit = _git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{revision}^{{commit}}",
        code="revision_not_found",
    ).decode("ascii").strip()
    tree_id = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()

    secrets_path = (storage.secrets_path or default_secrets_path()).expanduser()
    try:
        secrets_file: Optional[str] = (
            secrets_path.resolve(strict=False).relative_to(repo).as_posix()
        )
    except ValueError:
        secrets_file = None

    files: Dict[str, _File] = {}
    symlinks: Dict[str, str] = {}
    excluded: Dict[str, Dict[str, str]] = {}
    skipped: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    tracked: List[Tuple[str, bool, str]] = []
    tracked_links: List[Tuple[str, str]] = []
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _separator, raw_path = record.partition(b"\t")
        mode, _kind, oid = meta.decode("ascii").split(" ")
        path = _safe_relative(raw_path.decode("utf-8", "surrogateescape"))
        if mode == "160000":
            skipped.append({"path": path, "reason": "submodule"})
            continue
        rule = _secret_rule(path, secrets_file)
        reason = "secret_like"
        if rule is None:
            rule, reason = _cache_rule(path), "cache"
        if rule is None:
            rule = next((item for item in exclude_patterns if _pattern_matches(item, path)), None)
            reason = "exclude_pattern"
        if rule is not None:
            excluded[path] = {"path": path, "reason": reason, "rule": rule}
        elif mode == "120000":
            tracked_links.append((path, oid))
        elif mode in {"100644", "100755"}:
            tracked.append((path, mode == "100755", oid))
        else:
            skipped.append({"path": path, "reason": f"unsupported_mode_{mode}"})

    blobs: Dict[str, _Blob] = {}
    pointers = set()
    with _CatFile(repo) as cat:
        for path, oid in tracked_links:
            target = bytearray()
            cat.read(oid, target.extend)
            text = bytes(target).decode("utf-8", "surrogateescape")
            _check_symlink(path, text)
            symlinks[path] = text
        for path, executable, oid in tracked:
            blob = blobs.get(oid)
            if blob is None:
                digest = hashlib.sha256()
                head = bytearray()

                def sink(chunk: bytes) -> None:
                    digest.update(chunk)
                    if len(head) < len(_LFS_POINTER):
                        head.extend(chunk[: len(_LFS_POINTER) - len(head)])

                size = cat.read(oid, sink)
                blob = blobs[oid] = _Blob(digest.hexdigest(), size, oid=oid)
                if size < 1024 and bytes(head) == _LFS_POINTER:
                    pointers.add(oid)
            if oid in pointers:
                warnings.append({
                    "code": "lfs_pointer",
                    "path": path,
                    "message": "tracked content is a Git LFS pointer, not the large file",
                })
            files[path] = _File(blob, executable)

    sources: List[Dict[str, Any]] = [
        {"kind": "git", "revision": commit, "tree": tree_id, "files": len(files)}
    ]
    included: Dict[str, Dict[str, Any]] = {}
    tracked_paths = set(files) | set(symlinks) | set(excluded)

    def skip(path: str, reason: str) -> None:
        entry = {"path": path, "reason": reason}
        if entry not in skipped:
            skipped.append(entry)

    for value in includes:
        for relative, local_path, explicit in _expand_include(repo, value, skip):
            if not explicit:
                rule = next(
                    (item for item in exclude_patterns if _pattern_matches(item, relative)), None
                )
                reason = "exclude_pattern"
                if rule is None:
                    rule, reason = _cache_rule(relative), "cache"
                if rule is not None:
                    if relative not in files and relative not in symlinks:
                        excluded.setdefault(
                            relative, {"path": relative, "reason": reason, "rule": rule}
                        )
                    continue
            secret = _secret_rule(relative, secrets_file)
            if secret is not None:
                raise StorageError(
                    f"include {relative} matches the secret-like rule {secret!r} and is "
                    "never snapshotted; rename or move it",
                    code="secret_like_include",
                )
            blob = _hash_include(local_path)
            symlinks.pop(relative, None)
            excluded.pop(relative, None)
            files[relative] = _File(blob, bool(os.lstat(local_path).st_mode & stat.S_IXUSR))
            included[relative] = {
                "kind": "include",
                "path": relative,
                "sha256": blob.sha256,
                "bytes": blob.size,
                "overrides": relative in tracked_paths,
            }
    sources.extend(included[path] for path in sorted(included))

    entries = set(files) | set(symlinks)
    if not entries:
        raise _invalid("the snapshot would contain no files")
    parents = {
        "/".join(parts[:index])
        for parts in (path.split("/") for path in entries)
        for index in range(1, len(parts))
    }
    conflicts = sorted(entries & parents)
    if conflicts:
        raise _invalid(
            f"snapshot paths conflict with directories: {', '.join(conflicts[:5])}",
            "invalid_include",
        )
    # Checked on the final link map, before any identity is computed, so a preview fails too.
    _check_symlink_chains(symlinks)

    file_map = {
        path: {"sha256": file.blob.sha256, "bytes": file.blob.size, "mode": file.mode}
        for path, file in sorted(files.items())
    }
    symlink_map = dict(sorted(symlinks.items()))
    excluded_list = [excluded[path] for path in sorted(excluded)]
    skipped.sort(key=lambda item: item["path"])
    content_id = _canonical_sha256({"files": file_map, "symlinks": symlink_map})
    snapshot_key = _canonical_sha256({
        "schema_version": SCHEMA_VERSION,
        "content_id": content_id,
        "revision": commit,
        "tree": tree_id,
        "sources": sources,
        "excluded": excluded_list,
        "skipped": skipped,
        "exclude_patterns": exclude_patterns,
    })

    tree_path = root / "trees" / content_id
    manifest_path = root / "manifests" / f"{snapshot_key}.json"
    objects = root / "objects" / "sha256"
    tmp_dir = root / "tmp"
    workdir = posixpath.join(container_root, "trees", content_id)
    manifest_container = posixpath.join(container_root, "manifests", f"{snapshot_key}.json")
    tree_existing = os.path.lexists(tree_path)
    manifest_existing = manifest_path.exists()
    if tree_existing:
        _verify_tree(tree_path, files, symlinks, verify)
    unique = {
        (file.blob.sha256, file.executable): file for file in files.values()
    }
    configured = storage.config.snapshots.link_mode
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "repo_dir": str(repo),
        "revision": commit,
        "tree": tree_id,
        "content_id": content_id,
        "snapshot_key": snapshot_key,
        "workdir": workdir,
        "host_path": posixpath.join(host_root, "trees", content_id),
        "local_path": str(tree_path),
        "existing": manifest_existing,
        "tree_existing": tree_existing,
        "files": len(files),
        "symlinks": len(symlinks),
        "bytes": sum(file.blob.size for file in files.values()),
        "excluded": excluded_list,
        "skipped": skipped,
        "warnings": warnings,
        "request_fields": {"workdir": workdir, "code_revision": commit},
    }

    if dry_run:
        if tree_existing:
            missing: List[_File] = []
        elif configured == "copy":
            missing = list(unique.values())
        else:
            missing = [
                file for file in unique.values()
                if not _object_path(objects, file.blob, file.executable).exists()
            ]
        result.update({
            "manifest_path": manifest_container if manifest_existing else None,
            "manifest_sha256": (
                hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                if manifest_existing
                else None
            ),
            "new_objects": 0 if configured == "copy" else len(missing),
            "new_bytes": sum(file.blob.size for file in missing),
            "link_mode": configured,
            "link_fallbacks": 0,
        })
        return result

    storage._mkdir_beneath_root(root, mount_root)
    for directory in (objects, root / "trees", root / "manifests", tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    new_objects = new_bytes = fallbacks = 0
    link_mode = configured
    with _CatFile(repo) as cat:
        if not tree_existing:
            link_mode = _resolve_link_mode(configured, tmp_dir)
            if link_mode != "copy":
                for file in unique.values():
                    if _store_object(objects, tmp_dir, file, cat, verify):
                        new_objects += 1
                        new_bytes += file.blob.size
            staging = tmp_dir / f"tree-{uuid.uuid4().hex}"
            staging.mkdir()
            try:
                for path, file in sorted(files.items()):
                    destination = staging.joinpath(*path.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if link_mode == "copy":
                        with open(destination, "xb") as handle:
                            _copy_blob(file.blob, handle, cat)
                        new_bytes += file.blob.size
                        copied = True
                    else:
                        source = _object_path(objects, file.blob, file.executable)
                        copied = _materialize(source, destination, link_mode)
                        fallbacks += int(copied)
                    if copied or link_mode == "reflink":
                        os.chmod(destination, 0o555 if file.executable else 0o444)
                for path, target in sorted(symlinks.items()):
                    destination = staging.joinpath(*path.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(target, destination)
                _check_staged_symlinks(staging, symlinks)
                _seal_below(staging)
                try:
                    os.rename(staging, tree_path)
                except OSError:
                    if not os.path.lexists(tree_path):
                        raise
                    # A concurrent snapshot published the same content first.
                    _verify_tree(tree_path, files, symlinks, verify)
                else:
                    os.chmod(tree_path, 0o555)
            finally:
                if staging.exists():
                    _remove_tree(staging)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "content_id": content_id,
        "revision": commit,
        "tree": tree_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "files": file_map,
        "symlinks": symlink_map,
        "excluded": excluded_list,
        "skipped": skipped,
        "exclude_patterns": exclude_patterns,
        "warnings": warnings,
    }
    payload = _publish_manifest(manifest_path, tmp_dir, manifest)
    result.update({
        "manifest_path": manifest_container,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "new_objects": new_objects,
        "new_bytes": new_bytes,
        "link_mode": link_mode,
        "link_fallbacks": fallbacks,
    })
    return result


__all__ = ["SCHEMA_VERSION", "create_snapshot"]
