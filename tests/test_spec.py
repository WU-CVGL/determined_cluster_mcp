"""Tests for the typed task specification and the code prelude."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml
from pydantic import ValidationError

from determined_compute import code, spec
from determined_compute.code import plan_context
from determined_compute.policy import Resources
from determined_compute.spec import (
    ContextCode,
    GitCode,
    PathCode,
    TaskSpec,
    code_environment,
    render_entrypoint,
    render_prelude,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "89abcdef" * 8
DROP = object()
BASE: Dict[str, Any] = {
    "kind": "command",
    "name": "train",
    "command": "python train.py",
    "output_dir": "/shared/out",
}
PATH_CODE = {"source": "path", "dir": "/shared/app"}
SEARCH = {"name": "random", "metric": "loss", "max_trials": 8, "max_concurrent_trials": 2}


def payload(**fields: Any) -> Dict[str, Any]:
    result = dict(BASE)
    for key, value in fields.items():
        if value is DROP:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def build(**fields: Any) -> TaskSpec:
    return TaskSpec.model_validate(payload(**fields))


def shell_fields(**fields: Any) -> Dict[str, Any]:
    return {"kind": "shell", "command": DROP, "output_dir": DROP, **fields}


def experiment_fields(**fields: Any) -> Dict[str, Any]:
    return {"kind": "experiment", **fields}


# Model validation


def test_command_defaults() -> None:
    task = build()

    assert task.workdir == "."
    assert task.admission == "queue"
    assert task.code is None
    assert task.env == {}
    assert task.slots is None and task.pool is None and task.image is None
    assert task.experiment is None


def test_code_sources_validate_and_normalize() -> None:
    git = build(code={"source": "git", "repo": "/shared//repos/app/"}, workdir="./src//pkg/")
    context = build(
        code={
            "source": "context",
            "repo": "/src/app",
            "revision": "v1.2",
            "include": ["data/small.csv"],
            "exclude": ["*.ckpt", "logs/"],
        }
    )
    path = build(code=PATH_CODE)

    assert git.code == GitCode(source="git", repo="/shared/repos/app", revision="HEAD")
    assert git.workdir == "src/pkg"
    assert context.code == ContextCode(
        source="context",
        repo="/src/app",
        revision="v1.2",
        include=["data/small.csv"],
        exclude=["*.ckpt", "logs/"],
    )
    assert path.code == PathCode(source="path", dir="/shared/app")
    assert build(code={"source": "context", "repo": "/src/app"}).code.revision == "HEAD"


def test_a_leading_slash_anchors_an_exclude_but_not_an_include() -> None:
    code = ContextCode(source="context", repo="/src/app", exclude=["/data/", "/a/*.ckpt"])

    assert code.exclude == ["/data/", "/a/*.ckpt"]
    with pytest.raises(ValidationError, match="relative to the repository root"):
        ContextCode(source="context", repo="/src/app", include=["/data/"])


@pytest.mark.parametrize(
    "workdir, expected",
    [(".", "."), ("./", "."), ("src", "src"), ("./src/./pkg//", "src/pkg")],
)
def test_workdir_is_normalized(workdir: str, expected: str) -> None:
    assert build(code=PATH_CODE, workdir=workdir).workdir == expected


@pytest.mark.parametrize(
    "code", [None, {"source": "context", "repo": "/src/app"}, PATH_CODE], ids=str
)
def test_shell_accepts_context_and_path_code(code: Optional[Dict[str, Any]]) -> None:
    fields = shell_fields() if code is None else shell_fields(code=code)
    task = build(**fields)

    assert task.kind == "shell"
    assert task.command is None and task.output_dir is None


def test_resources_env_and_placement_fields() -> None:
    task = build(
        image="registry.example/train:1",
        pool="gpu",
        slots=0,
        env={"WANDB_PROJECT": "demo", "_EMPTY": ""},
        workspace="research",
    )
    experiment = build(**experiment_fields(workspace="research", project="baselines"))

    assert task.slots == 0
    assert task.env == {"WANDB_PROJECT": "demo", "_EMPTY": ""}
    assert (task.image, task.pool, task.workspace, task.project) == (
        "registry.example/train:1",
        "gpu",
        "research",
        None,
    )
    assert (experiment.workspace, experiment.project) == ("research", "baselines")


@pytest.mark.parametrize("kind", ["command", "experiment"])
def test_immediate_admission_is_passed_through(kind: str) -> None:
    # The master owns the experiment rejection; the model does not duplicate it.
    assert build(kind=kind, admission="immediate").admission == "immediate"


def test_legacy_looking_command_is_fine_outside_experiments() -> None:
    assert build(command="model_def:Trial").command == "model_def:Trial"


@pytest.mark.parametrize(
    "experiment",
    [
        {"searcher": {"name": "single", "metric": "loss", "max_length": 10}},
        {"searcher": SEARCH, "hyperparameters": {"lr": {"type": "log", "base": 10}}},
        {"checkpoint_storage": {"storage_path": "runs/a"}},
        {"checkpoint_storage": {"type": "shared_fs", "storage_path": ".", "save_trial_best": 1}},
        {"checkpoint_storage": None, "searcher": None, "max_restarts": 0},
        {
            "resources": {"max_slots": 4, "priority": 10},
            "environment": {"force_pull_image": True},
        },
        {"resources": None, "environment": None},
    ],
    ids=[
        "single",
        "search",
        "storage-path",
        "shared-fs",
        "nulls",
        "other-settings",
        "null-sections",
    ],
)
def test_experiment_accepts_valid_config(experiment: Dict[str, Any]) -> None:
    task = build(**experiment_fields(experiment=experiment))

    assert task.experiment == experiment


def test_json_schema_publishes_the_code_union() -> None:
    schema = TaskSpec.model_json_schema()

    (code,) = [item for item in schema["properties"]["code"]["anyOf"] if "discriminator" in item]
    assert set(code["discriminator"]["mapping"]) == {"git", "context", "path"}
    assert "accelerators" not in schema["properties"]
    assert schema["additionalProperties"] is False


INVALID = [
    # Unknown fields and basic types.
    ({"accelerators": {"model": "a100"}}, "Extra inputs are not permitted"),
    ({"kind": "trial"}, "Input should be 'command', 'shell' or 'experiment'"),
    ({"name": DROP}, "Field required"),
    ({"name": "   "}, "must not be empty"),
    ({"name": "a\nb"}, "control characters"),
    ({"name": "x" * 129}, "at most 128 characters"),
    ({"command": DROP}, "kind command requires command"),
    ({"command": " \n "}, "must not be empty"),
    ({"command": ["python", "train.py"]}, "Input should be a valid string"),
    ({"output_dir": DROP}, "kind command requires output_dir"),
    ({"output_dir": "out"}, "must be an absolute container path"),
    ({"output_dir": "/shared/../etc"}, "must not contain '..'"),
    ({"output_dir": "/shared/a\0b"}, "must not contain NUL"),
    ({"admission": "later"}, "Input should be 'queue' or 'immediate'"),
    ({"slots": -1}, "greater than or equal to 0"),
    ({"slots": True}, "Input should be a valid integer"),
    ({"slots": "1"}, "Input should be a valid integer"),
    ({"image": ""}, "must not be empty"),
    ({"pool": 3}, "Input should be a valid string"),
    ({"env": {"1BAD": "x"}}, "must be a shell variable name"),
    ({"env": {"BAD-NAME": "x"}}, "must be a shell variable name"),
    ({"env": {"COMPUTE_CODE_COMMIT": SHA}}, "prefix is reserved"),
    ({"env": {"OK": 1}}, "Input should be a valid string"),
    # workdir.
    ({"code": PATH_CODE, "workdir": "/abs"}, "must be relative to the code root"),
    ({"code": PATH_CODE, "workdir": "../up"}, "must not contain '..'"),
    ({"code": PATH_CODE, "workdir": "a/../b"}, "must not contain '..'"),
    ({"code": PATH_CODE, "workdir": ""}, "must not be empty"),
    ({"workdir": "src"}, "workdir is relative to the code root and requires code"),
    # Code sources.
    ({"code": {"source": "git", "repo": "shared/app"}}, "must be an absolute container path"),
    ({"code": {"source": "git", "repo": "/shared/../app"}}, "must not contain '..'"),
    ({"code": {"source": "git", "repo": "/a", "revision": "--upload-pack=x"}}, "start with '-'"),
    ({"code": {"source": "git", "repo": "/a", "revision": ""}}, "non-empty revision"),
    ({"code": {"source": "git", "repo": "/a", "revision": "main\n"}}, "control characters"),
    ({"code": {"source": "git", "repo": "/a", "include": ["x"]}}, "Extra inputs"),
    ({"code": {"source": "git", "dir": "/a"}}, "Field required"),
    ({"code": {"source": "context", "repo": "relative/app"}}, "must be an absolute local path"),
    (
        {"code": {"source": "context", "repo": "/src/app", "include": ["/etc/passwd"]}},
        "relative to the repository root",
    ),
    (
        {"code": {"source": "context", "repo": "/src/app", "include": ["../secret"]}},
        "must not contain '..'",
    ),
    (
        {"code": {"source": "context", "repo": "/src/app", "exclude": [""]}},
        "relative to the repository root",
    ),
    (
        {"code": {"source": "context", "repo": "/src/app", "exclude": ["/"]}},
        "one leading '/' anchors it there",
    ),
    (
        {"code": {"source": "context", "repo": "/src/app", "exclude": ["//data"]}},
        "one leading '/' anchors it there",
    ),
    (
        {"code": {"source": "context", "repo": "/src/app", "exclude": ["/data/../x"]}},
        "must not contain '..'",
    ),
    ({"code": {"source": "path", "dir": "app"}}, "must be an absolute container path"),
    ({"code": {"source": "path", "dir": "/a", "revision": "HEAD"}}, "Extra inputs"),
    ({"code": {"source": "svn", "repo": "/a"}}, "does not match any of the expected tags"),
    ({"code": {"repo": "/a"}}, "Unable to extract tag"),
    # Shells.
    (shell_fields(command="bash"), "a shell has no command"),
    (shell_fields(code={"source": "git", "repo": "/a"}), "only context or path code"),
    (shell_fields(output_dir="/shared/out"), "no prelude to create output_dir"),
    (shell_fields(code=PATH_CODE, workdir="src"), "no prelude to enter workdir"),
    (shell_fields(experiment={}), "experiment applies only to kind experiment"),
    # Experiments.
    ({"experiment": {"searcher": SEARCH}}, "experiment applies only to kind experiment"),
    (experiment_fields(output_dir=DROP), "kind experiment requires output_dir"),
    (experiment_fields(command=DROP), "kind experiment requires command"),
    (experiment_fields(command="model_def:MyTrial"), "legacy 'module:Class' entrypoint"),
    (experiment_fields(command=" pkg.model_def:Trial\n"), "legacy 'module:Class' entrypoint"),
    (experiment_fields(experiment={"bind_mounts": []}), "bind_mounts is not allowed"),
    (experiment_fields(experiment={"entrypoint": "python x"}), "entrypoint is rendered"),
    (
        experiment_fields(experiment={"checkpoint_storage": {"host_path": "/data"}}),
        "checkpoint_storage.host_path is not allowed",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"container_path": "/ckpt"}}),
        "checkpoint_storage.container_path is not allowed",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"checkpoint_path": "/ckpt"}}),
        "checkpoint_storage.checkpoint_path is a legacy field",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"tensorboard_path": "/tb"}}),
        "checkpoint_storage.tensorboard_path is a legacy field",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"storage_path": "/abs"}}),
        "storage_path must be relative without '..'",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"storage_path": "a/../../b"}}),
        "storage_path must be relative without '..'",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"storage_path": ""}}),
        "storage_path must be relative without '..'",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": {"storage_path": 5}}),
        "storage_path must be relative without '..'",
    ),
    (
        experiment_fields(experiment={"checkpoint_storage": "shared_fs"}),
        "checkpoint_storage must be an object",
    ),
    (experiment_fields(experiment={"searcher": "random"}), "searcher must be an object"),
    (
        experiment_fields(experiment={"resources": {"resource_pool": "gpu"}}),
        "resources.resource_pool duplicates the top-level pool; set pool instead",
    ),
    (
        experiment_fields(experiment={"resources": {"slots_per_trial": 2}}),
        "resources.slots_per_trial duplicates the top-level slots; set slots instead",
    ),
    (
        experiment_fields(experiment={"environment": {"image": "registry.example/x:1"}}),
        "environment.image duplicates the top-level image; set image instead",
    ),
    (
        experiment_fields(experiment={"environment": {"image": {"cuda": "x", "cpu": "y"}}}),
        "environment.image duplicates the top-level image; set image instead",
    ),
    (
        experiment_fields(experiment={"environment": {"environment_variables": ["A=1"]}}),
        "environment.environment_variables duplicates the top-level env; set env instead",
    ),
    (
        experiment_fields(pool="gpu", experiment={"resources": {"resource_pool": "gpu"}}),
        "resources.resource_pool duplicates the top-level pool; set pool instead",
    ),
    (experiment_fields(experiment={"resources": "gpu"}), "resources must be an object"),
    (experiment_fields(experiment={"environment": []}), "environment must be an object"),
    (experiment_fields(experiment={"searcher": {"metric": "loss"}}), "searcher.name is required"),
    (experiment_fields(experiment={"name": "x"}), "name duplicates the top-level name"),
    (
        experiment_fields(experiment={"workspace": "w", "project": "p"}),
        "workspace duplicates the top-level workspace",
    ),
    (experiment_fields(experiment={"project": "p"}), "project duplicates the top-level project"),
    (experiment_fields(workspace="w"), "sets workspace and project together"),
    (experiment_fields(project="p"), "sets workspace and project together"),
    # Workspaces and projects.
    ({"project": "p"}, "a command runs in a workspace and has no project"),
    (shell_fields(workspace="w", project="p"), "a shell runs in a workspace and has no project"),
]
for _limit in (DROP, 0, True, "2", None, 1.5):
    _search = {key: value for key, value in SEARCH.items() if key != "max_concurrent_trials"}
    if _limit is not DROP:
        _search["max_concurrent_trials"] = _limit
    INVALID.append(
        (experiment_fields(experiment={"searcher": _search}), "a search must set searcher.")
    )
for _name in ("grid", "adaptive_asha", "custom"):
    INVALID.append(
        (experiment_fields(experiment={"searcher": {"name": _name}}), "max_concurrent_trials")
    )


@pytest.mark.parametrize("fields, message", INVALID)
def test_invalid_specs_are_rejected(fields: Dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        build(**fields)


# Rendering

CHECKOUT = "git -C /run/determined/code "
LFS = (
    "-c filter.lfs.process='git-lfs filter-process' -c filter.lfs.required=true "
    "-c lfs.fetchinclude= -c lfs.fetchexclude= "
)
LFS_CHECKOUT = "GIT_LFS_SKIP_SMUDGE=0 " + CHECKOUT + LFS


def delivery(repo: str, checkout: str = CHECKOUT) -> str:
    """The isolated git delivery subshell for ``repo``, already quoted, at SHA."""

    root = "/run/determined/code"
    return (
        "( fail() { printf '%s\\n' \"compute: git code delivery failed: $1\" >&2; exit 1; }; "
        'export GIT_COMPUTE_UNSET=1 && eval "$(env | LC_ALL=C sed -n '
        "'s/^\\(GIT_[A-Za-z0-9_]*\\)=.*/unset \\1 \\&\\&/p') :\" "
        '&& test -z "${GIT_COMPUTE_UNSET+x}" '
        "|| fail 'cannot unset the GIT_ variables; the image needs env and sed'; "
        "export HOME=/dev/null/home XDG_CONFIG_HOME=/dev/null/home GIT_CONFIG_NOSYSTEM=1 "
        "GIT_ATTR_NOSYSTEM=1 || fail 'cannot isolate git from user and system config'; "
        f"git -c safe.directory={repo} clone -q --template= --shared --no-checkout -- {repo} "
        f"{root} || fail 'cannot clone the repository'; "
        f"{checkout}checkout -q --detach {SHA} || fail 'cannot check out {SHA}'; "
        f'test "$(git -C {root} rev-parse --show-toplevel)" = "$(cd -- {root} && pwd -P)" '
        f"|| fail 'the work tree is not {root}'; "
        f"test \"$(git -C {root} rev-parse HEAD)\" = {SHA} || fail 'HEAD is not {SHA}' )"
    )


def contained(root: str) -> str:
    """The physical workdir check that follows entering a workdir below ``root``, quoted."""

    return (
        f' && {{ test "$(r=$(cd -- {root} && pwd -P && echo .) && r=${{r%?.}} && r=${{r%/}} '
        '&& p=$(pwd -P && echo .) && p=${p%?.} && case "$p/" in ("$r"/*) echo in;; esac)" = in '
        "|| { printf 'compute: the workdir resolves to %s, outside the code root\\n' "
        '"$(pwd -P)" >&2; false; }; }'
    )


GIT_DELIVERY = delivery("/shared/repo")
LFS_DELIVERY = delivery("/shared/repo", LFS_CHECKOUT)


@pytest.mark.parametrize(
    "workdir, uses_lfs, expected",
    [
        (
            ".",
            False,
            GIT_DELIVERY + " && mkdir -p -- /shared/out && cd -- /run/determined/code",
        ),
        (
            ".",
            True,
            LFS_DELIVERY + " && mkdir -p -- /shared/out && cd -- /run/determined/code",
        ),
        (
            "src/pkg",
            False,
            GIT_DELIVERY
            + " && mkdir -p -- /shared/out && cd -- /run/determined/code/src/pkg"
            + contained("/run/determined/code"),
        ),
        (
            "./src//pkg/",
            True,
            LFS_DELIVERY
            + " && mkdir -p -- /shared/out && cd -- /run/determined/code/src/pkg"
            + contained("/run/determined/code"),
        ),
    ],
)
def test_git_prelude_bytes(workdir: str, uses_lfs: bool, expected: str) -> None:
    prelude = render_prelude(
        "git",
        output_dir="/shared/out",
        location="/shared/repo",
        commit=SHA,
        workdir=workdir,
        uses_lfs=uses_lfs,
    )

    assert prelude == expected


@pytest.mark.parametrize(
    "source, workdir, expected",
    [
        ("context", ".", "mkdir -p -- /shared/out && cd -- /run/determined/workdir"),
        (
            "context",
            "src/pkg",
            "mkdir -p -- /shared/out && cd -- /run/determined/workdir/src/pkg"
            + contained("/run/determined/workdir"),
        ),
        ("path", ".", "mkdir -p -- /shared/out && cd -- /shared/app"),
        (
            "path",
            "src/pkg",
            "mkdir -p -- /shared/out && cd -- /shared/app/src/pkg" + contained("/shared/app"),
        ),
        (None, ".", "mkdir -p -- /shared/out"),
    ],
)
def test_context_path_and_no_code_prelude_bytes(
    source: Optional[str], workdir: str, expected: str
) -> None:
    prelude = render_prelude(
        source,  # type: ignore[arg-type]
        output_dir="/shared/out",
        location="/shared/app" if source == "path" else None,
        commit=SHA if source == "context" else None,
        workdir=workdir,
    )

    assert prelude == expected


def test_entrypoint_shape() -> None:
    prelude = "mkdir -p -- /shared/out && cd -- /shared/app"

    assert render_entrypoint(prelude, "a; b\nc & d; wait") == (
        "mkdir -p -- /shared/out && cd -- /shared/app || exit $?\na; b\nc & d; wait"
    )


def test_awkward_values_are_quoted() -> None:
    repo = "/srv/it's a $repo; x\ny"
    output = "/out/$(touch pwned); rm -rf ~"
    workdir = 'sub dir/"q"'

    git = render_prelude(
        "git", output_dir=output, location=repo, commit=SHA, workdir=workdir, uses_lfs=True
    )
    context = render_prelude("context", output_dir=output, commit=SHA, workdir=workdir)
    path = render_prelude("path", output_dir=output, location=repo, workdir=workdir)

    quoted_repo = "'/srv/it'\"'\"'s a $repo; x\ny'"
    quoted_output = "'/out/$(touch pwned); rm -rf ~'"
    assert git == (
        f"{delivery(quoted_repo, LFS_CHECKOUT)} "
        f"&& mkdir -p -- {quoted_output} && cd -- '/run/determined/code/sub dir/\"q\"'"
        + contained("/run/determined/code")
    )
    assert context == (
        f"mkdir -p -- {quoted_output} && cd -- '/run/determined/workdir/sub dir/\"q\"'"
        + contained("/run/determined/workdir")
    )
    quoted_target = "'/srv/it'\"'\"'s a $repo; x\ny/sub dir/\"q\"'"
    assert path == (
        f"mkdir -p -- {quoted_output} && cd -- {quoted_target}" + contained(quoted_repo)
    )


def test_context_repository_never_reaches_the_container() -> None:
    local = "/local/tree/private-name"

    prelude = render_prelude("context", output_dir="/shared/out", location=local, commit=SHA)
    environment = code_environment("context", location=local, commit=SHA)

    assert "private-name" not in prelude
    assert environment == {
        "COMPUTE_CODE_SOURCE": "context",
        "COMPUTE_CODE_ROOT": "/run/determined/workdir",
        "COMPUTE_CODE_COMMIT": SHA,
    }


def test_provenance_environment() -> None:
    assert code_environment("git", location="/shared/repo", commit=SHA256) == {
        "COMPUTE_CODE_SOURCE": "git",
        "COMPUTE_CODE_ROOT": "/run/determined/code",
        "COMPUTE_CODE_COMMIT": SHA256,
    }
    assert code_environment("path", location="/shared//app/") == {
        "COMPUTE_CODE_SOURCE": "path",
        "COMPUTE_CODE_ROOT": "/shared/app",
    }
    assert code_environment(None) == {}


@pytest.mark.parametrize(
    "source, arguments, message",
    [
        ("git", {"location": "/r", "commit": "HEAD"}, "pinned to a full SHA"),
        ("git", {"location": "/r", "commit": SHA[:12]}, "pinned to a full SHA"),
        ("git", {"location": "/r", "commit": SHA.upper()}, "pinned to a full SHA"),
        ("git", {"location": "/r"}, "pinned to a full SHA"),
        ("git", {"commit": SHA}, "absolute container path"),
        ("context", {}, "pinned to a full SHA"),
        ("path", {"location": "/r", "commit": SHA}, "never pinned"),
        ("path", {}, "absolute container path"),
        ("context", {"commit": SHA, "uses_lfs": True}, "applies only to git"),
        ("path", {"location": "/r", "uses_lfs": True}, "applies only to git"),
        ("path", {"location": "/r", "workdir": "/abs"}, "relative to the code root"),
        ("context", {"commit": SHA, "workdir": "a/../../b"}, "'..'"),
        (None, {"workdir": "src"}, "require code"),
        (None, {"commit": SHA}, "require code"),
        ("svn", {"location": "/r", "commit": SHA}, "unknown code source"),
    ],
)
def test_renderer_rejects_inconsistent_arguments(
    source: Optional[str], arguments: Dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        render_prelude(source, output_dir="/shared/out", **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "source, arguments, message",
    [
        ("git", {"location": "/r", "commit": "HEAD"}, "pinned to a full SHA"),
        ("context", {}, "pinned to a full SHA"),
        ("path", {"location": "/r", "commit": SHA}, "never pinned"),
        ("svn", {}, "unknown code source"),
    ],
)
def test_provenance_rejects_inconsistent_arguments(
    source: str, arguments: Dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        code_environment(source, **arguments)  # type: ignore[arg-type]


# Running the rendered text

Shell = Tuple[str, str]
SHELLS = [
    pytest.param(("sh", "-c"), id="sh-c"),
    pytest.param(
        ("bash", "-lc"),
        id="bash-lc",
        marks=pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed"),
    ),
    pytest.param(
        ("dash", "-c"),
        id="dash-c",
        marks=pytest.mark.skipif(shutil.which("dash") is None, reason="dash is not installed"),
    ),
]
FORMS = {
    "sequence": "touch {a}; touch {b}",
    "or-list": "false || touch {b}",
    "two-lines": "touch {a}\ntouch {b}",
    "background": "touch {a} & touch {b}; wait",
}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
needs_lfs = pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")


def search_path(*programs: str) -> str:
    directories = []
    for program in programs:
        found = shutil.which(program)
        if found is None:
            pytest.skip(f"{program} is not installed")
        directory = os.path.dirname(found)
        if directory not in directories:
            directories.append(directory)
    return os.pathsep.join(directories)


def isolated_env(tmp_path: Path, *programs: str) -> Dict[str, str]:
    """Only PATH and a fresh HOME, so no user profile is read."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    tools = ("sh", "touch", "mkdir", "cat", "env", "sed", *programs)
    return {"PATH": search_path(*tools), "HOME": str(home)}


