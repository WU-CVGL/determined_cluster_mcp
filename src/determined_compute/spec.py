"""Typed task specification and the code prelude rendered before every command.

Commands run under ``bash -lc`` and experiment entrypoints under ``sh -c``, so the
rendered text is POSIX sh with one shape for every code source::

    <prelude> || exit $?
    <command>

The prelude is a single ``&&`` list, so a failure anywhere in it exits with that status
before the shell reads the first user statement, whatever the command's form. git delivery is
one subshell in that list, so what it changes to isolate git never reaches the command.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import unicodedata
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CodeSource = Literal["git", "context", "path"]

# Where context code lives in the container: Determined extracts it here before the startup
# hooks.
CODE_ROOT = "/run/determined/workdir"
# Where the git prelude clones. git clone needs an empty target, and the workdir may be the
# task user's HOME and the startup hooks' working directory, so a hook can write there first.
# Determined creates /run/determined for the task user (master/pkg/tasks/task.go,
# workDirArchive) and puts nothing at this path.
GIT_CODE_ROOT = "/run/determined/code"

# A missing git-lfs must fail the checkout instead of leaving pointer files behind, and
# neither GIT_LFS_SKIP_SMUDGE nor fetch filters from the image, env or .lfsconfig may skip a
# download.
_LFS_ENV = "GIT_LFS_SKIP_SMUDGE=0 "
_LFS_OPTIONS = (
    "-c filter.lfs.process='git-lfs filter-process' -c filter.lfs.required=true"
    " -c lfs.fetchinclude= -c lfs.fetchexclude="
)
# git takes its repository, work tree, index, object store, exec path and configuration from
# GIT_* variables, which TaskSpec.env, the image or a startup hook may set for the workload;
# with them, a clone and checkout can succeed while the code lands elsewhere. Delivery runs in
# a subshell that unsets every exported GIT_* name and leaves the workload's environment alone.
# POSIX sh cannot list exported names, so they come from `env`. sed emits only names made of
# [A-Za-z0-9_], so the text is safe to eval, and a value spanning lines can at worst unset a
# name that is not set. The probe proves that env and sed ran, since without them nothing
# would be unset.
_UNSET_GIT = (
    "export GIT_COMPUTE_UNSET=1"
    " && eval \"$(env | sed -n 's/^\\(GIT_[A-Za-z0-9_]*\\)=.*/unset \\1 \\&\\&/p') :\""
    ' && test -z "${GIT_COMPUTE_UNSET+x}"'
)
# GIT_CONFIG_GLOBAL needs git 2.32, which an image may lack. Nothing can create a path below
# /dev/null, and git reads a config file there as missing, so no user config applies either.
_NO_GIT_CONFIG = "export HOME=/dev/null/home XDG_CONFIG_HOME=/dev/null/home GIT_CONFIG_NOSYSTEM=1"
# One line on stderr names the failed step; the subshell's status fails the prelude.
_GIT_FAIL = "fail() { printf '%s\\n' \"compute: git code delivery failed: $1\" >&2; exit 1; }"
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The harness rewrites an entrypoint of this form into a trial-class launch before running
# it, so any prefix breaks it (harness/determined/util.py, match_legacy_trial_class).
_LEGACY_TRIAL_CLASS = re.compile(r"[a-zA-Z0-9_.]+:[a-zA-Z0-9_]+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Provenance variables are set by the MCP; the prefix is reserved so they cannot be forged.
_RESERVED_ENV_PREFIX = "COMPUTE_"
_NAME_MAX_LENGTH = 128


def _no_nul(value: str) -> str:
    if "\0" in value:
        raise ValueError("must not contain NUL")
    return value


def container_path(value: str) -> str:
    """Normalize an absolute container path, rejecting relative paths and '..'."""

    _no_nul(value)
    if not value.startswith("/"):
        raise ValueError("must be an absolute container path")
    # Split by hand: posixpath.normpath keeps a leading '//' and resolves '..' lexically.
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError("must not contain '..'")
    return "/" + "/".join(parts)


def normalize_workdir(value: str) -> str:
    """Normalize a path relative to the code root; '.' is the root itself."""

    _no_nul(value)
    if not value:
        raise ValueError("must not be empty; use '.' for the code root")
    if value.startswith("/"):
        raise ValueError("must be relative to the code root")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError("must not contain '..'")
    return "/".join(parts) or "."


def _local_path(value: str) -> str:
    _no_nul(value)
    # A relative path would resolve against the server's directory, not the caller's.
    if not os.path.isabs(value):
        raise ValueError("must be an absolute local path")
    return value


def _relative_pattern(value: str) -> str:
    _no_nul(value)
    if not value or value.startswith("/"):
        raise ValueError("must be a non-empty path relative to the repository root")
    if ".." in value.split("/"):
        raise ValueError("must not contain '..'")
    return value


def _exclude_pattern(value: str) -> str:
    # Unlike an include, one leading '/' is allowed: it anchors the pattern at the repository
    # root (code._pattern_matches), the only way to drop a top-level directory alone.
    _no_nul(value)
    if not value.strip("/") or value.startswith("//"):
        raise ValueError(
            "must be a non-empty pattern relative to the repository root; "
            "one leading '/' anchors it there"
        )
    if ".." in value.split("/"):
        raise ValueError("must not contain '..'")
    return value


def _revision(value: str) -> str:
    if not value or value.startswith("-"):
        raise ValueError("must be a non-empty revision that does not start with '-'")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("must not contain control characters")
    return value


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise ValueError("must not contain control characters")
    return value


def _name(value: str) -> str:
    value = _text(value)
    if len(value) > _NAME_MAX_LENGTH:
        raise ValueError(f"must be at most {_NAME_MAX_LENGTH} characters")
    return value


def _command(value: str) -> str:
    _no_nul(value)
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _env_name(value: str) -> str:
    if not _ENV_NAME.fullmatch(value):
        raise ValueError("must be a shell variable name")
    if value.startswith(_RESERVED_ENV_PREFIX):
        raise ValueError(f"the {_RESERVED_ENV_PREFIX} prefix is reserved for code provenance")
    return value


ContainerPath = Annotated[StrictStr, AfterValidator(container_path)]
Workdir = Annotated[StrictStr, AfterValidator(normalize_workdir)]
LocalPath = Annotated[StrictStr, AfterValidator(_local_path)]
RelativePattern = Annotated[StrictStr, AfterValidator(_relative_pattern)]
ExcludePattern = Annotated[StrictStr, AfterValidator(_exclude_pattern)]
Revision = Annotated[StrictStr, AfterValidator(_revision)]
Text = Annotated[StrictStr, AfterValidator(_text)]
Name = Annotated[StrictStr, AfterValidator(_name)]
Command = Annotated[StrictStr, AfterValidator(_command)]
EnvName = Annotated[StrictStr, AfterValidator(_env_name)]
EnvValue = Annotated[StrictStr, AfterValidator(_no_nul)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitCode(_Model):
    """A repository on shared storage, cloned in the container at a pinned commit."""

    source: Literal["git"]
    repo: ContainerPath
    revision: Revision = "HEAD"


class ContextCode(_Model):
    """A local working tree, uploaded as the task context."""

    source: Literal["context"]
    repo: LocalPath
    revision: Revision = "HEAD"
    include: List[RelativePattern] = Field(default_factory=list)
    exclude: List[ExcludePattern] = Field(default_factory=list)


class PathCode(_Model):
    """A directory on shared storage, run in place and never pinned."""

    source: Literal["path"]
    dir: ContainerPath


Code = Annotated[Union[GitCode, ContextCode, PathCode], Field(discriminator="source")]


def _check_experiment(config: Mapping[str, Any]) -> None:
    if "entrypoint" in config:
        raise ValueError("entrypoint is rendered from command; set command instead")
    if "bind_mounts" in config:
        raise ValueError("bind_mounts is not allowed; the administrator mounts every bind source")
    storage = config.get("checkpoint_storage")
    if storage is not None:
        if not isinstance(storage, Mapping):
            raise ValueError("checkpoint_storage must be an object")
        for key in ("host_path", "container_path"):
            if key in storage:
                raise ValueError(
                    f"checkpoint_storage.{key} is not allowed; the workspace or master "
                    "default supplies the storage root"
                )
        for key in ("checkpoint_path", "tensorboard_path"):
            if key in storage:
                raise ValueError(f"checkpoint_storage.{key} is a legacy field; use storage_path")
        storage_path = storage.get("storage_path")
        if storage_path is not None:
            if (
                not isinstance(storage_path, str)
                or not storage_path
                or storage_path.startswith("/")
                or ".." in storage_path.split("/")
            ):
                raise ValueError("checkpoint_storage.storage_path must be relative without '..'")
    searcher = config.get("searcher")
    if searcher is not None:
        if not isinstance(searcher, Mapping):
            raise ValueError("searcher must be an object")
        name = searcher.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("searcher.name is required")
        limit = searcher.get("max_concurrent_trials")
        # Zero means unlimited to the master, which would leave slots times concurrency open.
        if name != "single" and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError(
                "a search must set searcher.max_concurrent_trials to a positive integer"
            )


class TaskSpec(_Model):
    """A command, shell, or experiment request, published as the plan tool's input schema."""

    kind: Literal["command", "shell", "experiment"]
    name: Name
    command: Optional[Command] = None
    code: Optional[Code] = None
    workdir: Workdir = "."
    output_dir: Optional[ContainerPath] = None
    admission: Literal["queue", "immediate"] = "queue"
    image: Optional[Text] = None
    pool: Optional[Text] = None
    slots: Optional[Annotated[StrictInt, Field(ge=0)]] = None
    env: Dict[EnvName, EnvValue] = Field(default_factory=dict)
    workspace: Optional[Text] = None
    project: Optional[Text] = None
    experiment: Optional[Dict[str, Any]] = None

    @field_validator("experiment")
    @classmethod
    def _experiment_rules(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if value is not None:
            _check_experiment(value)
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> "TaskSpec":
        if self.kind == "shell":
            # A shell runs sshd, so there is no command to prefix with a prelude.
            if self.command is not None:
                raise ValueError("a shell has no command")
            if self.code is not None and self.code.source == "git":
                raise ValueError("a shell accepts only context or path code")
            if self.output_dir is not None:
                raise ValueError("a shell has no prelude to create output_dir")
            if self.workdir != ".":
                raise ValueError("a shell has no prelude to enter workdir")
        else:
            if self.command is None:
                raise ValueError(f"kind {self.kind} requires command")
            if self.output_dir is None:
                raise ValueError(f"kind {self.kind} requires output_dir")
        if self.code is None and self.workdir != ".":
            raise ValueError("workdir is relative to the code root and requires code")
        if self.kind == "experiment":
            if _LEGACY_TRIAL_CLASS.fullmatch((self.command or "").strip()):
                raise ValueError(
                    "a legacy 'module:Class' entrypoint is not supported; "
                    "launch the trial with a command"
                )
        elif self.experiment is not None:
            raise ValueError("experiment applies only to kind experiment")
        return self


def _full_sha(commit: Optional[str], source: str) -> str:
    if commit is None or not _FULL_SHA.fullmatch(commit):
        raise ValueError(f"{source} code needs its commit pinned to a full SHA")
    return commit


def _enter(root: str, workdir: str) -> str:
    """Enter ``workdir`` below ``root`` and require it to resolve inside ``root``.

    ``workdir`` is checked only lexically, and a symlink along it, committed or on shared
    storage, can lead out of the delivered tree. So the physical directory is compared with
    the resolved root, which a path ``dir`` may itself reach through a symlink. The verdict
    comes from one command substitution, so its variable never reaches the command, and a root
    that does not resolve fails it. For '.', ``pwd -P`` is the resolved root by construction.
    """

    workdir = normalize_workdir(workdir)
    if workdir == ".":
        return f"cd -- {shlex.quote(root)}"
    quoted = shlex.quote(root)
    # Stripping the slash of a root that resolves to / keeps the pattern from requiring two;
    # it is a separate step because bash mismatches "${r%/}" inside a case pattern.
    verdict = (
        f"r=$(cd -- {quoted} && pwd -P) && r=${{r%/}}"
        ' && case "$(pwd -P)/" in ("$r"/*) echo in;; esac'
    )
    message = "compute: the workdir resolves to %s, outside the code root\\n"
    return (
        f"cd -- {shlex.quote(posixpath.join(root, workdir))}"
        f' && {{ test "$({verdict})" = in'
        f" || {{ printf '{message}' \"$(pwd -P)\" >&2; false; }}; }}"
    )


def render_prelude(
    source: Optional[CodeSource],
    *,
    output_dir: str,
    location: Optional[str] = None,
    commit: Optional[str] = None,
    workdir: str = ".",
    uses_lfs: bool = False,
) -> str:
    """Return the statements that deliver code, create ``output_dir``, and enter ``workdir``.

    ``location`` is the container repository for git and the directory for path. A context
    ignores it: its repository is a local path that must not reach the task config.
    Submodules are never recursed. A failed git delivery, or a workdir whose physical path
    lies outside the code root, prints one ``compute:`` line to stderr.
    """

    if uses_lfs and source != "git":
        raise ValueError("uses_lfs applies only to git code")
    make_output = f"mkdir -p -- {shlex.quote(container_path(output_dir))}"
    if source is None:
        if commit is not None or normalize_workdir(workdir) != ".":
            raise ValueError("commit and workdir require code")
        return make_output
    if source == "path":
        if commit is not None:
            raise ValueError("path code is never pinned")
        return f"{make_output} && {_enter(container_path(location or ''), workdir)}"
    if source not in ("git", "context"):
        raise ValueError(f"unknown code source: {source!r}")
    commit = _full_sha(commit, source)
    if source == "context":
        return f"{make_output} && {_enter(CODE_ROOT, workdir)}"
    delivery = _git_delivery(container_path(location or ""), commit, uses_lfs)
    return f"{delivery} && {make_output} && {_enter(GIT_CODE_ROOT, workdir)}"


def _git_delivery(repo: str, commit: str, uses_lfs: bool) -> str:
    """Return the subshell that clones ``repo`` and checks out ``commit`` in isolation."""

    source = shlex.quote(repo)
    root = shlex.quote(GIT_CODE_ROOT)
    lfs_env, lfs = (_LFS_ENV, f" {_LFS_OPTIONS}") if uses_lfs else ("", "")

    def fail(message: str) -> str:
        return f"fail {shlex.quote(message)}"

    steps = [
        _GIT_FAIL,
        f"{_UNSET_GIT} || {fail('cannot unset the GIT_ variables; the image needs env and sed')}",
        _NO_GIT_CONFIG,
        f"git -c safe.directory={source} clone -q --shared --no-checkout -- {source} {root}"
        f" || {fail('cannot clone the repository')}",
        f"{lfs_env}git -C {root}{lfs} checkout -q --detach {commit}"
        f" || {fail(f'cannot check out {commit}')}",
        # The environment is clean by now; these also catch what it cannot reach, such as a
        # core.worktree that an image's clone template writes, and prove what the command runs.
        f'test "$(git -C {root} rev-parse --show-toplevel)" = "$(cd -- {root} && pwd -P)"'
        f" || {fail(f'the work tree is not {GIT_CODE_ROOT}')}",
        f'test "$(git -C {root} rev-parse HEAD)" = {commit} || {fail(f"HEAD is not {commit}")}',
    ]
    return f"( {'; '.join(steps)} )"


def render_entrypoint(prelude: str, command: str) -> str:
    """Join a prelude and a command so no user statement runs after a failed prelude."""

    return f"{prelude} || exit $?\n{command}"


def code_environment(
    source: Optional[CodeSource], *, location: Optional[str] = None, commit: Optional[str] = None
) -> Dict[str, str]:
    """Return the provenance variables for the task config; empty when there is no code."""

    if source is None:
        return {}
    if source == "path":
        if commit is not None:
            raise ValueError("path code is never pinned")
        root = container_path(location or "")
        return {"COMPUTE_CODE_SOURCE": "path", "COMPUTE_CODE_ROOT": root}
    if source not in ("git", "context"):
        raise ValueError(f"unknown code source: {source!r}")
    return {
        "COMPUTE_CODE_SOURCE": source,
        "COMPUTE_CODE_ROOT": GIT_CODE_ROOT if source == "git" else CODE_ROOT,
        "COMPUTE_CODE_COMMIT": _full_sha(commit, source),
    }
