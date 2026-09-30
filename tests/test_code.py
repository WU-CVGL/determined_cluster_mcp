from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
from pathlib import Path

import pytest

from determined_compute import code
from determined_compute.code import (
    MAX_CONTEXT_SIZE,
    CodeError,
    check_context_size,
    harness_size,
    plan_context,
    plan_git,
    plan_path,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
needs_lfs = pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is required")

ROOTS = ["/shared"]


def git(repo, *args):
    """Run git for fixtures with a fixed identity and no user or system configuration."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
        }
    )
    completed = subprocess.run(
        [
            "git",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout.decode().strip()


def write(path: Path, content, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    path.chmod(mode)


def commit_all(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def lfs_pointer(content: bytes) -> bytes:
    oid = hashlib.sha256(content).hexdigest()
    return (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content)}\n"
    ).encode()


def lfs_object(repo: Path, content: bytes) -> Path:
    oid = hashlib.sha256(content).hexdigest()
    return repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root / "train.py", "print('train')\n")
    write(root / "docs" / "notes.md", "notes\n")
    write(root / "scripts" / "run.sh", "#!/bin/sh\necho run\n", 0o755)
    write(root / "tokenizer.py", "TOKENS = 1\n")
    write(root / ".env", "KEY=not-real\n")
    write(root / ".netrc", "machine example login not-real\n")
    write(root / "config" / "credentials.json", "{}\n")
    write(root / "cache" / "table.bin", "cached\n")
    os.symlink("../train.py", root / "scripts" / "train-link.py")
    commit_all(root, "initial")
    return root


def by_path(result):
    return {item["path"]: item for item in result.files}


def decoded(item) -> bytes:
    return base64.b64decode(item["content"])


def payload_bytes(result) -> bytes:
    return json.dumps(list(result.files), sort_keys=True).encode()


def raises(code_name, function, *args, **kwargs) -> CodeError:
    with pytest.raises(CodeError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code_name, caught.value
    return caught.value


# git source


def test_git_pins_a_full_sha_in_a_mounted_repository(repo):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1")

    result = plan_git(repo, "/shared/team/repo", ROOTS)
    tagged = plan_git(repo, "/shared/team/repo", ["/shared/"], revision="v1")
    short = plan_git(repo, "/shared/team/repo", ROOTS, revision=head[:7])

    assert result.source == "git" and result.repo == "/shared/team/repo"
    assert result.commit == tagged.commit == short.commit == head
    assert len(result.commit) == 40
    assert result.content_digest == head
    assert result.uses_lfs is False and result.warnings == ()


def test_git_rejects_a_commit_left_only_in_the_reflog(repo):
    write(repo / "train.py", "print('lost')\n")
    lost = commit_all(repo, "lost")
    git(repo, "reset", "-q", "--hard", "HEAD~1")

    error = raises("commit_not_on_ref", plan_git, repo, "/shared/repo", ROOTS, revision=lost)

    assert "alternates" in str(error) and "gc" in str(error)
    git(repo, "tag", "keep", lost)
    assert plan_git(repo, "/shared/repo", ROOTS, revision=lost).commit == lost


def test_git_rejects_a_commit_reachable_only_from_the_stash(repo):
    write(repo / "train.py", "print('stashed')\n")
    git(repo, "stash", "-q")

    raises("commit_not_on_ref", plan_git, repo, "/shared/repo", ROOTS, revision="refs/stash")
    raises("commit_not_on_ref", plan_git, repo, "/shared/repo", ROOTS, revision="stash@{0}")


def test_git_accepts_a_commit_on_a_remote_tracking_ref_and_a_bare_repository(repo, tmp_path):
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/work", head)
    git(repo, "checkout", "-q", "--detach")
    git(repo, "branch", "-q", "-D", "main")

    assert plan_git(repo, "/shared/repo", ROOTS).commit == head
    assert plan_git(bare, "/shared/bare.git", ROOTS).commit == head


def test_git_trusts_a_repository_owned_by_another_user_but_context_does_not(
    repo, tmp_path, monkeypatch
):
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    environment = code._git_environment
    monkeypatch.setattr(
        code,
        "_git_environment",
        lambda: {**environment(), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
    )
    head = git(repo, "rev-parse", "HEAD")

    assert plan_git(repo, "/shared/repo", ROOTS).commit == head
    assert plan_git(bare, "/shared/bare.git", ROOTS).commit == head
    # context runs git diff over the working tree, which would run the repository's filters.
    raises("invalid_repository", plan_context, repo)


def test_git_rejects_repositories_whose_objects_live_elsewhere(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked))
    separate = tmp_path / "separate"
    git(tmp_path, "clone", "-q", "--separate-git-dir", "sep.git", str(repo), "separate")
    shared = tmp_path / "shared-clone"
    git(tmp_path, "clone", "-q", "--shared", str(repo), str(shared))
    ordinary = tmp_path / "ordinary"
    git(tmp_path, "clone", "-q", str(repo), str(ordinary))

    for path in (linked, separate):
        error = raises("invalid_repository", plan_git, path, "/shared/work", ROOTS)
        assert "outside the repository" in str(error)
    error = raises("invalid_repository", plan_git, shared, "/shared/work", ROOTS)
    assert "alternates" in str(error)
    assert plan_git(ordinary, "/shared/work", ROOTS).commit == head
    # A context is packed locally, so a linked worktree is fine there.
    assert plan_context(linked).commit == head


def test_objects_missing_from_the_store_are_code_errors(repo, tmp_path):
    notes = git(repo, "rev-parse", "HEAD:docs/notes.md")
    write(repo / "train.py", "print('newer')\n")
    commit_all(repo, "newer")
    git(repo, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    git(tmp_path, "clone", "-q", "--filter=blob:none", repo.as_uri(), str(clone))

    # The older train.py was never fetched, and lazy fetching is off while planning.
    error = raises("git_failed", plan_context, clone, revision="HEAD~1")
    assert "missing" in str(error)
    assert plan_context(clone).commit == git(repo, "rev-parse", "HEAD")

    (repo / ".git" / "objects" / notes[:2] / notes[2:]).unlink()
    raises("git_failed", plan_git, repo, "/shared/repo", ROOTS)
    raises("git_failed", plan_context, repo)


def _unreachable_partial_clone(repo, tmp_path):
    """A blob:none clone whose promisor remote is gone; any contact with it leaves a mark."""
    git(repo, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    git(tmp_path, "clone", "-q", "--filter=blob:none", repo.as_uri(), str(clone))
    contacted = tmp_path / "contacted"
    upload = tmp_path / "upload-pack"
    upload.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(contacted))}\nexit 1\n")
    upload.chmod(0o755)
    git(clone, "config", "remote.origin.url", (tmp_path / "gone.git").as_uri())
    git(clone, "config", "remote.origin.uploadpack", str(upload))
    return clone, contacted


def _as_older_git(monkeypatch, trace=None):
    """Drop GIT_NO_LAZY_FETCH, which git before 2.44 ignores, and optionally trace git."""
    environment = code._git_environment

    def older():
        result = {k: v for k, v in environment().items() if k != "GIT_NO_LAZY_FETCH"}
        return {**result, "GIT_TRACE": str(trace)} if trace else result

    monkeypatch.setattr(code, "_git_environment", older)


def test_a_missing_blob_is_reported_without_attempting_a_fetch(repo, tmp_path, monkeypatch):
    write(repo / "train.py", "print('newer')\n")
    commit_all(repo, "newer")
    clone, contacted = _unreachable_partial_clone(repo, tmp_path)
    trace = tmp_path / "trace"
    _as_older_git(monkeypatch, trace)

    # The clone never fetched the older train.py.
    error = raises("git_failed", plan_context, clone, revision="HEAD~1")

    assert "missing" in str(error) and error.details["count"] == 1
    lines = trace.read_text().splitlines()
    assert any("rev-list" in line for line in lines)
    assert [line for line in lines if "run_command:" in line and " fetch " in line] == []
    assert not contacted.exists()
    assert plan_context(clone).commit == git(repo, "rev-parse", "HEAD")


def test_a_commit_missing_from_a_partial_clone_contacts_no_remote(repo, tmp_path, monkeypatch):
    clone, contacted = _unreachable_partial_clone(repo, tmp_path)
    write(repo / "train.py", "print('later')\n")
    later = commit_all(repo, "later")  # never fetched into the clone
    _as_older_git(monkeypatch)

    # Resolving it reads the missing commit; git starts a lazy fetch that no transport allows.
    raises("revision_not_found", plan_context, clone, revision=later)

    assert not contacted.exists()


@pytest.fixture
def git_reporting(tmp_path, monkeypatch):
    """Put first on PATH a git that reports a chosen version and otherwise runs the real one."""
    real = shutil.which("git")
    directory = tmp_path / "fake-git"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")

    def install(reported):
        script = directory / "git"
        script.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = --version ]; then printf '%s\\n' {shlex.quote(reported)}; exit 0; fi\n"
            f'exec {shlex.quote(real)} "$@"\n'
        )
        script.chmod(0o755)
        code._git_version.cache_clear()

    yield install
    code._git_version.cache_clear()


@pytest.mark.parametrize(
    "reported, found",
    [
        ("git version 2.31.1", "2.31.1"),
        ("git version 2.31.8.vendor.2 (Distro Git-1)", "2.31.8.vendor.2 (Distro Git-1)"),
        ("git version 2.4.0", "2.4.0"),  # compared as numbers, not text
        ("git version 1.99.0", "1.99.0"),
        ("not a version", "not a version"),
    ],
)
def test_planning_requires_git_2_32(repo, git_reporting, reported, found):
    git_reporting(reported)

    for plan in (lambda: plan_git(repo, "/shared/repo", ROOTS), lambda: plan_context(repo)):
        error = raises("git_too_old", plan)
        assert error.details == {"found": found, "required": "2.32"}
        assert f"needs git 2.32 or later; found {found}" in str(error)
    # Observing a path source is best effort, so it reports nothing instead.
    assert plan_path("/shared/repo", repo).observed_commit is None


@pytest.mark.parametrize(
    "reported",
    [
        "git version 2.32.0",
        "git version 2.39.3 (Apple Git-145)",
        "git version 2.45.1.windows.1",
        "git version 3.0.0",
    ],
)
def test_git_2_32_and_later_with_vendor_suffixes_are_accepted(repo, git_reporting, reported):
    git_reporting(reported)

    assert plan_git(repo, "/shared/repo", ROOTS).commit == git(repo, "rev-parse", "HEAD")


def test_the_git_version_is_read_once_per_process(repo, git_reporting, monkeypatch):
    git_reporting("git version 2.32.0")
    calls = []
    real_run = subprocess.run

    def record(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(code.subprocess, "run", record)
    plan_git(repo, "/shared/repo", ROOTS)
    plan_context(repo)

    assert [argv for argv in calls if argv[1:] == ["--version"]] == [["git", "--version"]]
    assert all("protocol.allow=never" in argv for argv in calls if argv[1:] != ["--version"])


def test_git_rejects_a_partial_clone(repo, tmp_path):
    git(repo, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    git(tmp_path, "clone", "-q", "--filter=blob:none", repo.as_uri(), str(clone))

    error = raises("partial_clone", plan_git, clone, "/shared/partial", ROOTS)

    assert "network" in str(error)


def test_git_requires_lfs_objects_for_pointers(repo):
    content = b"weights" * 100
    write(repo / "model.bin", lfs_pointer(content))
    commit = commit_all(repo, "pointer")

    error = raises("lfs_object_missing", plan_git, repo, "/shared/repo", ROOTS)
    assert error.details == {"commit": commit, "count": 1, "paths": ["model.bin"]}

    stored = lfs_object(repo, content)
    write(stored, content[:-1])  # an incomplete download is not the object
    raises("lfs_object_missing", plan_git, repo, "/shared/repo", ROOTS)

    write(stored, content)
    result = plan_git(repo, "/shared/repo", ROOTS)
    assert result.uses_lfs is True and result.commit == commit


def test_git_warns_about_submodules(repo):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
    git(repo, "commit", "-q", "-m", "submodule")

    result = plan_git(repo, "/shared/repo", ROOTS)

    assert [(item.code, item.paths) for item in result.warnings] == [
        ("submodule_not_checked_out", ("vendor/lib",))
    ]


@pytest.mark.parametrize("container", ["/other/repo", "/sharedx/repo", "/"])
def test_git_repo_must_lie_under_a_mounted_root(repo, container):
    error = raises("repo_not_mounted", plan_git, repo, container, ROOTS)
    assert error.details["roots"] == ROOTS


def test_git_paths_must_be_absolute_and_the_repository_itself(repo):
    for container in ("shared/repo", "/shared/../etc", ""):
        raises("invalid_path", plan_git, repo, container, ROOTS)
    raises("invalid_repository", plan_git, repo / "scripts", "/shared/repo/scripts", ROOTS)
    raises("invalid_repository", plan_git, repo / "missing", "/shared/missing", ROOTS)
    raises("invalid_repository", plan_context, repo / "docs")


@pytest.mark.parametrize(
    "revision", ["--output=/dev/null", "-h", "HEAD --all", "", "a\nb", "x" * 257, None]
)
def test_option_like_or_malformed_revisions_are_rejected_before_git_runs(
    repo, revision, monkeypatch
):
    def refuse(*_args, **_kwargs):
        raise AssertionError("git must not run")

    monkeypatch.setattr(code.subprocess, "run", refuse)
    raises("invalid_revision", plan_git, repo, "/shared/repo", ROOTS, revision=revision)
    raises("invalid_revision", plan_context, repo, revision=revision)
    raises("invalid_revision", code.resolve_commit, repo, revision)


def test_unknown_revision_is_reported(repo):
    raises("revision_not_found", plan_git, repo, "/shared/repo", ROOTS, revision="no-such-ref")
    raises("revision_not_found", plan_context, repo, revision="HEAD:train.py")


def test_git_runs_from_argv_with_a_clean_environment(repo, tmp_path, monkeypatch):
    head = git(repo, "rev-parse", "HEAD")
    calls = []
    real_run = subprocess.run

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_run(argv, **kwargs)

    # The caller's git environment and configuration never reach the planner's git.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bare'='true'")
    monkeypatch.setattr(code.subprocess, "run", record)

    result = plan_git(repo, "/shared/repo", ROOTS, revision="main")

    assert result.commit == head
    for argv, kwargs in calls:
        assert isinstance(argv, list) and argv[0] == "git" and "shell" not in kwargs
        assert kwargs["timeout"] == code.GIT_TIMEOUT_SECONDS
        environment = kwargs["env"]
        assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS", "HOME"} & set(environment)
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_TERMINAL_PROMPT"] == "0" and environment["LC_ALL"] == "C"
    resolve = next(argv for argv, _ in calls if "--verify" in argv)
    assert resolve[-2:] == ["--end-of-options", "main^{commit}"]


def test_git_ignores_replace_refs_as_the_container_clone_does(repo):
    head = git(repo, "rev-parse", "HEAD")
    write(repo / "model.bin", lfs_pointer(b"weights"))
    replacement = commit_all(repo, "replacement")
    git(repo, "reset", "-q", "--hard", head)
    git(repo, "replace", head, replacement)

    assert plan_git(repo, "/shared/repo", ROOTS).uses_lfs is False
    assert "model.bin" not in by_path(plan_context(repo))


def test_git_failures_map_to_stable_codes(repo, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("git")

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(code.subprocess, "run", missing)
    raises("git_unavailable", plan_git, repo, "/shared/repo", ROOTS)
    monkeypatch.setattr(code.subprocess, "run", slow)
    raises("git_timeout", plan_git, repo, "/shared/repo", ROOTS)


def test_path_source_is_unpinned():
    result = plan_path("/shared/work/run")

    assert (result.source, result.dir, result.content_digest) == (
        "path",
        "/shared/work/run",
        "unpinned",
    )
    assert (result.observed_commit, result.observed_dirty, result.verified) == (None, None, False)
    raises("invalid_path", plan_path, "relative/run")


def test_path_source_reports_the_work_tree_it_observed(repo, tmp_path, monkeypatch):
    environment = code._git_environment
    ceiling = {"GIT_CEILING_DIRECTORIES": str(tmp_path)}  # no repository above the test's
    monkeypatch.setattr(code, "_git_environment", lambda: {**environment(), **ceiling})
    head = git(repo, "rev-parse", "HEAD")

    def observed(local):
        result = plan_path("/shared/work", local)
        assert result.verified is False and result.content_digest == "unpinned"
        return result.observed_commit, result.observed_dirty

    assert observed(repo) == observed(repo / "docs") == (head, False)
    write(repo / "new.txt", "untracked files run in place too\n")
    assert observed(repo) == (head, True)
    assert observed(repo / "docs") == (head, False)
    os.remove(repo / "new.txt")
    write(repo / "docs" / "notes.md", "edited\n")
    assert observed(repo) == observed(repo / "docs") == (head, True)
    (tmp_path / "plain").mkdir()
    assert observed(tmp_path / "plain") == observed(tmp_path / "missing") == (None, None)


# context source: payload and identity


def test_context_payload_mirrors_the_harness_file_list(repo):
    result = plan_context(repo)
    files = by_path(result)

    assert [item["path"] for item in result.files] == sorted(files)
    assert all(
        set(item) == {"path", "type", "content", "mtime", "mode", "uid", "gid"}
        and item["mtime"] == "1501632000"
        and item["uid"] == item["gid"] == 0
        for item in result.files
    )
    assert files["scripts"] == {**files["scripts"], "type": ord("5"), "content": "", "mode": 0o755}
    assert (files["train.py"]["type"], files["train.py"]["mode"]) == (ord("0"), 0o644)
    assert decoded(files["train.py"]) == b"print('train')\n"
    assert files["scripts/run.sh"]["mode"] == 0o755
    assert files["scripts/train-link.py"]["type"] == ord("2")
    assert decoded(files["scripts/train-link.py"]) == b"../train.py"
    assert result.source == "context" and result.commit == git(repo, "rev-parse", "HEAD")
    assert result.dirty is False
    assert result.excluded == (
        {"path": ".env", "reason": "secret_like", "rule": ".env*"},
        {"path": ".netrc", "reason": "secret", "rule": ".netrc"},
        {"path": "cache/table.bin", "reason": "cache", "rule": "cache/"},
        {"path": "config/credentials.json", "reason": "secret_like", "rule": "*credential*"},
    )
    assert not {".env", ".netrc", "cache/table.bin", "config/credentials.json"} & set(files)

    provenance = json.loads(decoded(files[".code-provenance.json"]))
    assert provenance == {
        "commit": result.commit,
        "dirty": False,
        "included": [],
        "excluded": list(result.excluded),
        "skipped": [],
    }
    assert result.manifest == tuple(
        {
            "path": item["path"],
            "type": {48: "file", 50: "symlink", 53: "dir"}[item["type"]],
            "mode": f"{item['mode']:04o}",
            "sha256": hashlib.sha256(decoded(item)).hexdigest(),
        }
        for item in result.files
    )
    canonical = json.dumps(list(result.manifest), sort_keys=True, separators=(",", ":"))
    assert result.content_digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert result.size == sum(len(f["content"]) // 4 * 3 for f in result.files if f["content"])


def test_context_payload_extracts_as_the_tree(repo, tmp_path):
    result = plan_context(repo, include=["docs"])
    buffer = io.BytesIO()
    # The master's archive package writes each item this way; a symlink's content is its target.
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in result.files:
            info = tarfile.TarInfo(item["path"])
            info.type, info.mode = bytes([item["type"]]), item["mode"]
            data = decoded(item)
            if info.type == tarfile.SYMTYPE:
                info.linkname, data = data.decode(), b""
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    target = tmp_path / "extracted"
    with tarfile.open(fileobj=buffer) as archive:
        archive.extractall(target, filter="data")

    assert (target / "scripts" / "train-link.py").read_text() == "print('train')\n"
    assert os.access(target / "scripts" / "run.sh", os.X_OK)
    assert json.loads((target / ".code-provenance.json").read_text())["included"] == [
        "docs/notes.md"
    ]


def test_unchanged_tree_gives_identical_payload_and_digest(repo):
    write(repo / "extra" / "data.txt", "abc\n")
    first = plan_context(repo, include=["extra"])
    for path in (repo / "train.py", repo / "extra" / "data.txt", repo / "extra"):
        os.utime(path, (1, 1))
    git(repo, "status", "--porcelain")
    second = plan_context(repo, include=["extra"])

    assert second.content_digest == first.content_digest
    assert payload_bytes(second) == payload_bytes(first)

    write(repo / "extra" / "data.txt", "abd\n")
    included_change = plan_context(repo, include=["extra"])
    write(repo / "extra" / "data.txt", "abc\n")
    write(repo / "tokenizer.py", "TOKENS = 2\n")
    commit_all(repo)
    committed_change = plan_context(repo, include=["extra"])

    assert included_change.content_digest != first.content_digest
    assert committed_change.content_digest not in {
        first.content_digest,
        included_change.content_digest,
    }


def test_included_file_modes_follow_the_executable_bit_only(repo):
    # Untracked files only: changing a tracked file's exec bit would make the tree dirty.
    write(repo / "extra" / "u.txt", "u\n", 0o644)
    write(repo / "extra" / "p.txt", "p\n", 0o644)
    write(repo / "extra" / "x.sh", "x\n", 0o755)
    base = plan_context(repo, include=["extra"])
    for name, mode in (("u.txt", 0o664), ("p.txt", 0o600), ("x.sh", 0o775)):
        (repo / "extra" / name).chmod(mode)
    other = plan_context(repo, include=["extra"])
    files = by_path(other)

    assert [files[f"extra/{name}"]["mode"] for name in ("u.txt", "p.txt", "x.sh")] == [
        0o644,
        0o644,
        0o755,
    ]
    assert other.content_digest == base.content_digest
    assert payload_bytes(other) == payload_bytes(base)


def test_dirty_records_a_working_tree_that_differs_from_the_revision(repo):
    clean = plan_context(repo)
    write(repo / "train.py", "print('edited')\n")
    dirty = plan_context(repo)

    assert (clean.dirty, dirty.dirty) == (False, True)
    # Tracked content still comes from the revision; only the provenance changes.
    assert decoded(by_path(dirty)["train.py"]) == b"print('train')\n"
    assert dirty.content_digest != clean.content_digest


def test_a_smudged_lfs_file_is_not_dirty(repo):
    content = b"weights" * 100
    write(repo / ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    write(repo / "data.bin", lfs_pointer(content))
    commit_all(repo, "pointer")
    pointer = plan_context(repo)
    # What git lfs leaves in the work tree; the planner's git has no LFS filter.
    write(repo / "data.bin", content)
    smudged = plan_context(repo)

    assert (pointer.dirty, smudged.dirty) == (False, False)
    assert smudged.content_digest == pointer.content_digest
    for changed in (content + b"!", content[:-1] + b"?"):
        write(repo / "data.bin", changed)
        assert plan_context(repo).dirty is True


@needs_lfs
def test_an_lfs_tree_reads_clean_whether_or_not_its_index_is_fresh(tmp_path):
    root = tmp_path / "lfs"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "lfs", "install", "--local")
    git(root, "lfs", "track", "*.bin")
    write(root / "model.bin", b"weights\n")
    commit_all(root, "model")
    assert git(root, "cat-file", "-p", "HEAD:model.bin").startswith("version https://git-lfs")
    os.utime(root / "model.bin", (1, 1))  # stale index data, as after touch or a racy commit
    stale = plan_context(root)
    git(root, "status", "--porcelain")  # refreshes the index through the LFS filter
    fresh = plan_context(root)

    assert (stale.dirty, fresh.dirty) == (False, False)
    assert stale.content_digest == fresh.content_digest


@pytest.mark.parametrize("kind", ["clean", "process"])
def test_planning_runs_no_filter_the_repository_configures(repo, tmp_path, kind):
    marker = tmp_path / "marker"
    # A driver name may contain '=', which a -c override would split at.
    (repo / ".git" / "info" / "attributes").write_text("*.py filter=x=y\n*.md filter=evil\n")
    command = f"sh -c 'touch {shlex.quote(str(marker))}; cat'"
    git(repo, "config", f"filter.x=y.{kind}", command)
    git(repo, "config", f"filter.evil.{kind}", command)
    git(repo, "config", "filter.evil.required", "true")
    for path in (repo / "train.py", repo / "docs" / "notes.md"):
        os.utime(path, (1, 1))  # stale index data makes git hash the file again

    assert plan_context(repo).dirty is False
    assert plan_path("/shared/repo", repo).observed_dirty is False
    assert not marker.exists()


def test_context_takes_tracked_files_from_the_named_revision(repo):
    parent = git(repo, "rev-parse", "HEAD")
    write(repo / "train.py", "print('newer')\n")
    commit_all(repo, "newer")

    older = plan_context(repo, revision="HEAD~1")

    assert older.commit == parent
    assert decoded(by_path(older)["train.py"]) == b"print('train')\n"
    assert older.dirty is True  # the working tree holds the newer commit


def test_manifest_digest_is_over_utf8_json(repo):
    write(repo / "données.txt", "bonjour\n")
    commit_all(repo, "non-ascii name")

    result = plan_context(repo)

    assert decoded(by_path(result)["données.txt"]) == b"bonjour\n"
    canonical = json.dumps(
        list(result.manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert result.content_digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# context source: size


def test_context_size_counts_like_the_harness_at_its_limit():
    assert MAX_CONTEXT_SIZE == 99_614_720
    assert check_context_size({"big.bin": 99_614_718}) == 99_614_718
    # base64 rounds each file up to a multiple of 3, so these fail although they fit in bytes.
    for size in (99_614_719, 99_614_720):
        error = raises("context_too_large", check_context_size, {"big.bin": size})
        assert error.details["total"] == 99_614_721
        assert error.details["limit"] == MAX_CONTEXT_SIZE
    error = raises("context_too_large", check_context_size, {"a": 99_614_717, "b": 1})
    assert error.details["total"] == 99_614_721
    assert error.details["largest"] == [
        {"path": "a", "bytes": 99_614_717},
        {"path": "b", "bytes": 1},
    ]
    assert "git" in error.details["hint"]
    assert check_context_size({"a": 1, "b": 1, "c": 1}) == 9
    assert [harness_size(size) for size in range(5)] == [0, 3, 3, 3, 6]


def test_context_size_counts_every_record_with_content_as_the_harness_does(repo):
    # The fixture's scripts/train-link.py is a relative in-tree symlink.
    result = plan_context(repo)
    files = result.files
    regular = sum(len(f["content"]) // 4 * 3 for f in files if f["type"] == ord("0"))

    assert result.size == sum(len(f["content"]) // 4 * 3 for f in files if f["content"])
    assert result.size == regular + harness_size(len(b"../train.py"))
    # Exactly at the limit passes; the files alone, without the link's target, do not.
    assert plan_context(repo, limit=result.size).size == result.size
    error = raises("context_too_large", plan_context, repo, limit=result.size - 1)
    assert error.details["reason"] == "content_size"
    assert error.details["total"] == result.size
    raises("context_too_large", plan_context, repo, limit=regular)


def test_working_tree_symlinks_count_toward_the_early_limit(repo):
    for index in range(4):
        os.symlink("../train.py", repo / "scripts" / f"link-{index}.py")
    once = plan_context(repo, include=["scripts"])

    assert once.size == sum(len(f["content"]) // 4 * 3 for f in once.files if f["content"])
    # The walk stops at the second link: 12 + 12 counted bytes are over 20.
    error = raises("context_too_large", plan_context, repo, include=["scripts"], limit=20)
    assert error.details == {**error.details, "reason": "content_size", "total": 24}


def test_context_size_limit_applies_to_real_files(repo):
    write(repo / "data" / "big.bin", b"\0" * 5000)
    baseline = plan_context(repo, include=["data"])

    assert plan_context(repo, include=["data"], limit=baseline.size).size == baseline.size
    error = raises(
        "context_too_large", plan_context, repo, include=["data"], limit=baseline.size - 1
    )
    assert error.details["total"] == baseline.size
    assert error.details["largest"][0] == {"path": "data/big.bin", "bytes": 5000}


def test_working_tree_includes_stop_at_the_limit(repo):
    for index in range(5):
        write(repo / "data" / f"{index}.bin", b"\0" * 3000)
    once = plan_context(repo, include=["data"])

    # Counted once, although the nested include visits data/0.bin again.
    assert plan_context(repo, include=["data", "data/0.bin"], limit=once.size).size == once.size
    # The walk stops at the second file instead of enumerating the rest.
    error = raises("context_too_large", plan_context, repo, include=["data"], limit=5000)
    assert error.details["total"] == 6000 and error.details["limit"] == 5000


def test_a_working_tree_include_may_shrink_a_tracked_file(repo):
    write(repo / "big.bin", b"\0" * 6000)
    commit_all(repo, "big")
    write(repo / "big.bin", b"\0" * 30)
    shrunk = plan_context(repo, include=["big.bin"])

    assert plan_context(repo, include=["big.bin"], limit=shrunk.size).size == shrunk.size
    raises("context_too_large", plan_context, repo, limit=shrunk.size)


def test_context_size_rounds_every_file(tmp_path):
    root = tmp_path / "tiny"
    root.mkdir()
    git(root, "init", "-q")
    for name in "abc":
        write(root / name, "x")
    commit_all(root, "tiny")
    result = plan_context(root)
    provenance = len(decoded(by_path(result)[".code-provenance.json"]))

    assert result.size == 9 + harness_size(provenance)
    # The raw bytes (3 + provenance) fit this limit; the harness count does not.
    raises("context_too_large", plan_context, root, limit=8 + harness_size(provenance))


# context source: symlinks


@pytest.mark.parametrize(
    "links",
    [
        {"scripts/escape": "/etc/hosts"},
        {"scripts/escape": "../../outside"},
        # Each target stays inside on its own; followed through the first link, it does not.
        {"a/b/up": "../..", "escape": "a/b/up/../.."},
        {"loop-a": "loop-b", "loop-b": "loop-a"},
        # The harness also normalizes Windows separators and rejects the whole archive.
        {"scripts/windows": "..\\..\\outside"},
        # ntpath.splitdrive and, before Python 3.13, ntpath.isabs treat these as absolute.
        {"scripts/drive": "C:outside"},
        {"scripts/rooted": "\\outside"},
    ],
    ids=["absolute", "parent", "chain", "loop", "windows", "drive", "rooted"],
)
def test_unsafe_symlinks_are_plan_errors(repo, links):
    for path, target in links.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / path)
    commit_all(repo, "links")

    error = raises("unsafe_symlink", plan_context, repo)

    assert error.details["path"] in links


def test_symlink_chains_that_stay_inside_are_kept(repo):
    for path, target in {"a/b/up": "../..", "alias.py": "a/b/up/train.py"}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / path)
    commit_all(repo, "links")

    files = by_path(plan_context(repo))

    assert decoded(files["alias.py"]) == b"a/b/up/train.py"
    assert files["a/b/up"]["type"] == ord("2")


def test_a_symlink_named_by_an_include_is_checked_as_a_link(repo, tmp_path):
    write(tmp_path / "outside" / "key.txt", "outside\n")
    os.symlink(tmp_path / "outside", repo / "linked")
    os.symlink("../outside/key.txt", repo / "model.cfg")
    os.symlink("train.py", repo / "alias.py")

    raises("unsafe_symlink", plan_context, repo, include=["linked"])
    raises("unsafe_symlink", plan_context, repo, include=["model.cfg"])
    files = by_path(plan_context(repo, include=["alias.py"]))
    assert files["alias.py"]["type"] == ord("2")
    assert decoded(files["alias.py"]) == b"train.py"


def test_working_tree_symlinks_are_checked_too(repo):
    write(repo / "gen" / "table.json", "{}\n")
    os.symlink("table.json", repo / "gen" / "alias.json")
    assert decoded(by_path(plan_context(repo, include=["gen"]))["gen/alias.json"]) == b"table.json"

    os.symlink("/etc", repo / "gen" / "system")
    raises("unsafe_symlink", plan_context, repo, include=["gen"])
    # A cache directory is pruned before its links are looked at.
    os.unlink(repo / "gen" / "system")
    os.makedirs(repo / ".venv" / "bin")
    os.symlink("/usr/bin/python3", repo / ".venv" / "bin" / "python")
    result = plan_context(repo, include=["."])
    assert {"path": ".venv/", "reason": "cache", "rule": ".venv/"} in result.excluded


# context source: rules and includes


def test_hard_secret_rules_are_never_uploaded(repo):
    write(repo / ".ssh" / "config", "Host example\n")
    write(repo / "cluster.conf", "DET_PASSWORD=not-real\n")
    commit_all(repo, "secrets")

    for include in ([], [".netrc", ".ssh/config", "cluster.conf"], [".ssh"], ["."]):
        result = plan_context(repo, include=include, secrets_file=repo / "cluster.conf")
        files = by_path(result)
        assert not {".netrc", ".ssh/config", "cluster.conf"} & set(files), include
        rules = {item["path"]: (item["reason"], item["rule"]) for item in result.excluded}
        assert rules[".netrc"] == ("secret", ".netrc")
        assert rules[".ssh/config"] == ("secret", ".ssh/")
        assert rules["cluster.conf"] == ("secret", "configured secrets file")
        assert not any(item.code == "secret_like_included" for item in result.warnings)


def test_secret_rules_ignore_case(repo):
    for path in ("ID_RSA", ".SSH/config", "certs/server.PEM"):
        write(repo / path, "not-real\n")
    commit_all(repo, "upper case")

    result = plan_context(repo, include=[".SSH"])
    rules = {item["path"]: (item["reason"], item["rule"]) for item in result.excluded}

    assert not {"ID_RSA", ".SSH/config", "certs/server.PEM"} & set(by_path(result))
    assert rules["ID_RSA"] == ("secret", "id_rsa")
    assert rules[".SSH/config"] == ("secret", ".ssh/")
    assert rules["certs/server.PEM"] == ("secret_like", "*.pem")
    # On a case-insensitive filesystem this include would read .ssh itself.
    assert rules[".SSH/"] == ("secret", ".ssh/")


def test_other_names_for_the_secrets_file_are_hard_secrets(repo, tmp_path):
    inside = repo / "cluster.conf"
    write(inside, "DET_PASSWORD=not-real\n")
    write(repo / "CLUSTER.CONF", "DET_PASSWORD=not-real\n")
    outside = tmp_path / "private" / "compute.env"
    write(outside, "DET_PASSWORD=not-real\n")
    os.link(outside, repo / "notes.txt")
    os.link(outside, repo / "prod.env")
    secret = ("secret", "configured secrets file")

    upper = plan_context(repo, include=["CLUSTER.CONF"], secrets_file=inside)
    assert {"path": "CLUSTER.CONF", "reason": secret[0], "rule": secret[1]} in upper.excluded
    named = plan_context(repo, include=["notes.txt", "prod.env"], secrets_file=outside)
    walked = plan_context(repo, include=["."], secrets_file=outside)

    for result in (named, walked):
        rules = {item["path"]: (item["reason"], item["rule"]) for item in result.excluded}
        assert not {"notes.txt", "prod.env"} & set(by_path(result))
        assert rules["notes.txt"] == secret
    # Found by name first, a hard link is excluded all the same, and never as a soft include.
    assert {"path": "prod.env", "reason": "secret_like", "rule": "*.env"} in walked.excluded
    assert {"path": "prod.env", "reason": secret[0], "rule": secret[1]} in named.excluded
    assert named.warnings == ()


def test_credential_stores_are_hard_secrets(repo):
    stores = (
        ".kube/config",
        ".docker/config.json",
        ".config/gh/hosts.yml",
        ".gnupg/pubring.kbx",
        ".git-credentials",
    )
    for path in stores:
        write(repo / path, "not-real\n")

    walked = plan_context(repo, include=["."])
    named = plan_context(repo, include=[".git-credentials", ".docker/config.json"])
    git(repo, "add", ".docker/config.json")
    git(repo, "commit", "-q", "-m", "docker config")
    tracked = plan_context(repo)

    assert not set(stores) & set(by_path(walked))
    rules = {item["path"]: (item["reason"], item["rule"]) for item in walked.excluded}
    for key in (".kube/", ".docker/config.json", ".config/gh/", ".gnupg/", ".git-credentials"):
        assert rules[key] == ("secret", key)
    assert not {".git-credentials", ".docker/config.json"} & set(by_path(named))
    assert named.warnings == ()
    assert {"path": ".docker/config.json", "reason": "secret", "rule": ".docker/config.json"} in (
        tracked.excluded
    )


def test_key_containers_are_secret_like(repo):
    write(repo / "keys" / "x.p12", b"\x30\x82")
    write(repo / "keys" / "x.ppk", "PuTTY-User-Key-File-3: not-real\n")

    walked = plan_context(repo, include=["keys"])
    named = plan_context(repo, include=["keys/x.p12", "keys/x.ppk"])

    assert {"path": "keys/x.p12", "reason": "secret_like", "rule": "*.p12"} in walked.excluded
    assert {"path": "keys/x.ppk", "reason": "secret_like", "rule": "*.ppk"} in walked.excluded
    assert {"keys/x.p12", "keys/x.ppk"} <= set(by_path(named))
    assert [(item.code, item.paths) for item in named.warnings] == [
        ("secret_like_included", ("keys/x.p12", "keys/x.ppk"))
    ]


def test_soft_secret_rules_yield_only_to_an_include_that_names_the_file(repo):
    write(repo / "src" / "secrets.py", "def load():\n    return None\n")
    commit_all(repo, "module")
    soft = {"path": "src/secrets.py", "reason": "secret_like", "rule": "*secret*"}

    default = plan_context(repo)
    walked = plan_context(repo, include=["src"])
    named = plan_context(repo, include=["src/secrets.py", "config/credentials.json"])

    assert soft in default.excluded and soft in walked.excluded
    assert "src/secrets.py" not in by_path(walked)
    assert decoded(by_path(named)["src/secrets.py"]) == b"def load():\n    return None\n"
    assert "config/credentials.json" in by_path(named)
    assert soft not in named.excluded
    assert named.included == ("config/credentials.json", "src/secrets.py")
    assert [(item.code, item.paths) for item in named.warnings] == [
        ("secret_like_included", ("config/credentials.json", "src/secrets.py"))
    ]


def test_includes_add_working_tree_files_over_the_revision(repo):
    write(repo / "train.py", "print('edited')\n", 0o755)
    write(repo / "generated" / "table.json", "{}\n")
    write(repo / "generated" / "__pycache__" / "x.pyc", "bytecode")
    write(repo / "generated" / "empty" / ".keep", "")
    os.remove(repo / "generated" / "empty" / ".keep")
    write(repo / "cache" / "table.bin", "restored\n")

    result = plan_context(repo, include=["train.py", "./generated", "cache/table.bin"])
    files = by_path(result)

    assert decoded(files["train.py"]) == b"print('edited')\n"
    assert files["train.py"]["mode"] == 0o755
    assert decoded(files["cache/table.bin"]) == b"restored\n"
    assert files["generated/empty"]["type"] == ord("5")
    assert "generated/__pycache__/x.pyc" not in files
    assert {"path": "generated/__pycache__/", "reason": "cache", "rule": "__pycache__/"} in (
        result.excluded
    )
    assert not any(item["path"] == "cache/table.bin" for item in result.excluded)
    assert result.included == ("cache/table.bin", "generated/table.json", "train.py")
    assert result.dirty is True


def test_exclude_patterns_and_git_metadata(repo):
    write(repo / "vendor" / "lib" / ".git", "gitdir: ../../.git/modules/lib\n")
    write(repo / "vendor" / "lib" / "code.py", "VALUE = 1\n")

    result = plan_context(repo, include=["."], exclude=["docs/", "*.md"])

    assert "docs/notes.md" not in by_path(result)
    # The tracked file and the walked directory are each reported with the rule that hit them.
    assert {"path": "docs/notes.md", "reason": "exclude_pattern", "rule": "*.md"} in (
        result.excluded
    )
    assert {"path": "docs/", "reason": "exclude_pattern", "rule": "docs/"} in result.excluded
    assert result.skipped == (
        {"path": ".git", "reason": "git_metadata"},
        {"path": "vendor/lib/.git", "reason": "git_metadata"},
    )
    assert "vendor/lib/code.py" in by_path(result)


def test_a_leading_slash_anchors_an_exclude_at_the_root(repo):
    write(repo / "data" / "a.txt", "a\n")
    write(repo / "src" / "data" / "b.txt", "b\n")
    commit_all(repo, "data")

    anchored = plan_context(repo, exclude=["/data/"])
    anywhere = plan_context(repo, exclude=["data/"])

    assert "data/a.txt" not in by_path(anchored)
    assert "src/data/b.txt" in by_path(anchored)
    assert {"path": "data/a.txt", "reason": "exclude_pattern", "rule": "/data/"} in (
        anchored.excluded
    )
    assert not {"data/a.txt", "src/data/b.txt"} & set(by_path(anywhere))


def test_duplicate_includes_are_walked_once(repo, monkeypatch):
    write(repo / "data" / "a.txt", "a\n")
    walked = []
    walk = code._Context._walk

    def record(self, top):
        walked.append(top)
        walk(self, top)

    monkeypatch.setattr(code._Context, "_walk", record)
    base = plan_context(repo, include=["."])

    for includes in ([".", "."], [".", "./", "./data/.", "data", "data/"]):
        walked.clear()
        result = plan_context(repo, include=includes)
        assert (result.files, result.content_digest) == (base.files, base.content_digest)
        assert len(walked) == len(set(walked))
    # A nested include is kept: naming a directory restores what its parent's rules dropped.
    nested = plan_context(repo, include=[".", "data"])
    assert nested.content_digest == base.content_digest


@pytest.mark.parametrize("swap", ["symlink", "fifo", "grown"])
def test_a_file_changed_after_the_walk_is_not_read(repo, tmp_path, monkeypatch, swap):
    write(tmp_path / "outside" / "id_rsa", "PRIVATE\n")
    write(repo / "gen" / "table.json", "{}\n")
    target = repo / "gen" / "table.json"
    check = code._check_paths

    def changed(context):
        if swap == "grown":
            write(target, "{}\n" * 10)
        else:
            target.unlink()
            if swap == "symlink":
                os.symlink(tmp_path / "outside" / "id_rsa", target)
            else:
                os.mkfifo(target)
        return check(context)

    def blocked(*_args):
        raise AssertionError("reading the changed file blocked")

    monkeypatch.setattr(code, "_check_paths", changed)
    previous = signal.signal(signal.SIGALRM, blocked)
    signal.alarm(20)
    try:
        error = raises("invalid_include", plan_context, repo, include=["gen"])
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert "gen/table.json changed" in str(error)


@pytest.mark.parametrize("name", ["..\\evil.py", "C:evil.py"], ids=["windows-parent", "drive"])
def test_names_the_harness_would_reject_are_plan_errors(repo, name):
    write(repo / name, "x = 1\n")
    commit_all(repo, "windows name")

    raises("unsafe_path", plan_context, repo)


@pytest.mark.parametrize(
    "include", ["../outside.txt", "/etc/hosts", ".git/HEAD", "missing.txt", "linked/file.txt"]
)
def test_includes_must_stay_inside_the_tree(repo, tmp_path, include):
    write(tmp_path / "outside" / "file.txt", "outside\n")
    write(tmp_path / "outside.txt", "outside\n")
    os.symlink(tmp_path / "outside", repo / "linked")

    raises("invalid_include", plan_context, repo, include=[include])


def test_include_and_exclude_lists_are_bounded(repo):
    raises("invalid_include", plan_context, repo, include=["train.py"] * 257)
    raises("invalid_include", plan_context, repo, include=["a" * 4097])
    raises("invalid_include", plan_context, repo, include="train.py")
    raises("invalid_exclude", plan_context, repo, exclude=["*"] * 65)


def test_context_warnings_and_reserved_names(repo):
    write(repo / "model.bin", lfs_pointer(b"weights"))
    write(repo / "startup-hook.sh", "export X=1\n")
    write(repo / ".code-provenance.json", "{}\n")
    head = commit_all(repo, "warnings")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
    git(repo, "commit", "-q", "-m", "submodule")

    result = plan_context(repo)

    assert [(item.code, item.paths) for item in result.warnings] == [
        ("lfs_pointer", ("model.bin",)),
        ("startup_hook", ("startup-hook.sh",)),
        ("submodule_not_checked_out", ("vendor/lib",)),
    ]
    assert result.skipped == (
        {"path": ".code-provenance.json", "reason": "reserved"},
        {"path": "vendor/lib", "reason": "submodule"},
    )
    provenance = json.loads(decoded(by_path(result)[".code-provenance.json"]))
    assert provenance["skipped"] == list(result.skipped)


def test_context_rules_cover_the_transfer_exclusions():
    from determined_compute.storage.service import _SYNC_EXCLUDES

    rules = set(code._CACHE_RULES) | set(code._HARD_SECRET_RULES) | set(code._SOFT_SECRET_RULES)
    assert set(_SYNC_EXCLUDES) <= rules