def wrapped_tools(tmp_path: Path, *programs: str) -> str:
    """A directory of wrappers for exactly ``programs``, each at its real location."""

    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for program in programs:
        found = shutil.which(program)
        assert found is not None, program
        wrapper = tools / program
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(found)} "$@"\n')
        wrapper.chmod(0o755)
    return str(tools)


def git_env(tmp_path: Path, *programs: str) -> Dict[str, str]:
    """An isolated environment that ignores every git configuration file."""

    return {
        **isolated_env(tmp_path, "git", *programs),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Spec Test",
        "GIT_AUTHOR_EMAIL": "spec-test@example.invalid",
        "GIT_COMMITTER_NAME": "Spec Test",
        "GIT_COMMITTER_EMAIL": "spec-test@example.invalid",
    }


def run_shell(
    shell: Shell, text: str, env: Dict[str, str], cwd: Path
) -> subprocess.CompletedProcess:
    program, flag = shell
    executable = shutil.which(program)
    assert executable is not None
    return subprocess.run(
        [executable, flag, text], env=env, cwd=cwd, capture_output=True, text=True, timeout=120
    )


def git(env: Dict[str, str], cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], env=env, cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def marker_command(form: str, marks: Path) -> str:
    return FORMS[form].format(a=shlex.quote(str(marks / "a")), b=shlex.quote(str(marks / "b")))


