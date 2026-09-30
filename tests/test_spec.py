"""Tests for the typed task specification and the code prelude."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from pydantic import ValidationError

from determined_compute import spec
from determined_compute.code import plan_context
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
        project="baselines",
    )

    assert task.slots == 0
    assert task.env == {"WANDB_PROJECT": "demo", "_EMPTY": ""}
    assert (task.image, task.pool, task.workspace, task.project) == (
        "registry.example/train:1",
        "gpu",
        "research",
        "baselines",
    )


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
    ],
    ids=["single", "search", "storage-path", "shared-fs", "nulls"],
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
    (experiment_fields(experiment={"searcher": {"metric": "loss"}}), "searcher.name is required"),
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

GIT_CLONE = (
    "git -c safe.directory=/shared/repo clone -q --shared --no-checkout -- /shared/repo "
    "/run/determined/code && "
)
CHECKOUT = "git -C /run/determined/code "
LFS = (
    "-c filter.lfs.process='git-lfs filter-process' -c filter.lfs.required=true "
    "-c lfs.fetchinclude= -c lfs.fetchexclude= "
)
LFS_CHECKOUT = "GIT_LFS_SKIP_SMUDGE=0 " + CHECKOUT + LFS


@pytest.mark.parametrize(
    "workdir, uses_lfs, expected",
    [
        (
            ".",
            False,
            GIT_CLONE + CHECKOUT + f"checkout -q --detach {SHA} && mkdir -p -- /shared/out "
            "&& cd -- /run/determined/code",
        ),
        (
            ".",
            True,
            GIT_CLONE + LFS_CHECKOUT + f"checkout -q --detach {SHA} && mkdir -p -- /shared/out "
            "&& cd -- /run/determined/code",
        ),
        (
            "src/pkg",
            False,
            GIT_CLONE + CHECKOUT + f"checkout -q --detach {SHA} && mkdir -p -- /shared/out "
            "&& cd -- /run/determined/code/src/pkg",
        ),
        (
            "./src//pkg/",
            True,
            GIT_CLONE + LFS_CHECKOUT + f"checkout -q --detach {SHA} && mkdir -p -- /shared/out "
            "&& cd -- /run/determined/code/src/pkg",
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
        ("context", "src/pkg", "mkdir -p -- /shared/out && cd -- /run/determined/workdir/src/pkg"),
        ("path", ".", "mkdir -p -- /shared/out && cd -- /shared/app"),
        ("path", "src/pkg", "mkdir -p -- /shared/out && cd -- /shared/app/src/pkg"),
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
        f"git -c safe.directory={quoted_repo} clone -q --shared --no-checkout -- "
        f"{quoted_repo} /run/determined/code && {LFS_CHECKOUT}checkout -q --detach {SHA} "
        f"&& mkdir -p -- {quoted_output} && cd -- '/run/determined/code/sub dir/\"q\"'"
    )
    assert context == (
        f"mkdir -p -- {quoted_output} && cd -- '/run/determined/workdir/sub dir/\"q\"'"
    )
    quoted_target = "'/srv/it'\"'\"'s a $repo; x\ny/sub dir/\"q\"'"
    assert path == f"mkdir -p -- {quoted_output} && cd -- {quoted_target}"


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
    return {"PATH": search_path("sh", "touch", "mkdir", "cat", *programs), "HOME": str(home)}


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


def _source_repository(tmp_path: Path, env: Dict[str, str]) -> Tuple[Path, str]:
    repo = tmp_path / "shared repo's"
    (repo / "pkg").mkdir(parents=True)
    git(env, repo, "init", "-q", "-b", "main")
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
    tools = tmp_path / "tools"
    tools.mkdir()
    for program in ("git", "mkdir", "touch"):
        wrapper = tools / program
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(shutil.which(program))} "$@"\n')
        wrapper.chmod(0o755)
    marks = tmp_path / "marks"
    marks.mkdir()
    prelude = render_prelude(
        "git", output_dir=str(tmp_path / "out"), location=str(repo), commit=pinned, uses_lfs=True
    )

    result = run_shell(
        ("sh", "-c"),
        render_entrypoint(prelude, marker_command("sequence", marks)),
        {**env, "PATH": str(tools)},
        tmp_path,
    )

    assert result.returncode != 0
    assert "git-lfs" in result.stderr
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
