"""Plan-time checks and payloads for the three code sources.

``git`` pins a revision of a repository on shared storage, which the container clones with
``--shared``; ``context`` packs the tracked files at a revision, plus explicit working-tree
includes, into the task context; ``path`` runs a shared directory in place and is never pinned.
Every check here is read-only: git runs without user configuration or filter commands, and
nothing contacts the master.
"""

from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
import posixpath
import re
import stat
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    BinaryIO,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from determined_compute.utils.secrets import default_secrets_path

PathLike = Union[str, "os.PathLike[str]"]

# harness/determined/common/constants.py: the smaller of the HTTP and WebSocket limits (128 MiB),
# less base64 overhead, less 1 MiB for the message envelope.
MAX_CONTEXT_SIZE = (128 * 1024 * 1024 // 8) * 6 - 1024 * 1024
GIT_TIMEOUT_SECONDS = 120
PROVENANCE_PATH = ".code-provenance.json"
# Any fixed mtime makes payloads reproducible. This is the master's own archive default
# (pkg.DeterminedBirthday, 2017-08-02T00:00:00Z); a date after 1980 keeps zip and wheel builds
# in the container working.
CONTEXT_MTIME = 1501632000
# tar type flags, as the harness and the master's archive package use them.
REGTYPE, SYMTYPE, DIRTYPE = ord("0"), ord("2"), ord("5")
_TYPE_NAMES = {REGTYPE: "file", SYMTYPE: "symlink", DIRTYPE: "dir"}
# The harness extracts with ``mode & 0o755`` and clears exec bits without owner exec, so these
# modes survive unchanged. Files follow git's executable-bit model, so a working-tree include
# gets the same mode under any umask.
_FILE_MODE, _EXEC_MODE, _DIR_MODE, _LINK_MODE = 0o644, 0o755, 0o755, 0o755

_MAX_REVISION = 256
_MAX_INCLUDES, _MAX_INCLUDE_LENGTH = 256, 4096
_MAX_EXCLUDES, _MAX_EXCLUDE_LENGTH = 64, 256
_MAX_SYMLINK_HOPS = 40
_MAX_REPORTED = 20
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md: pointers are under 1024 bytes, and
# keys after the version are sorted, so extensions precede oid and size.
_LFS_POINTER_LIMIT = 1024
_LFS_POINTER = re.compile(
    rb"version https://git-lfs\.github\.com/spec/v1\n"
    rb"(?:ext-[^\n]*\n)*oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?"
)
_FALSE = {"false", "no", "off", "0"}

# Rules from PR #1's snapshot enumeration. Cache rules describe caches, not secrets: they drop
# tracked files and cache-like paths below an included directory, but never a named file.
_CACHE_RULES = (".cache/", ".git/", ".pytest_cache/", ".venv/", "__pycache__/", "cache/", "*.pyc")
# Credential stores that are never uploaded, even when an include names them. Secret rules are
# lowercase and match the case-folded path.
_HARD_SECRET_RULES = (
    ".ssh/",
    ".aws/",
    ".config/gcloud/",
    ".config/gh/",
    ".docker/config.json",
    ".gnupg/",
    ".kube/",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
)
# Name heuristics that also match ordinary files (id_rsa_parser.py, id_ed25519.pub, a secrets.py
# module): excluded unless an include names the file. They are the secret-like transfer
# exclusions, plus key containers, key-name prefixes and the substring rules below.
_SOFT_SECRET_RULES = (
    ".local/",
    ".env*",
    "*.env",
    "*.env.*",
    ".secrets*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    ".credentials/",
    "credentials/",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
)


class CodeError(ValueError):
    """A code-source check failed. ``code`` is stable and ``details`` is JSON-safe."""

    def __init__(self, message: str, *, code: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details: Dict[str, Any] = details or {}


@dataclass(frozen=True)
class CodeWarning:
    code: str
    message: str
    paths: Tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class GitCode:
    """A revision pinned in a repository that the container clones."""

    source: str = field(default="git", init=False)
    repo: str  # container path
    commit: str
    uses_lfs: bool
    warnings: Tuple[CodeWarning, ...] = ()
    content_digest: str  # the commit


@dataclass(frozen=True, kw_only=True)
class ContextCode:
    """A task context: ``files`` is the create request's file list, sorted by path."""

    source: str = field(default="context", init=False)
    repo: str  # local working tree
    commit: str
    dirty: bool
    files: Tuple[Dict[str, Any], ...]
    manifest: Tuple[Dict[str, str], ...]
    content_digest: str
    size: int  # as the harness counts it
    included: Tuple[str, ...]
    excluded: Tuple[Dict[str, str], ...]
    skipped: Tuple[Dict[str, str], ...]
    warnings: Tuple[CodeWarning, ...] = ()


@dataclass(frozen=True, kw_only=True)
class PathCode:
    """A shared directory run in place; its content is never pinned.

    ``observed_commit`` and ``observed_dirty`` describe the git work tree holding the directory
    as the planner saw it, or are None; nothing ties them to what the task runs.
    """

    source: str = field(default="path", init=False)
    dir: str
    content_digest: str = field(default="unpinned", init=False)
    observed_commit: Optional[str] = None
    observed_dirty: Optional[bool] = None
    verified: bool = field(default=False, init=False)


# git


def _git_environment() -> Dict[str, str]:
    """A fresh environment: no user or system config, prompts, pager, or network fetches."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        # Status-like commands must not refresh the index of a repository they only read.
        "GIT_OPTIONAL_LOCKS": "0",
        # A partial clone would otherwise fetch missing objects over the network.
        "GIT_NO_LAZY_FETCH": "1",
        # The container clone never fetches refs/replace, so the plan must not honour it either.
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def run_git(
    repo: PathLike,
    *args: str,
    input: Optional[bytes] = None,
    ok: Tuple[int, ...] = (0,),
    code: str = "git_failed",
    timeout: float = GIT_TIMEOUT_SECONDS,
    trust: bool = False,
    config: Optional[Mapping[str, str]] = None,
) -> Tuple[int, bytes]:
    """Run git in ``repo`` from an argv list, never a shell; return (exit status, stdout).

    Callers pass user text only after ``--end-of-options`` or as validated object names. A
    status outside ``ok`` raises ``code``; a missing git or a timeout have codes of their own.
    ``trust`` accepts a repository owned by another user; ``config`` applies to this call only.
    """
    # core.fsmonitor names a command in the repository's config; scanning is enough here.
    argv = ["git", "--no-pager", "-c", "core.fsmonitor=false"]
    if trust:
        # safe.directory is read only from protected config, which the fresh environment
        # hides, and a shared repository may belong to another uid; the container clone
        # trusts it the same way. Only plan_git trusts: its commands run nothing the
        # repository configures, while a diff over a work tree runs its filters.
        argv += ["-c", f"safe.directory={os.fspath(repo)}"]
    argv += ["-C", os.fspath(repo), *args]
    environment = _git_environment()
    if config:
        # These take a key verbatim, while -c splits at the first '=', which a filter
        # driver's name may contain.
        environment["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    feed: Dict[str, Any] = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        completed = subprocess.run(
            argv, capture_output=True, env=environment, timeout=timeout, **feed
        )
    except FileNotFoundError as exc:
        raise CodeError("git is not installed", code="git_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeError(f"git {args[0]} timed out after {timeout:g}s", code="git_timeout") from exc
    if completed.returncode not in ok:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = f": {lines[0][:300]}" if lines else ""
        raise CodeError(f"git {args[0]} failed{detail}", code=code)
    return completed.returncode, completed.stdout


def _revision(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_REVISION
        or value.startswith("-")
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise CodeError(
            f"revision must be 1 to {_MAX_REVISION} printable characters without whitespace "
            "or a leading '-'",
            code="invalid_revision",
        )
    return value


def _repository(repo: PathLike, *, work_tree: bool) -> Tuple[Path, Path]:
    """Return (resolved repository, common git dir).

    ``repo`` must be the repository itself: git discovers a parent repository from any
    directory inside it, and a clone of such a directory fails in the container. Without
    ``work_tree`` it is a repository for the container to clone, which is trusted and must
    hold its own objects.
    """
    resolved = Path(os.path.realpath(repo))
    if not resolved.is_dir():
        raise CodeError(f"repository {repo} is not a directory", code="invalid_repository")
    _, output = run_git(
        resolved,
        "rev-parse",
        "--is-inside-git-dir",
        "--absolute-git-dir",
        "--path-format=absolute",
        "--git-common-dir",
        code="invalid_repository",
        trust=not work_tree,
    )
    inside, git_dir, common = os.fsdecode(output).splitlines()
    if inside == "true":
        top = git_dir
    else:
        _, output = run_git(
            resolved,
            "rev-parse",
            "--show-toplevel",
            code="invalid_repository",
            trust=not work_tree,
        )
        top = os.fsdecode(output).strip()
    if (work_tree and inside == "true") or Path(top) != resolved:
        wanted = "the top level of a work tree" if work_tree else "a repository's top level"
        raise CodeError(
            f"{repo} must be {wanted}, not a directory inside one", code="invalid_repository"
        )
    if not work_tree:
        _check_self_contained(resolved, Path(git_dir), Path(common), bare=inside == "true")
    return resolved, Path(common)


def _check_self_contained(root: Path, git_dir: Path, common: Path, *, bare: bool) -> None:
    """Require the git directory and every object inside the repository.

    The container clones the repository by its container path. A git directory elsewhere (a
    linked worktree, ``--separate-git-dir``, a submodule checkout) and alternates both name
    planner-side paths, which the container cannot resolve.
    """
    expected = root if bare else root / ".git"
    if (
        git_dir != expected
        or common != expected
        or (not bare and (expected.is_symlink() or not expected.is_dir()))
    ):
        raise CodeError(
            f"{root} keeps its git directory at {git_dir}, outside the repository, which the "
            "container cannot reach (a linked worktree, a separate git directory or a "
            "submodule checkout); plan the main repository or a regular clone instead",
            code="invalid_repository",
        )
    try:
        lines = (common / "objects" / "info" / "alternates").read_bytes().splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodeError(f"cannot read the alternates of {root}: {exc}", code="git_failed") from exc
    if any(line.strip() and not line.startswith(b"#") for line in lines):
        raise CodeError(
            f"{root} borrows objects from another repository through alternates, whose "
            "planner-side paths the container cannot resolve; run 'git repack -a -d' there "
            "and remove objects/info/alternates, or plan a regular clone",
            code="invalid_repository",
        )


def resolve_commit(repo: PathLike, revision: str = "HEAD", *, trust: bool = False) -> str:
    """Resolve ``revision`` to the full SHA of a commit."""
    revision = _revision(revision)
    status, output = run_git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{revision}^{{commit}}",
        ok=(0, 1),
        trust=trust,
    )
    commit = output.decode("ascii", "replace").strip()
    if status or not _SHA.fullmatch(commit):
        raise CodeError(
            f"revision {revision!r} does not name a commit in {repo}", code="revision_not_found"
        )
    return commit


def _check_not_partial(repo: Path, where: str) -> None:
    _, output = run_git(
        repo,
        "config",
        "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.promisor)$",
        ok=(0, 1),
        trust=True,
    )
    for line in output.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        if key == "extensions.partialclone" or value.strip().lower() not in _FALSE:
            raise CodeError(
                f"{where} is a partial clone ({key}); a clone of it would fetch missing objects "
                "over the network, which the container cannot do. Use a full clone.",
                code="partial_clone",
            )


def _check_on_ref(repo: Path, commit: str, where: str) -> None:
    _, output = run_git(
        repo,
        "for-each-ref",
        "--count=1",
        "--format=%(refname)",
        "--contains",
        commit,
        "refs/heads",
        "refs/tags",
        "refs/remotes",
        trust=True,
    )
    if not output.strip():
        raise CodeError(
            f"commit {commit} is on no branch, tag or remote-tracking ref of {where}. The "
            "container clone borrows objects from the repository through alternates, and git "
            "gc there prunes unreachable commits; commit it on a branch or tag it first.",
            code="commit_not_on_ref",
        )


def _tree(
    repo: Path, commit: str, *, trust: bool = False
) -> List[Tuple[str, str, str, Optional[int]]]:
    """List (path, mode, object, size) for every entry at ``commit``; gitlinks have no size."""
    _, output = run_git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, trust=trust)
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, _kind, oid, size = meta.decode("ascii").split()
        path = raw_path.decode("utf-8", "surrogateescape")
        if size != "-" and not size.isdigit():
            # ls-tree -l prints BAD for an object missing from the store (a partial clone,
            # broken alternates, corruption); lazy fetching is off while planning.
            raise CodeError(
                f"git object {oid} for {path!r} is missing from {repo}", code="git_failed"
            )
        entries.append((path, mode, oid, None if size == "-" else int(size)))
    return entries


def _read_blobs(repo: Path, oids: Iterable[str], *, trust: bool = False) -> Dict[str, bytes]:
    """Read blobs through one ``git cat-file --batch`` process."""
    wanted = sorted(set(oids))
    if not wanted:
        return {}
    requests = "".join(f"{oid}\n" for oid in wanted).encode("ascii")
    _, output = run_git(repo, "cat-file", "--batch", input=requests, trust=trust)
    blobs: Dict[str, bytes] = {}
    offset = 0
    for oid in wanted:
        end = output.find(b"\n", offset)
        header = output[offset:end].split() if end >= 0 else []
        if len(header) != 3 or header[0] != oid.encode() or header[1] != b"blob":
            raise CodeError(f"git object {oid} is unavailable in {repo}", code="git_failed")
        start = end + 1
        stop = start + int(header[2])
        blobs[oid] = output[start:stop]
        offset = stop + 1
    return blobs


def _lfs_pointer(content: bytes) -> Optional[Tuple[str, int]]:
    """Return (oid, size) when ``content`` is a Git LFS pointer."""
    if len(content) >= _LFS_POINTER_LIMIT:
        return None
    match = _LFS_POINTER.fullmatch(content)
    return (match.group(1).decode(), int(match.group(2))) if match else None


def _bounded(paths: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(paths)[:_MAX_REPORTED])


# Paths and mounts


def _container_path(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\0" in value
        or ".." in PurePosixPath(value).parts
    ):
        raise CodeError(
            f"{name} must be an absolute container path without '..'", code="invalid_path"
        )
    return posixpath.normpath(value)


def under_root(path: str, roots: Iterable[str]) -> bool:
    """Whether a normalized absolute container path lies at or below one of ``roots``."""
    for root in roots:
        root = posixpath.normpath(root)
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return True
    return False


# Sources


def plan_git(
    repo: PathLike, container_repo: str, mount_roots: Sequence[str], revision: str = "HEAD"
) -> GitCode:
    """Check a ``git`` source and pin its revision.

    ``repo`` is where the planner reads the repository; ``container_repo`` is the same
    repository as the container sees it, which must lie under one of the bind-mount
    ``mount_roots``.
    """
    revision = _revision(revision)
    where = _container_path(container_repo, "repo")
    if not under_root(where, mount_roots):
        raise CodeError(
            f"repo {where} is not under a bind-mounted root ({', '.join(mount_roots) or 'none'})",
            code="repo_not_mounted",
            details={"repo": where, "roots": list(mount_roots)},
        )
    root, common = _repository(repo, work_tree=False)
    # Before anything reads objects, which a partial clone might not have.
    _check_not_partial(root, where)
    commit = resolve_commit(root, revision, trust=True)
    _check_on_ref(root, commit, where)

    warnings: List[CodeWarning] = []
    candidates: Dict[str, List[str]] = {}
    gitlinks = []
    for path, mode, oid, size in _tree(root, commit, trust=True):
        if mode == "160000":
            gitlinks.append(path)
        elif mode in {"100644", "100755"} and size is not None and size < _LFS_POINTER_LIMIT:
            candidates.setdefault(oid, []).append(path)
    if gitlinks:
        warnings.append(_submodule_warning(gitlinks))
    pointers = {
        oid: pointer
        for oid, content in _read_blobs(root, candidates, trust=True).items()
        if (pointer := _lfs_pointer(content)) is not None
    }
    missing = sorted(
        path
        for oid, (lfs_oid, lfs_size) in pointers.items()
        if not _lfs_object_present(common, lfs_oid, lfs_size)
        for path in candidates[oid]
    )
    if missing:
        raise CodeError(
            f"{len(missing)} Git LFS pointer(s) at {commit} have no object in the LFS store of "
            f"{where}; run 'git lfs fetch' for that commit in the repository",
            code="lfs_object_missing",
            details={"commit": commit, "count": len(missing), "paths": list(_bounded(missing))},
        )
    return GitCode(
        repo=where,
        commit=commit,
        uses_lfs=bool(pointers),
        warnings=tuple(warnings),
        content_digest=commit,
    )


def plan_path(directory: str, local_dir: Optional[PathLike] = None) -> PathCode:
    """A ``path`` source runs in place, so its content is reported as unpinned.

    ``local_dir`` is where the planner reads the same directory. When it lies in a git work
    tree, the plan reports the HEAD and dirty state observed there, unverified; otherwise, or
    when git cannot read it, neither. Untracked files count as dirty, since they run too.
    """
    where = _container_path(directory, "dir")
    if local_dir is None:
        return PathCode(dir=where)
    try:
        commit, dirty = _observe(Path(local_dir))
    except CodeError:
        commit, dirty = None, None
    return PathCode(dir=where, observed_commit=commit, observed_dirty=dirty)


def _observe(directory: Path) -> Tuple[Optional[str], Optional[bool]]:
    status, output = run_git(
        directory, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", ok=(0, 1, 128)
    )
    commit = output.decode("ascii", "replace").strip()
    if status or not _SHA.fullmatch(commit):
        return None, None
    _, output = run_git(directory, "rev-parse", "--show-toplevel")
    top = Path(os.fsdecode(output).rstrip("\n"))
    _, untracked = run_git(
        directory, "ls-files", "--others", "--exclude-standard", "--directory", "-z", "--", "."
    )
    return commit, bool(untracked) or _dirty(top, _tree(top, commit), _changed(directory, commit))


def _lfs_object_present(common: Path, oid: str, size: int) -> bool:
    location = common / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    try:
        info = location.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == size


def _submodule_warning(paths: Sequence[str]) -> CodeWarning:
    return CodeWarning(
        "submodule_not_checked_out",
        "submodules are never checked out in the container; their directories stay empty",
        _bounded(paths),
    )


# Working-tree state


def _no_filters(repo: Path) -> Dict[str, str]:
    """Blank every configured filter driver; hashing a stale file would run its clean command.

    git treats an empty clean or process command as no filter, and ``required=false`` keeps it
    from failing for want of one.
    """
    _, output = run_git(
        repo, "config", "--null", "--name-only", "--get-regexp", r"^filter\.", ok=(0, 1)
    )
    names = (os.fsdecode(name).removeprefix("filter.") for name in output.split(b"\0") if name)
    drivers = sorted({name.rpartition(".")[0] for name in names if "." in name})
    blank = {"clean": "", "process": "", "required": "false"}
    return {f"filter.{driver}.{key}": value for driver in drivers for key, value in blank.items()}


def _changed(directory: Path, commit: str) -> List[str]:
    """Paths at or below ``directory`` that git sees differ from ``commit``.

    Paths are relative to the top level. No filter runs, so a smudged file differs from its
    blob.
    """
    _, output = run_git(
        directory,
        "diff",
        "--name-only",
        "-z",
        "--no-relative",
        "--no-renames",
        "--no-ext-diff",
        "--ignore-submodules",
        commit,
        "--",
        ".",
        config=_no_filters(directory),
    )
    return [raw.decode("utf-8", "surrogateescape") for raw in output.split(b"\0") if raw]


def _dirty(
    root: Path, tree: Sequence[Tuple[str, str, str, Optional[int]]], changed: Sequence[str]
) -> bool:
    """Whether any changed path really differs from its entry in ``tree``.

    No filter ran: the user's config, where ``git lfs install`` puts the LFS filter, is hidden
    and the repository's drivers are blanked. So a smudged LFS file differs from its pointer
    blob; it is unchanged when its size and SHA-256 match the pointer. A file under any other
    filter reads as dirty while its index data is stale.
    """
    small = {
        path: oid
        for path, mode, oid, size in tree
        if mode in {"100644", "100755"} and size is not None and size < _LFS_POINTER_LIMIT
    }
    blobs = _read_blobs(root, (small[path] for path in changed if path in small))
    for path in changed:
        pointer = _lfs_pointer(blobs[small[path]]) if path in small else None
        if pointer is None or not _matches_pointer(root / path, *pointer):
            return True
    return False


def _matches_pointer(local: Path, oid: str, size: int) -> bool:
    try:
        handle, info = _open_regular(local)
    except OSError:
        return False
    with handle:
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == oid


# Context size


def harness_size(size: int) -> int:
    """Bytes the harness counts for a file of ``size`` bytes: ``len(base64) // 4 * 3``."""
    return (size + 2) // 3 * 3


def check_context_size(sizes: Mapping[str, int], limit: int = MAX_CONTEXT_SIZE) -> int:
    """Return the harness's count for regular files of these sizes, or raise above ``limit``.

    The harness rejects a context whose counted total is strictly greater than its limit
    (harness/determined/common/context.py); directories and symlinks count nothing.
    """
    total = sum(harness_size(size) for size in sizes.values())
    if total > limit:
        largest = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:10]
        raise CodeError(
            f"the context counts {total:,} bytes, over the harness limit of {limit:,}",
            code="context_too_large",
            details={
                "total": total,
                "limit": limit,
                "largest": [{"path": path, "bytes": size} for path, size in largest],
                "hint": "use code.source git, or keep large files on shared storage and read "
                "them from there",
            },
        )
    return total


# Rules


def _pattern_matches(pattern: str, path: str, directory: bool = False) -> bool:
    """Match an rsync-style pattern component-wise against a relative path.

    A trailing ``/`` matches directories only, a leading ``/`` anchors the pattern at the
    repository root, and otherwise the pattern may match at any depth. Each component uses
    case-sensitive ``fnmatch`` rules. With ``directory`` the path itself names a directory, so
    its last component may match a trailing-``/`` pattern too.
    """
    parts = path.split("/")
    wanted = pattern.strip("/").split("/")
    limit = len(parts) - 1 if pattern.endswith("/") and not directory else len(parts)
    starts = [0] if pattern.startswith("/") else range(limit - len(wanted) + 1)
    return any(
        start + len(wanted) <= limit
        and all(fnmatchcase(part, want) for part, want in zip(parts[start:], wanted))
        for start in starts
    )


def _hard_secret_rule(
    path: str, secrets_file: Optional[str], directory: bool = False
) -> Optional[str]:
    # Folding keeps an include of .SSH on a case-insensitive filesystem from reading .ssh.
    folded = path.casefold()
    if secrets_file is not None and folded == secrets_file.casefold():
        return "configured secrets file"
    return next(
        (rule for rule in _HARD_SECRET_RULES if _pattern_matches(rule, folded, directory)), None
    )


def _soft_secret_rule(path: str, directory: bool = False) -> Optional[str]:
    """Return the name heuristic a path matches; check hard rules first."""
    folded = path.casefold()
    for pattern in _SOFT_SECRET_RULES:
        if _pattern_matches(pattern, folded, directory):
            return pattern
    for part in folded.split("/"):
        if "credential" in part:
            return "*credential*"
        if "secret" in part:
            return "*secret*"
        if part in {"token", ".token"} or part.endswith(".token"):
            return "*.token"
    return None


def _cache_rule(path: str, directory: bool = False) -> Optional[str]:
    return next((rule for rule in _CACHE_RULES if _pattern_matches(rule, path, directory)), None)


def _string_list(value: Any, name: str, count: int, length: int) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or len(value) > count:
        raise CodeError(f"{name} must be a list of at most {count} strings", code=f"invalid_{name}")
    for item in value:
        if not isinstance(item, str) or not item or len(item) > length or not item.isprintable():
            raise CodeError(
                f"{name} entries must be printable strings of 1 to {length} characters",
                code=f"invalid_{name}",
            )
    return list(value)


def _include_path(value: str) -> str:
    """Normalize an include to a path relative to the tree; ``.`` names the whole tree."""
    parts = PurePosixPath(value).parts
    if value.startswith("/") or ".." in parts:
        raise CodeError(
            f"include {value!r} must be a relative path inside the tree", code="invalid_include"
        )
    if ".git" in parts:
        raise CodeError(f"include {value!r} reaches into .git", code="invalid_include")
    return "/".join(part for part in parts if part != ".") or "."


# The harness validates every archive member before extracting any
# (harness/determined/common/tarfile_utils.py), under both POSIX and Windows rules, and one bad
# member fails the whole task at init.


def _harness_absolute(path: str) -> bool:
    return bool(ntpath.splitdrive(path)[0]) or path.startswith(("/", "\\"))


def _harness_escapes(path: str) -> bool:
    posix, windows = posixpath.normpath(path), ntpath.normpath(path)
    return posix == ".." or posix.startswith("../") or windows == ".." or windows.startswith("..\\")


def _unsafe_symlink(path: str, target: str, why: str) -> CodeError:
    return CodeError(
        f"symlink {path} -> {target} {why}; the harness would reject the whole context. Only "
        "relative links that stay inside the tree are allowed.",
        code="unsafe_symlink",
        details={"path": path, "target": target},
    )


def _check_symlink(path: str, target: str) -> None:
    """Reject a target that leaves the tree on its own; chains are checked afterwards."""
    if not target or "\0" in target or _harness_absolute(target):
        raise _unsafe_symlink(path, target, "is absolute or empty")
    joined = (
        posixpath.join(posixpath.dirname(path), target),
        ntpath.join(ntpath.dirname(path), target),
    )
    if any(_harness_escapes(item) for item in joined):
        raise _unsafe_symlink(path, target, "points outside the tree")


def _check_symlink_chains(symlinks: Mapping[str, str]) -> None:
    """Resolve every link through the tree's own links and require it to stay inside.

    A component naming another link is replaced by that link's target, relative to the
    directory being walked, as path lookup does. Any other component is applied lexically,
    which is at least as strict as a lookup. The caller guarantees that no link is also a
    parent directory of another entry, so each link's own directory is a real directory.
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
                    raise _unsafe_symlink(path, symlinks[path], "resolves outside the tree")
                directory.pop()
                continue
            target = symlinks.get("/".join([*directory, part]))
            if target is None:
                directory.append(part)
                continue
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                raise _unsafe_symlink(
                    path, symlinks[path], f"does not resolve within {_MAX_SYMLINK_HOPS} links"
                )
            pending.extend(reversed(target.split("/")))


# Context


@dataclass
class _Entry:
    type: int
    executable: bool = False
    size: int = 0
    oid: Optional[str] = None  # a tracked blob
    local: Optional[Path] = None  # a working-tree file, with its identity when walked
    dev: int = 0
    ino: int = 0
    target: str = ""  # a symlink


class _Context:
    """Collects the entries of one context, with every rule decision recorded."""

    def __init__(
        self,
        root: Path,
        patterns: Sequence[str],
        secrets_file: Optional[str],
        secrets_identity: Optional[Tuple[int, int]],
        limit: int,
    ) -> None:
        self.root = root
        self.patterns = patterns
        self.secrets_file = secrets_file
        self.secrets_identity = secrets_identity
        self.limit = limit
        # Harness bytes of working-tree files alone; tracked files may still be replaced by
        # smaller working-tree copies, so only this part can end the walk early.
        self.local_total = 0
        self.entries: Dict[str, _Entry] = {}
        self.dirs: Set[str] = set()
        self.included: Set[str] = set()
        self.soft_included: Set[str] = set()
        self.excluded: Dict[str, Dict[str, str]] = {}
        self.skipped: Dict[str, Dict[str, str]] = {}
        self.submodules: List[str] = []

    def rule(self, path: str, below: str, directory: bool) -> Optional[Tuple[str, str]]:
        """Return (reason, rule) for an entry the rules drop.

        Secret rules and exclude patterns see the path in the tree; cache rules see only the
        part below an include root, so naming a cache-like directory restores it.
        """
        hard = _hard_secret_rule(path, self.secrets_file, directory)
        if hard is not None:
            return "secret", hard
        soft = _soft_secret_rule(path, directory)
        if soft is not None:
            return "secret_like", soft
        cache = _cache_rule(below, directory)
        if cache is not None:
            return "cache", cache
        pattern = next((p for p in self.patterns if _pattern_matches(p, path, directory)), None)
        return ("exclude_pattern", pattern) if pattern is not None else None

    def exclude(self, path: str, reason: str, rule: str, directory: bool = False) -> None:
        key = f"{path}/" if directory else path
        self.excluded.setdefault(key, {"path": key, "reason": reason, "rule": rule})

    def skip(self, path: str, reason: str) -> None:
        self.skipped.setdefault(path, {"path": path, "reason": reason})

    def add_tracked(self, entries: Iterable[Tuple[str, str, str, Optional[int]]]) -> None:
        for path, mode, oid, size in entries:
            if mode == "160000":
                self.skip(path, "submodule")
                self.submodules.append(path)
                continue
            dropped = self.rule(path, path, False)
            if dropped is not None:
                self.exclude(path, *dropped)
            elif mode == "120000":
                self.entries[path] = _Entry(SYMTYPE, oid=oid)
            elif mode in {"100644", "100755"}:
                self.entries[path] = _Entry(
                    REGTYPE, executable=mode == "100755", size=size or 0, oid=oid
                )
            else:
                self.skip(path, f"unsupported_mode_{mode}")

    def add_include(self, value: str) -> None:
        relative = _include_path(value)
        local = self.root if relative == "." else self.root / relative
        # A symlinked parent would let the include read outside the tree.
        if relative != "." and os.path.realpath(local.parent) != str(local.parent):
            raise CodeError(f"include {value!r} traverses a symlink", code="invalid_include")
        try:
            info = os.lstat(local)
        except OSError as exc:
            raise CodeError(f"include {value!r} does not exist", code="invalid_include") from exc
        if stat.S_ISDIR(info.st_mode):
            hard = _hard_secret_rule(relative, self.secrets_file, True)
            if hard is not None:
                self.exclude(relative, "secret", hard, directory=True)
                return
            if relative != ".":
                self.dirs.add(relative)
            self._walk(local)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            # A named file overrides soft, cache and exclude rules, never hard ones.
            hard = _hard_secret_rule(relative, self.secrets_file)
            if hard is not None:
                self.exclude(relative, "secret", hard)
                return
            if _soft_secret_rule(relative) is not None:
                self.soft_included.add(relative)
            self._add_local(relative, local, info)
        else:
            raise CodeError(
                f"include {value!r} is not a file, directory or symlink", code="invalid_include"
            )

    def _walk(self, top: Path) -> None:
        def failed(error: OSError) -> None:
            raise CodeError(
                f"cannot read {error.filename}: {error.strerror}", code="invalid_include"
            )

        for directory, dirnames, filenames in os.walk(top, onerror=failed):
            kept = []
            names = [(name, True) for name in dirnames] + [(name, False) for name in filenames]
            for name, is_dir in sorted(names):
                local = Path(directory, name)
                relative = local.relative_to(self.root).as_posix()
                if name == ".git":  # a repository, linked worktree or submodule checkout
                    self.skip(relative, "git_metadata")
                    continue
                dropped = self.rule(relative, local.relative_to(top).as_posix(), is_dir)
                if dropped is not None:
                    self.exclude(relative, *dropped, directory=is_dir)
                    continue
                info = os.lstat(local)
                if stat.S_ISDIR(info.st_mode):
                    self.dirs.add(relative)
                    kept.append(name)
                elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    self._add_local(relative, local, info)
                else:
                    self.skip(relative, "special_file")
            dirnames[:] = kept

    def _add_local(self, relative: str, local: Path, info: os.stat_result) -> None:
        if stat.S_ISLNK(info.st_mode):
            entry = _Entry(SYMTYPE, target=os.readlink(local))
        elif (info.st_dev, info.st_ino) == self.secrets_identity:
            # Another name for the configured secrets file: a hard link, or a case variant
            # on a case-insensitive filesystem. A tracked copy at this path goes too.
            self.entries.pop(relative, None)
            self.soft_included.discard(relative)
            self.excluded.pop(relative, None)
            self.exclude(relative, "secret", "configured secrets file")
            return
        else:
            if relative != PROVENANCE_PATH and relative not in self.included:
                self._count(harness_size(info.st_size))
            entry = _Entry(
                REGTYPE,
                executable=bool(info.st_mode & stat.S_IXUSR),
                size=info.st_size,
                local=local,
                dev=info.st_dev,
                ino=info.st_ino,
            )
        self.entries[relative] = entry
        self.excluded.pop(relative, None)
        self.included.add(relative)

    def _count(self, size: int) -> None:
        """Stop as soon as working-tree files alone exceed the limit, as the harness would."""
        self.local_total += size
        if self.local_total > self.limit:
            raise CodeError(
                f"the included working-tree files alone count over {self.limit:,} bytes",
                code="context_too_large",
                details={
                    "total": self.local_total,
                    "limit": self.limit,
                    "hint": "use code.source git, or keep large files on shared storage and "
                    "read them from there",
                },
            )


def plan_context(
    repo: PathLike,
    revision: str = "HEAD",
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    *,
    secrets_file: Optional[PathLike] = None,
    limit: int = MAX_CONTEXT_SIZE,
) -> ContextCode:
    """Build a ``context`` source: tracked files at ``revision`` plus working-tree includes.

    Every path a rule drops is listed in ``excluded``. The payload is deterministic: an
    unchanged tree yields identical file entries and the same ``content_digest``.
    """
    revision = _revision(revision)
    # Normalized duplicates would walk the same tree again. Nested includes stay: naming a
    # directory restores what cache rules dropped below its parent.
    includes: Dict[str, str] = {}
    for value in _string_list(include, "include", _MAX_INCLUDES, _MAX_INCLUDE_LENGTH):
        includes.setdefault(_include_path(value), value)  # fail before any git runs
    patterns = sorted(set(_string_list(exclude, "exclude", _MAX_EXCLUDES, _MAX_EXCLUDE_LENGTH)))
    root, _common = _repository(repo, work_tree=True)
    commit = resolve_commit(root, revision)
    secrets = Path(secrets_file) if secrets_file is not None else default_secrets_path()
    secrets = secrets.expanduser()
    try:
        secrets_relative: Optional[str] = (
            Path(os.path.realpath(secrets)).relative_to(root).as_posix()
        )
    except ValueError:
        secrets_relative = None
    # The file itself, wherever it lies: a hard link in the tree is another name for it.
    try:
        found = os.stat(secrets)
        secrets_identity: Optional[Tuple[int, int]] = (found.st_dev, found.st_ino)
    except OSError:
        secrets_identity = None

    context = _Context(root, patterns, secrets_relative, secrets_identity, limit)
    tree = _tree(root, commit)
    context.add_tracked(tree)
    for value in includes.values():
        context.add_include(value)
    entries = context.entries
    if PROVENANCE_PATH in entries:
        del entries[PROVENANCE_PATH]
        context.included.discard(PROVENANCE_PATH)
        context.skip(PROVENANCE_PATH, "reserved")

    dirs = _check_paths(context)
    dirty = _dirty(root, tree, _changed(root, commit))
    included = sorted(context.included)
    excluded = [context.excluded[key] for key in sorted(context.excluded)]
    skipped = [context.skipped[key] for key in sorted(context.skipped)]
    provenance = {
        "commit": commit,
        "dirty": dirty,
        "included": included,
        "excluded": excluded,
        "skipped": skipped,
    }
    provenance_bytes = (
        json.dumps(provenance, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    # Fail on size before any content is read, then again on the bytes actually read, in case a
    # working-tree file grew in between.
    sizes = {path: entry.size for path, entry in entries.items() if entry.type == REGTYPE}
    sizes[PROVENANCE_PATH] = len(provenance_bytes)
    check_context_size(sizes, limit)
    contents = _read_contents(root, entries)
    contents[PROVENANCE_PATH] = provenance_bytes
    size = check_context_size({path: len(contents[path]) for path in sizes}, limit)

    files: List[Dict[str, Any]] = []
    manifest: List[Dict[str, str]] = []
    for path in sorted(set(contents) | dirs):
        entry = entries.get(path)
        data = contents.get(path, b"")
        if path in dirs:
            kind, mode = DIRTYPE, _DIR_MODE
        elif entry is None or entry.type == REGTYPE:
            kind, mode = REGTYPE, _EXEC_MODE if entry and entry.executable else _FILE_MODE
        else:
            kind, mode = SYMTYPE, _LINK_MODE
        # The fields and encodings of bindings.v1File.to_json; ownership is dropped on
        # extraction anyway.
        files.append(
            {
                "path": path,
                "type": kind,
                "content": base64.b64encode(data).decode("ascii"),
                "mtime": str(CONTEXT_MTIME),
                "mode": mode,
                "uid": 0,
                "gid": 0,
            }
        )
        manifest.append(
            {
                "path": path,
                "type": _TYPE_NAMES[kind],
                "mode": f"{mode:04o}",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    pointers = [path for path in sizes if _lfs_pointer(contents[path]) is not None]
    return ContextCode(
        repo=str(root),
        commit=commit,
        dirty=dirty,
        files=tuple(files),
        manifest=tuple(manifest),
        content_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        size=size,
        included=tuple(included),
        excluded=tuple(excluded),
        skipped=tuple(skipped),
        warnings=_context_warnings(context, pointers),
    )


def _check_paths(context: _Context) -> Set[str]:
    """Validate every path of the context and return its directories."""
    paths = set(context.entries) | {PROVENANCE_PATH}
    parents = {
        "/".join(parts[:index])
        for parts in (path.split("/") for path in paths | context.dirs)
        for index in range(1, len(parts))
    }
    conflicts = sorted(paths & (parents | context.dirs))
    if conflicts:
        raise CodeError(
            f"context paths are both files and directories: {', '.join(conflicts[:5])}",
            code="invalid_include",
        )
    dirs = parents | context.dirs
    for path in sorted(paths | dirs):
        if _harness_absolute(path) or _harness_escapes(path):
            raise CodeError(
                f"path {path!r} would make the harness reject the whole context",
                code="unsafe_path",
            )
    # Paths are JSON strings in the request and in the provenance.
    for path in (*paths, *dirs, *context.excluded, *context.skipped):
        try:
            path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CodeError(f"path {path!r} is not valid UTF-8", code="invalid_path") from exc
    return dirs


def _open_regular(path: Path) -> Tuple[BinaryIO, os.stat_result]:
    """Open a file without following a final symlink or blocking on a FIFO."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    handle = os.fdopen(os.open(path, flags), "rb")
    return handle, os.fstat(handle.fileno())


def _read_local(path: str, local: Path, entry: _Entry) -> bytes:
    """Read a working-tree file only if it is still the regular file the walk recorded.

    Comparing the inode also catches a parent directory swapped for a symlink, which
    ``O_NOFOLLOW`` alone misses; a file that grew costs one byte past its size.
    """
    try:
        handle, info = _open_regular(local)
    except OSError as exc:
        raise CodeError(f"{path} changed during planning", code="invalid_include") from exc
    with handle:
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (entry.dev, entry.ino):
            raise CodeError(f"{path} changed during planning", code="invalid_include")
        data = handle.read(entry.size + 1)
    if len(data) != entry.size:
        raise CodeError(f"{path} changed during planning", code="invalid_include")
    return data


def _read_contents(root: Path, entries: Mapping[str, _Entry]) -> Dict[str, bytes]:
    """Read file contents and symlink targets, and check that every link stays inside."""
    blobs = _read_blobs(root, (entry.oid for entry in entries.values() if entry.oid))
    contents: Dict[str, bytes] = {}
    symlinks: Dict[str, str] = {}
    for path, entry in entries.items():
        if entry.oid is not None:
            data = blobs[entry.oid]
        elif entry.local is not None:
            data = _read_local(path, entry.local, entry)
        else:
            data = entry.target.encode("utf-8", "surrogateescape")
        if entry.type == SYMTYPE:
            try:
                symlinks[path] = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodeError(
                    f"symlink {path} has a non-UTF-8 target", code="invalid_path"
                ) from exc
            _check_symlink(path, symlinks[path])
        contents[path] = data
    _check_symlink_chains(symlinks)
    return contents


def _context_warnings(context: _Context, pointers: Sequence[str]) -> Tuple[CodeWarning, ...]:
    warnings = []
    if pointers:
        warnings.append(
            CodeWarning(
                "lfs_pointer",
                "these files are Git LFS pointers, not the large files they stand for",
                _bounded(pointers),
            )
        )
    if "startup-hook.sh" in context.entries:
        warnings.append(
            CodeWarning(
                "startup_hook",
                "Determined sources a root startup-hook.sh before the command runs",
                ("startup-hook.sh",),
            )
        )
    if context.soft_included:
        warnings.append(
            CodeWarning(
                "secret_like_included",
                "an include named these files although their names look secret; confirm they "
                "hold no secret, since anyone who can read the job can read its context",
                _bounded(context.soft_included),
            )
        )
    if context.submodules:
        warnings.append(_submodule_warning(context.submodules))
    return tuple(warnings)