def compute_lines(result: subprocess.CompletedProcess) -> list:
    return [line for line in result.stderr.splitlines() if line.startswith("compute:")]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("form", sorted(FORMS))
@pytest.mark.parametrize("source", ["path", "context", pytest.param("git", marks=needs_git)])
def test_failed_prelude_runs_no_user_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, form: str, source: str
) -> None:
    # Each source fails differently: cd into a missing directory, or a clone of a missing
    # repository.
    monkeypatch.setattr(spec, "CODE_ROOT", str(tmp_path / "missing-root"))
    monkeypatch.setattr(spec, "GIT_CODE_ROOT", str(tmp_path / "missing-root" / "code"))
    env = isolated_env(tmp_path, *(["git"] if source == "git" else []))
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        source,  # type: ignore[arg-type]
        output_dir=str(tmp_path / "out"),
        location=str(tmp_path / "missing"),
        commit=None if source == "path" else SHA,
    )

    prelude_status = run_shell(shell, prelude, env, tmp_path).returncode
    result = run_shell(
        shell, render_entrypoint(prelude, marker_command(form, marks)), env, tmp_path
    )

    assert prelude_status != 0
    assert result.returncode == prelude_status, result.stderr
    assert sorted(marks.iterdir()) == []
    if source == "git":
        assert compute_lines(result) == [
            "compute: git code delivery failed: cannot clone the repository"
        ]


