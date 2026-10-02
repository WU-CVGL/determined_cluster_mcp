"""The rendered entrypoint runs no user statement after a failed prelude."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest

from determined_compute.compute import ComputeService

render = ComputeService._render_entrypoint

Shell = Tuple[str, str]
SHELLS = [
    pytest.param(("sh", "-c"), id="sh-c"),
    pytest.param(
        ("bash", "-lc"),
        id="bash-lc",
        marks=pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed"),
    ),
]
FORMS = {
    "sequence": "touch {a}; touch {b}",
    "or-list": "false || touch {b}",
    "two-lines": "touch {a}\ntouch {b}",
    "background": "touch {a} & touch {b}; wait",
}


def test_string_command_bytes() -> None:
    assert render("a; b\nc & d; wait", "/shared/app", "/shared/out") == (
        "mkdir -p /shared/out && cd /shared/app || exit $?\na; b\nc & d; wait"
    )


def test_list_command_bytes_keep_quoting() -> None:
    assert render(["python", "train.py", "--name", "space value", "it's"], "/a b", "/o;x") == (
        "mkdir -p '/o;x' && cd '/a b' || exit $?\n"
        "python train.py --name 'space value' 'it'\"'\"'s'"
    )


def isolated_env(tmp_path: Path) -> Dict[str, str]:
    """Only PATH and a fresh HOME, so no user profile is read."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    directories = []
    for program in ("sh", "touch", "mkdir"):
        found = shutil.which(program)
        if found is None:
            pytest.skip(f"{program} is not installed")
        if os.path.dirname(found) not in directories:
            directories.append(os.path.dirname(found))
    return {"PATH": os.pathsep.join(directories), "HOME": str(home)}


def run_shell(shell: Shell, text: str, env: Dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    program, flag = shell
    executable = shutil.which(program)
    assert executable is not None
    return subprocess.run(
        [executable, flag, text], env=env, cwd=cwd, capture_output=True, text=True, timeout=60
    )


def marker_command(form: str, marks: Path) -> str:
    return FORMS[form].format(a=shlex.quote(str(marks / "a")), b=shlex.quote(str(marks / "b")))


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("form", sorted(FORMS))
@pytest.mark.parametrize("failing", ["cd", "mkdir"])
def test_failed_prelude_runs_no_user_statement(
    tmp_path: Path, shell: Shell, form: str, failing: str
) -> None:
    env = isolated_env(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    (tmp_path / "code").mkdir()
    (tmp_path / "file").write_text("")
    if failing == "cd":
        workdir, output_dir = tmp_path / "missing", tmp_path / "out"
    else:
        # A regular file stands where output_dir's parent must be.
        workdir, output_dir = tmp_path / "code", tmp_path / "file" / "out"
    text = render(marker_command(form, marks), str(workdir), str(output_dir))
    prelude = f"mkdir -p {shlex.quote(str(output_dir))} && cd {shlex.quote(str(workdir))}"

    prelude_status = run_shell(shell, prelude, env, tmp_path).returncode
    result = run_shell(shell, text, env, tmp_path)

    assert prelude_status != 0
    assert result.returncode == prelude_status, result.stderr
    assert sorted(marks.iterdir()) == []


@pytest.mark.parametrize("shell", SHELLS)
def test_successful_prelude_runs_command_in_workdir(tmp_path: Path, shell: Shell) -> None:
    env = isolated_env(tmp_path)
    code = tmp_path / "code dir"
    code.mkdir()
    output = tmp_path / "outputs" / "run 1"
    marks = tmp_path / "marks"
    marks.mkdir()
    command = f"pwd -P > {shlex.quote(str(marks / 'pwd'))}; {marker_command('two-lines', marks)}\nexit 7"

    result = run_shell(shell, render(command, str(code), str(output)), env, tmp_path)

    assert result.returncode == 7, result.stderr
    assert output.is_dir()
    assert (marks / "pwd").read_text().strip() == os.path.realpath(code)
    assert (marks / "a").exists() and (marks / "b").exists()