@pytest.mark.parametrize("shell", SHELLS)
def test_successful_prelude_runs_command_in_workdir(tmp_path: Path, shell: Shell) -> None:
    env = isolated_env(tmp_path)
    code = tmp_path / "code"
    (code / "src" / "pkg").mkdir(parents=True)
    output = tmp_path / "outputs" / "run 1"
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude("path", output_dir=str(output), location=str(code), workdir="src/pkg")
    command = f"pwd -P > {shlex.quote(str(marks / 'pwd'))}; {marker_command('two-lines', marks)}"

    result = run_shell(shell, render_entrypoint(prelude, command), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert output.is_dir()
    assert (marks / "pwd").read_text().strip() == os.path.realpath(code / "src" / "pkg")
    assert (marks / "a").exists() and (marks / "b").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_command_status_is_the_task_status(tmp_path: Path, shell: Shell) -> None:
    env = isolated_env(tmp_path)
    prelude = render_prelude(None, output_dir=str(tmp_path / "out"))

    result = run_shell(shell, render_entrypoint(prelude, "true\nexit 7"), env, tmp_path)

    assert result.returncode == 7
    assert (tmp_path / "out").is_dir()


@pytest.mark.parametrize("shell", SHELLS)
def test_values_are_quoted_never_executed(tmp_path: Path, shell: Shell) -> None:
    env = isolated_env(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    # Relative targets, so anything executed would land in the working directory `marks`.
    bait = "b $(touch x); touch y `touch z`\n'q' \"$HOME\" & touch w"
    code = tmp_path / "code"
    (code / bait).mkdir(parents=True)
    output = tmp_path / "out" / bait
    prelude = render_prelude("path", output_dir=str(output), location=str(code), workdir=bait)
    command = f"pwd -P > {shlex.quote(str(marks / 'pwd'))}"

    result = run_shell(shell, render_entrypoint(prelude, command), env, marks)

    assert result.returncode == 0, result.stderr
    assert output.is_dir()
    assert (marks / "pwd").read_text()[:-1] == os.path.realpath(code / bait)
    assert sorted(path.name for path in marks.iterdir()) == ["pwd"]


def _context_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "container" / "workdir"
    root.mkdir(parents=True)
    monkeypatch.setattr(spec, "CODE_ROOT", str(root))
    return root


def _git_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The clone target, whose parent exists as /run/determined does in the container."""

    root = tmp_path / "container" / "code"
    root.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(spec, "GIT_CODE_ROOT", str(root))
    return root


def test_context_prelude_enters_the_extracted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _context_root(tmp_path, monkeypatch)
    (root / "src").mkdir()
    env = isolated_env(tmp_path)
    prelude = render_prelude("context", output_dir=str(tmp_path / "out"), commit=SHA, workdir="src")

    result = run_shell(("sh", "-c"), render_entrypoint(prelude, "pwd -P"), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == os.path.realpath(root / "src")


def _linked_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> Tuple[str, Optional[str], Path]:
    """Code with pkg/sub, a link alias -> pkg, two links to a directory beside the root, and a
    link to a sibling whose name extends the root's.

    Returns the location and commit to render with, and the code root in the container.
    """

    if source == "git":
        root = _git_root(tmp_path, monkeypatch)
        tree = tmp_path / "repo"
    elif source == "context":
        root = tree = _context_root(tmp_path, monkeypatch)
    else:
        root = tree = tmp_path / "shared" / "app"
    outside = root.parent / "outside"
    (outside / "sub").mkdir(parents=True)
    (tree / "pkg" / "sub").mkdir(parents=True)
    (tree / "pkg" / "sub" / "keep").write_text("")  # git records no empty directory
    os.symlink(outside, tree / "external")
    os.symlink("../outside", tree / "up")
    # A sibling whose name extends the root's: a string-prefix check would take it as inside.
    (root.parent / (root.name + "-private")).mkdir()
    os.symlink("../" + root.name + "-private", tree / "sibling")
    os.symlink("pkg", tree / "alias")
    if source != "git":
        return str(root), SHA if source == "context" else None, root
    env = git_env(tmp_path)
    git(env, tree, "init", "-q", "-b", "main")
    git(env, tree, "add", "-A")
    git(env, tree, "commit", "-q", "-m", "links")
    return str(tree), git(env, tree, "rev-parse", "HEAD"), root


SOURCES = ["path", "context", pytest.param("git", marks=needs_git)]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("workdir", ["external", "up", "external/sub", "sibling"])
@pytest.mark.parametrize("source", SOURCES)
def test_a_workdir_outside_the_code_root_runs_no_user_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, workdir: str, source: str
) -> None:
    location, commit, root = _linked_code(tmp_path, monkeypatch, source)
    env = git_env(tmp_path) if source == "git" else isolated_env(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        source,  # type: ignore[arg-type]
        output_dir=str(tmp_path / "out"),
        location=location,
        commit=commit,
        workdir=workdir,
    )

    result = run_shell(
        shell, render_entrypoint(prelude, marker_command("sequence", marks)), env, tmp_path
    )

    assert result.returncode == 1, result.stderr
    assert sorted(marks.iterdir()) == []
    assert compute_lines(result) == [
        f"compute: the workdir resolves to {os.path.realpath(root / workdir)}, "
        "outside the code root"
    ]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("workdir", ["alias", "alias/sub", "pkg/sub"])
@pytest.mark.parametrize("source", SOURCES)
def test_a_workdir_through_an_in_tree_symlink_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, workdir: str, source: str
) -> None:
    location, commit, root = _linked_code(tmp_path, monkeypatch, source)
    env = git_env(tmp_path) if source == "git" else isolated_env(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        source,  # type: ignore[arg-type]
        output_dir=str(tmp_path / "out"),
        location=location,
        commit=commit,
        workdir=workdir,
    )
    # The check's variables must not reach the command.
    command = (
        f"pwd -P > {shlex.quote(str(marks / 'pwd'))}; "
        f"printf %s \"${{r-unset}} ${{p-unset}}\" > {shlex.quote(str(marks / 'r'))}"
    )

    result = run_shell(shell, render_entrypoint(prelude, command), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (marks / "pwd").read_text().strip() == os.path.realpath(root / workdir)
    assert (marks / "r").read_text() == "unset unset"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("workdir", [".", "pkg", "alias"])
def test_a_path_dir_may_itself_be_a_symlink(tmp_path: Path, shell: Shell, workdir: str) -> None:
    env = isolated_env(tmp_path)
    real = tmp_path / "volume" / "app"
    (real / "pkg").mkdir(parents=True)
    os.symlink("pkg", real / "alias")
    shared = tmp_path / "shared-app"
    os.symlink(real, shared)
    prelude = render_prelude(
        "path", output_dir=str(tmp_path / "out"), location=str(shared), workdir=workdir
    )

    result = run_shell(shell, render_entrypoint(prelude, "pwd -P"), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == os.path.realpath(real / workdir)


# $(...) drops trailing newlines, so a root and a directory that differ only by one would
# compare equal without the check's sentinel.
NEWLINE_CASES = {
    # The workdir leads to a sibling named as the root plus a newline.
    "sibling-with-newline": ("app", "app\n", False),
    # The root's name ends in a newline, and the workdir leads to the same name without it.
    "root-with-newline": ("app\n", "app", False),
    # A real directory inside a root whose name ends in a newline.
    "inside-root-with-newline": ("app\n", None, True),
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("case", sorted(NEWLINE_CASES))
def test_the_workdir_check_keeps_trailing_newlines(tmp_path: Path, shell: Shell, case: str) -> None:
    root_name, target_name, inside = NEWLINE_CASES[case]
    env = isolated_env(tmp_path)
    shared = tmp_path / "shared"
    root = shared / root_name
    (root / "pkg").mkdir(parents=True)
    workdir = "pkg"
    if target_name is not None:
        (shared / target_name).mkdir()
        os.symlink(shared / target_name, root / "ext")
        workdir = "ext"
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "path", output_dir=str(tmp_path / "out"), location=str(root), workdir=workdir
    )
    command = f"pwd -P > {shlex.quote(str(marks / 'pwd'))}"

    result = run_shell(shell, render_entrypoint(prelude, command), env, tmp_path)

    if inside:
        assert result.returncode == 0, result.stderr
        assert (marks / "pwd").read_text() == os.path.realpath(root / "pkg") + "\n"
    else:
        assert result.returncode == 1, result.stderr
        assert sorted(marks.iterdir()) == []
        # The message's own $(pwd -P) drops the trailing newline.
        target = os.path.realpath(shared / target_name).rstrip("\n")
        assert compute_lines(result) == [
            f"compute: the workdir resolves to {target}, outside the code root"
        ]


@pytest.mark.parametrize("shell", SHELLS)
def test_a_path_dir_may_be_the_filesystem_root(tmp_path: Path, shell: Shell) -> None:
    # The resolved root is then /, whose slash the check strips so the pattern needs only one.
    env = isolated_env(tmp_path)
    (tmp_path / "pkg").mkdir()
    workdir = os.path.join(os.path.realpath(tmp_path), "pkg").lstrip("/")
    prelude = render_prelude(
        "path", output_dir=str(tmp_path / "out"), location="/", workdir=workdir
    )

    result = run_shell(shell, render_entrypoint(prelude, "pwd -P"), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == os.path.realpath(tmp_path / "pkg")


def _source_repository(tmp_path: Path, env: Dict[str, str]) -> Tuple[Path, str]:
    repo = tmp_path / "shared repo's"
    (repo / "pkg").mkdir(parents=True)
    git(env, repo, "init", "-q", "-b", "main")
    (repo / "main.py").write_text("print('pinned')\n")
    (repo / "pkg" / "data.txt").write_text("first\n")
    git(env, repo, "add", "-A")
    git(env, repo, "commit", "-q", "-m", "first")
    pinned = git(env, repo, "rev-parse", "HEAD")
    (repo / "pkg" / "data.txt").write_text("second\n")
    git(env, repo, "commit", "-q", "-a", "-m", "second")
    return repo, pinned


@needs_git
def test_git_prelude_checks_out_the_pinned_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned, workdir="pkg"
    )
    command = (
        f"cat data.txt > {shlex.quote(str(marks / 'data'))}; "
        f"git rev-parse HEAD > {shlex.quote(str(marks / 'head'))}"
    )

    result = run_shell(("sh", "-c"), render_entrypoint(prelude, command), env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (marks / "data").read_text() == "first\n"
    assert (marks / "head").read_text().strip() == pinned
    # --shared borrows objects through alternates instead of copying them.
    assert (root / ".git" / "objects" / "info" / "alternates").is_file()
    assert git(env, root, "status", "--porcelain", "--branch").startswith("## HEAD (no branch)")


@needs_git
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_git_prelude_ignores_what_hooks_left_in_the_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A startup hook running pip leaves ~/.cache/pip in the workdir, which may be HOME.
    workdir = _context_root(tmp_path, monkeypatch)
    (workdir / ".cache" / "pip").mkdir(parents=True)
    (workdir / ".cache" / "pip" / "x").write_text("cached\n")
    root = _git_root(tmp_path, monkeypatch)
    env = {**git_env(tmp_path), "HOME": str(workdir)}
    repo, pinned = _source_repository(tmp_path, env)
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned, workdir="pkg"
    )

    result = run_shell(("bash", "-lc"), render_entrypoint(prelude, "cat data.txt"), env, workdir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "first\n"
    assert (root / "pkg" / "data.txt").read_text() == "first\n"


# Each makes git work on another repository, work tree, index, object store or exec path, or
# run hooks and config from elsewhere, if the task environment reaches the delivery. git reads
# core.worktree from the repository's own config only, so the config variables add a hook.
HOSTILE_GIT_ENV = {
    "work-tree": {"GIT_WORK_TREE": "{elsewhere}"},
    "git-dir": {"GIT_DIR": "{elsewhere}/.git"},
    "index-file": {"GIT_INDEX_FILE": "{elsewhere}/.git/index"},
    "object-directory": {"GIT_OBJECT_DIRECTORY": "{elsewhere}/.git/objects"},
    "config-parameters": {
        "GIT_CONFIG_PARAMETERS": "'core.worktree'='{elsewhere}' 'core.hookspath'='{hooks}'"
    },
    "config-count": {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": "{elsewhere}",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "{hooks}",
    },
    "exec-path": {"GIT_EXEC_PATH": "{elsewhere}"},
    "template-dir": {"GIT_TEMPLATE_DIR": "{templates}"},
}


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("hostile", sorted(HOSTILE_GIT_ENV))
def test_git_delivery_ignores_the_task_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, hostile: str
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(env, elsewhere, "init", "-q", "-b", "main")
    templates = tmp_path / "templates"
    (templates / "hooks").mkdir(parents=True)
    (templates / "config").write_text(f"[core]\n\tworktree = {elsewhere}\n")
    hook = templates / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(tmp_path / 'hooked'))}\n")
    hook.chmod(0o755)
    hostile_env = {
        key: value.format(elsewhere=elsewhere, templates=templates, hooks=templates / "hooks")
        for key, value in HOSTILE_GIT_ENV[hostile].items()
    }
    task_env = {**env, **hostile_env}
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )
    command = f"env > {shlex.quote(str(marks / 'env'))}; pwd -P > {shlex.quote(str(marks / 'pwd'))}"

    result = run_shell(shell, render_entrypoint(prelude, command), task_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (root / "main.py").read_text() == "print('pinned')\n"
    assert git(env, root, "rev-parse", "HEAD") == pinned
    assert git(env, root, "status", "--porcelain") == ""
    assert not (elsewhere / "main.py").exists() and not (tmp_path / "hooked").exists()
    assert (marks / "pwd").read_text().strip() == os.path.realpath(root)
    # The workload still gets its own environment.
    seen = dict(
        line.split("=", 1) for line in (marks / "env").read_text().splitlines() if "=" in line
    )
    assert {key: seen.get(key) for key in hostile_env} == hostile_env
    assert seen["HOME"] == task_env["HOME"]


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
def test_git_delivery_reads_no_user_config_without_git_config_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(tmp_path / 'hooked'))}\n")
    hook.chmod(0o755)
    home, xdg = tmp_path / "user-home", tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    home.mkdir()
    for config in (home / ".gitconfig", xdg / "git" / "config"):
        config.write_text(f"[core]\n\thooksPath = {hooks}\n")
    # As git before 2.32 sees it: GIT_CONFIG_GLOBAL is not set, so HOME and XDG apply.
    task_env = {**isolated_env(tmp_path, "git"), "HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
    control = tmp_path / "control"
    git(task_env, tmp_path, "clone", "-q", "--no-checkout", str(repo), str(control))
    git(task_env, control, "checkout", "-q", "--detach", pinned)
    assert (tmp_path / "hooked").exists()  # the user config applies to a plain checkout
    (tmp_path / "hooked").unlink()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(shell, render_entrypoint(prelude, 'printf %s "$HOME"'), task_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(home)
    assert (root / "main.py").exists()
    assert not (tmp_path / "hooked").exists()


@needs_git
def test_git_delivery_fails_closed_without_sed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )
    tools = wrapped_tools(tmp_path, "git", "mkdir", "touch", "env")

    result = run_shell(
        ("sh", "-c"),
        render_entrypoint(prelude, marker_command("sequence", marks)),
        {**env, "PATH": tools, "GIT_WORK_TREE": str(tmp_path)},
        tmp_path,
    )

    assert result.returncode != 0
    assert compute_lines(result) == [
        "compute: git code delivery failed: cannot unset the GIT_ variables; "
        "the image needs env and sed"
    ]
    assert sorted(marks.iterdir()) == [] and not root.exists()


def _wrapped_git(tmp_path: Path, env: Dict[str, str], body: str) -> Dict[str, str]:
    """Return ``env`` with a git first on PATH that runs ``body``, with $real the real git.

    The delivery calls it after cleaning the environment and config, so it stands in for what
    the image's own git or its compiled-in defaults would do.
    """

    real = shutil.which("git", path=env["PATH"])
    assert real is not None
    tools = tmp_path / "wrapped-git"
    tools.mkdir()
    wrapper = tools / "git"
    wrapper.write_text(f"#!/bin/sh\nreal={shlex.quote(real)}\n{body}")
    wrapper.chmod(0o755)
    return {**env, "PATH": os.pathsep.join([str(tools), env["PATH"]])}


# What an image's default template directory can seed in a clone. None of it needs a variable
# or a config file that the delivery cleans, and each leaves HEAD and the work tree in place.
CLONE_TEMPLATES = {
    "post-checkout-hook": {
        "hooks/post-checkout": "#!/bin/sh\nprintf \"print('hooked')\\n\" > main.py\n",
    },
    "smudge-filter": {
        "config": '[filter "evil"]\n\tsmudge = sed s/pinned/smudged/\n',
        "info/attributes": "* filter=evil\n",
    },
    "attributes": {"info/attributes": "*.py text eol=crlf\n"},
    "sparse-checkout": {
        "config": "[core]\n\tsparseCheckout = true\n",
        "info/sparse-checkout": "/pkg/\n",
    },
    "replace-ref": {"refs/replace/{pinned}": "{later}\n"},
}


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("template", sorted(CLONE_TEMPLATES))
def test_git_delivery_takes_nothing_from_a_clone_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, template: str
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    later = git(env, repo, "rev-parse", "HEAD")
    templates = tmp_path / "templates"
    for name, content in CLONE_TEMPLATES[template].items():
        path = templates / name.format(pinned=pinned)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.format(later=later))
        path.chmod(0o755)
    # Unsetting GIT_TEMPLATE_DIR and hiding init.templateDir leave the compiled-in default.
    task_env = _wrapped_git(
        tmp_path, env, f'GIT_TEMPLATE_DIR={shlex.quote(str(templates))} exec "$real" "$@"\n'
    )
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(shell, render_entrypoint(prelude, "true"), task_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (root / "main.py").read_bytes() == b"print('pinned')\n"
    assert (root / "pkg" / "data.txt").read_bytes() == b"first\n"
    assert git(env, root, "rev-parse", "HEAD") == pinned
    assert git(env, root, "status", "--porcelain") == ""


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
def test_git_delivery_ignores_system_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell
) -> None:
    # An image's /etc/gitattributes can convert the checkout with no config at all, and no test
    # may write there; every git the delivery runs must see GIT_ATTR_NOSYSTEM instead.
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    seen = tmp_path / "seen"
    seen.mkdir()
    task_env = _wrapped_git(
        tmp_path, env, f'env > {shlex.quote(str(seen))}/$$\nexec "$real" "$@"\n'
    )
    task_env["GIT_ATTR_NOSYSTEM"] = "0"  # the task's own value must not reach the delivery
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(shell, render_entrypoint(prelude, "true"), task_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (root / "main.py").exists()
    calls = [path.read_text().splitlines() for path in seen.iterdir()]
    assert len(calls) >= 2  # the clone and the checkout at least
    for lines in calls:
        assert {"GIT_ATTR_NOSYSTEM=1", "GIT_CONFIG_NOSYSTEM=1", "HOME=/dev/null/home"} <= set(lines)


def _sed_splits_invalid_utf8() -> bool:
    """Whether sed in a UTF-8 locale ends ``.*`` at an invalid byte, as GNU sed does."""

    probe = subprocess.run(
        ["sed", "-n", "s/^x.*/y/p"],
        input=b"x\xe9z\n",
        env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C.UTF-8"},
        capture_output=True,
    )
    return probe.returncode == 0 and probe.stdout != b"y\n"


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "value", [b"Jos\xe9; touch MARK", b"Ren\xe9 (ops)"], ids=["eval", "syntax"]
)
def test_git_delivery_unsets_values_that_are_invalid_in_the_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell, value: bytes
) -> None:
    if shutil.which("sed") is None or not _sed_splits_invalid_utf8():
        pytest.skip("sed does not stop at an invalid byte in C.UTF-8 here")
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    marks = tmp_path / "marks"
    marks.mkdir()
    # A Latin-1 name from the image or a startup hook, in a UTF-8 locale.
    task_env = {**env, "LC_ALL": "C.UTF-8", "GIT_COMMITTER_NAME": os.fsdecode(value)}
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )
    command = f"env > {shlex.quote(str(marks / 'env'))}"

    result = run_shell(shell, render_entrypoint(prelude, command), task_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "MARK").exists()
    assert (root / "main.py").read_text() == "print('pinned')\n"
    assert b"GIT_COMMITTER_NAME=" + value in (marks / "env").read_bytes().splitlines()


@needs_git
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_git_delivery_fails_when_it_cannot_hide_the_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf \"print('hooked')\\n\" > main.py\n")
    hook.chmod(0o755)
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n")
    # A login profile can mark HOME readonly, so moving it away fails under bash -lc.
    (home / ".bash_profile").write_text("readonly HOME\n")
    task_env = {**isolated_env(tmp_path, "git"), "HOME": str(home)}
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(
        ("bash", "-lc"),
        render_entrypoint(prelude, marker_command("sequence", marks)),
        task_env,
        tmp_path,
    )

    assert result.returncode != 0
    assert compute_lines(result) == [
        "compute: git code delivery failed: cannot isolate git from user and system config"
    ]
    assert sorted(marks.iterdir()) == [] and not root.exists()


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
def test_git_delivery_fails_when_the_work_tree_is_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # Nothing the delivery cleans can move the work tree any more; a git that points the new
    # clone's core.worktree elsewhere stands in for whatever still could.
    task_env = _wrapped_git(
        tmp_path,
        env,
        '"$real" "$@" || exit\n'
        f'case " $* " in *" clone "*) exec "$real" -C {shlex.quote(str(root))} '
        f"config core.worktree {shlex.quote(str(elsewhere))};; esac\n",
    )
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(
        shell, render_entrypoint(prelude, marker_command("sequence", marks)), task_env, tmp_path
    )

    assert result.returncode != 0
    assert compute_lines(result) == [
        f"compute: git code delivery failed: the work tree is not {root}"
    ]
    assert sorted(marks.iterdir()) == []


@needs_git
@pytest.mark.parametrize("shell", SHELLS)
def test_git_delivery_fails_when_head_is_not_the_pinned_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: Shell
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path)
    repo, pinned = _source_repository(tmp_path, env)
    later = git(env, repo, "rev-parse", "HEAD")
    # A git that checks out another commit after the delivery's own checkout stands in for
    # anything that moves HEAD once the environment and templates are clean.
    task_env = _wrapped_git(
        tmp_path,
        env,
        '"$real" "$@" || exit\n'
        f'case " $* " in *" checkout "*) exec "$real" -C {shlex.quote(str(root))} '
        f"checkout -q --detach {later};; esac\n",
    )
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned
    )

    result = run_shell(
        shell, render_entrypoint(prelude, marker_command("sequence", marks)), task_env, tmp_path
    )

    assert result.returncode != 0
    assert compute_lines(result) == [f"compute: git code delivery failed: HEAD is not {pinned}"]
    assert sorted(marks.iterdir()) == []


def _lfs_repository(tmp_path: Path, env: Dict[str, str]) -> Tuple[Path, str]:
    repo = tmp_path / "lfs repo"
    repo.mkdir()
    git(env, repo, "init", "-q", "-b", "main")
    git(env, repo, "lfs", "install", "--local")
    git(env, repo, "lfs", "track", "*.bin")
    (repo / "model.bin").write_bytes(b"weights\n")
    git(env, repo, "add", "-A")
    git(env, repo, "commit", "-q", "-m", "model")
    assert git(env, repo, "cat-file", "-p", "HEAD:model.bin").startswith("version https://git-lfs")
    return repo, git(env, repo, "rev-parse", "HEAD")


LFS_SKIPS = {
    "none": {},
    "skip-smudge": {"GIT_LFS_SKIP_SMUDGE": "1"},
    "fetch-exclude": {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "lfs.fetchexclude",
        "GIT_CONFIG_VALUE_0": "*",
    },
    "fetch-include": {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "lfs.fetchinclude",
        "GIT_CONFIG_VALUE_0": "nothing/*",
    },
}


@needs_git
@needs_lfs
@pytest.mark.parametrize("skip", sorted(LFS_SKIPS))
def test_git_prelude_smudges_lfs_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip: str
) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path, "git-lfs")
    repo, pinned = _lfs_repository(tmp_path, env)
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned, uses_lfs=True
    )

    # The image or TaskSpec.env may carry settings that leave pointers in place.
    result = run_shell(
        ("sh", "-c"), render_entrypoint(prelude, "true"), {**env, **LFS_SKIPS[skip]}, tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert (root / "model.bin").read_bytes() == b"weights\n"


@needs_git
@needs_lfs
def test_git_prelude_fails_without_git_lfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path, monkeypatch)
    env = git_env(tmp_path, "git-lfs")
    repo, pinned = _lfs_repository(tmp_path, env)
    exec_path = git(env, tmp_path, "--exec-path")
    if shutil.which("git-lfs", path=exec_path):
        pytest.skip("git-lfs is installed in git's exec path")
    # Wrappers keep each tool's real location, and leave git-lfs off PATH.
    tools = wrapped_tools(tmp_path, "git", "mkdir", "touch", "env", "sed")
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned, uses_lfs=True
    )

    result = run_shell(
        ("sh", "-c"),
        render_entrypoint(prelude, marker_command("sequence", marks)),
        {**env, "PATH": tools},
        tmp_path,
    )

    assert result.returncode != 0
    assert "git-lfs" in result.stderr
    assert compute_lines(result) == [
        f"compute: git code delivery failed: cannot check out {pinned}"
    ]
    assert sorted(marks.iterdir()) == []
    assert not (root / "model.bin").exists()


@needs_git
def test_a_validated_anchored_exclude_drops_only_the_top_level_directory(tmp_path: Path) -> None:
    env = git_env(tmp_path)
    repo = tmp_path / "app"
    for path in ("data/a.txt", "src/pkg/data/x.json"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("{}\n")
    git(env, repo, "init", "-q", "-b", "main")
    git(env, repo, "add", "-A")
    git(env, repo, "commit", "-q", "-m", "data")
    code = ContextCode(source="context", repo=str(repo), exclude=["/data/"])

    result = plan_context(code.repo, code.revision, code.include, code.exclude)
    paths = {item["path"] for item in result.files}

    assert "src/pkg/data/x.json" in paths
    assert "data/a.txt" not in paths


# Compiler

RESOURCES = Resources(image="registry.example/train:1", pool="gpu", slots=2)
GIT_PLAN = code.GitCode(repo="/shared/app", commit=SHA, uses_lfs=False, content_digest=SHA)
CONTEXT_FILES = (
    {
        "path": "train.py",
        "type": 48,
        "content": "cHJpbnQoKQo=",
        "mtime": "0",
        "mode": 420,
        "uid": 0,
        "gid": 0,
    },
)


def context_plan(**fields: Any) -> code.ContextCode:
    values: Dict[str, Any] = {
        "repo": "/local/app",
        "commit": SHA,
        "dirty": False,
        "files": CONTEXT_FILES,
        "manifest": (),
        "content_digest": "d" * 64,
        "size": 6,
        "included": (),
        "excluded": (),
        "skipped": (),
    }
    values.update(fields)
    return code.ContextCode(**values)


def env_list(*names: str, **values: str) -> list:
    return [f"{name}={values[name]}" for name in names]


def test_command_compiles_to_bash_with_the_prelude() -> None:
    task = build(
        code={"source": "git", "repo": "/shared/app"},
        workdir="src",
        env={"B": "2", "A": "1"},
    )

    request = spec.compile_request(task, GIT_PLAN, RESOURCES, workspace_id=7)

    prelude = render_prelude(
        "git", output_dir="/shared/out", location="/shared/app", commit=SHA, workdir="src"
    )
    assert request == spec.CreateRequest(
        kind="command",
        config={
            "description": "train",
            "resources": {"resource_pool": "gpu", "slots": 2},
            "environment": {
                "image": "registry.example/train:1",
                "environment_variables": [
                    "A=1",
                    "B=2",
                    "COMPUTE_CODE_SOURCE=git",
                    "COMPUTE_CODE_ROOT=/run/determined/code",
                    f"COMPUTE_CODE_COMMIT={SHA}",
                    "COMPUTE_OUTPUT_DIR=/shared/out",
                ],
            },
            "entrypoint": ["/bin/bash", "-lc", render_entrypoint(prelude, "python train.py")],
        },
        files=(),
        workspace_id=7,
    )


def test_git_lfs_reaches_the_prelude() -> None:
    task = build(code={"source": "git", "repo": "/shared/app"})
    lfs = code.GitCode(repo="/shared/app", commit=SHA, uses_lfs=True, content_digest=SHA)

    entrypoint = spec.compile_request(task, lfs, RESOURCES).config["entrypoint"][2]

    assert "GIT_LFS_SKIP_SMUDGE=0" in entrypoint


def test_shells_carry_code_but_no_entrypoint() -> None:
    context = build(**shell_fields(code={"source": "context", "repo": "/local/app"}))
    path = build(**shell_fields(code=PATH_CODE))

    packed = spec.compile_request(context, context_plan(), RESOURCES)
    in_place = spec.compile_request(path, code.PathCode(dir="/shared/app"), RESOURCES)

    assert packed.files == CONTEXT_FILES
    assert "entrypoint" not in packed.config and "entrypoint" not in in_place.config
    assert packed.config["environment"]["environment_variables"] == [
        "COMPUTE_CODE_SOURCE=context",
        "COMPUTE_CODE_ROOT=/run/determined/workdir",
        f"COMPUTE_CODE_COMMIT={SHA}",
    ]
    assert in_place.files == ()
    assert in_place.config["environment"]["environment_variables"] == [
        "COMPUTE_CODE_SOURCE=path",
        "COMPUTE_CODE_ROOT=/shared/app",
    ]


def test_experiment_merges_its_config_and_sends_the_model_definition() -> None:
    settings = {
        "searcher": dict(SEARCH),
        "resources": {"max_slots": 4, "priority": 10},
        "environment": {"force_pull_image": True},
        "checkpoint_storage": {"type": "shared_fs", "storage_path": "runs/a"},
    }
    task = build(
        **experiment_fields(
            code={"source": "context", "repo": "/local/app"},
            experiment=settings,
            env={"WANDB_PROJECT": "demo"},
            workspace="research",
            project="baselines",
        )
    )
    before = json.dumps(task.experiment, sort_keys=True)

    request = spec.compile_request(task, context_plan(), RESOURCES)

    prelude = render_prelude("context", output_dir="/shared/out", commit=SHA)
    assert request.kind == "experiment" and request.workspace_id is None
    assert request.files == CONTEXT_FILES
    assert request.config == {
        "searcher": SEARCH,
        "resources": {"max_slots": 4, "priority": 10, "resource_pool": "gpu", "slots_per_trial": 2},
        "environment": {
            "force_pull_image": True,
            "image": "registry.example/train:1",
            "environment_variables": [
                "WANDB_PROJECT=demo",
                "COMPUTE_CODE_SOURCE=context",
                "COMPUTE_CODE_ROOT=/run/determined/workdir",
                f"COMPUTE_CODE_COMMIT={SHA}",
                "COMPUTE_OUTPUT_DIR=/shared/out",
            ],
        },
        "checkpoint_storage": {"type": "shared_fs", "storage_path": "runs/a"},
        "name": "train",
        "entrypoint": render_entrypoint(prelude, "python train.py"),
        "workspace": "research",
        "project": "baselines",
    }
    # The spec is untouched, and the config is plain enough to send as YAML.
    assert json.dumps(task.experiment, sort_keys=True) == before
    assert yaml.safe_load(yaml.safe_dump(request.config)) == request.config


def test_experiment_sections_may_be_null() -> None:
    task = build(**experiment_fields(experiment={"resources": None, "environment": None}))

    config = spec.compile_request(task, None, RESOURCES).config

    assert config["resources"] == {"resource_pool": "gpu", "slots_per_trial": 2}
    assert config["environment"]["image"] == "registry.example/train:1"
    assert "workspace" not in config and "project" not in config


def test_an_experiment_takes_no_workspace_id() -> None:
    with pytest.raises(ValueError, match="names its workspace in the config"):
        spec.compile_request(build(**experiment_fields()), None, RESOURCES, workspace_id=3)


@pytest.mark.parametrize(
    "kind, source",
    [
        (kind, source)
        for kind in ("command", "shell", "experiment")
        for source in (None, "git", "context", "path")
        if (kind, source) != ("shell", "git")
    ],
)
def test_no_config_carries_work_dir_or_bind_mounts(kind: str, source: Optional[str]) -> None:
    specs = {
        None: (None, None),
        "git": ({"source": "git", "repo": "/shared/app"}, GIT_PLAN),
        "context": ({"source": "context", "repo": "/local/app"}, context_plan()),
        "path": (PATH_CODE, code.PathCode(dir="/shared/app")),
    }
    fields, planned = specs[source]
    extra = {"code": fields} if fields else {}
    task = build(**(shell_fields(**extra) if kind == "shell" else {"kind": kind, **extra}))

    config = spec.compile_request(task, planned, RESOURCES).config

    assert "work_dir" not in json.dumps(config)
    assert "bind_mounts" not in config
    if kind != "shell":
        entrypoint = config["entrypoint"] if kind == "experiment" else config["entrypoint"][2]
        assert entrypoint.endswith(" || exit $?\npython train.py")


def test_the_request_is_deterministic() -> None:
    first = build(env={"A": "1", "B": "2"}, code=PATH_CODE)
    second = build(env={"B": "2", "A": "1"}, code=PATH_CODE)
    planned = code.PathCode(dir="/shared/app")

    assert spec.compile_request(first, planned, RESOURCES) == spec.compile_request(
        second, planned, RESOURCES
    )


def test_the_planned_code_must_match_the_spec() -> None:
    with pytest.raises(ValueError, match="the planned code is None, but the spec's is git"):
        spec.compile_request(build(code={"source": "git", "repo": "/a"}), None, RESOURCES)
    with pytest.raises(ValueError, match="the planned code is git, but the spec's is None"):
        spec.resolve(build(), GIT_PLAN, RESOURCES)


def test_concurrent_trials() -> None:
    assert spec.concurrent_trials(build()) == 1
    assert spec.concurrent_trials(build(**experiment_fields())) == 1
    single = {"searcher": {"name": "single", "metric": "loss"}}
    assert spec.concurrent_trials(build(**experiment_fields(experiment=single))) == 1
    search = {"searcher": SEARCH}
    assert spec.concurrent_trials(build(**experiment_fields(experiment=search))) == 2


def test_resolve_pins_the_revision_and_fills_the_resources() -> None:
    git = build(code={"source": "git", "repo": "/shared/app", "revision": "main"})
    context = build(**shell_fields(code={"source": "context", "repo": "/local/app"}))
    path = build(code=PATH_CODE, slots=0)

    pinned = spec.resolve(git, GIT_PLAN, RESOURCES)
    packed = spec.resolve(context, context_plan(), RESOURCES)
    in_place = spec.resolve(path, code.PathCode(dir="/shared/app"), RESOURCES)

    assert pinned.code.revision == SHA and packed.code.revision == SHA
    assert (pinned.image, pinned.pool, pinned.slots) == ("registry.example/train:1", "gpu", 2)
    assert in_place.code == path.code
    # The resolved spec round-trips through JSON as a tool argument.
    assert TaskSpec.model_validate(json.loads(pinned.model_dump_json())) == pinned
