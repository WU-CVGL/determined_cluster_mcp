from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from determined_compute import compute_cli
from determined_compute.compute import ComputeProfile
from determined_compute.storage import SSHConfig, StorageAccessConfig, StorageError, StorageService
from determined_compute.storage import snapshot as snapshot_module

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@pytest.fixture(autouse=True)
def writable_after_test(tmp_path):
    """Snapshot trees are read-only; let pytest remove its temporary directories later."""
    yield
    for directory, _dirnames, _filenames in os.walk(tmp_path):
        os.chmod(directory, 0o755)


def git(repo, *args):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    completed = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, env=environment,
    )
    return completed.stdout.decode().strip()


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root / "train.py", "print('train')\n")
    write(root / "shared.txt", "same content\n")
    write(root / "docs" / "notes.md", "same content\n")
    write(root / "scripts" / "run.sh", "#!/bin/sh\necho run\n", 0o755)
    write(root / "tokenizer.py", "TOKENS = 1\n")
    write(root / ".env", "KEY=not-real\n")
    write(root / "config" / "credentials.json", "{}\n")
    write(root / "api.token", "not-real\n")
    write(root / "cache" / "table.bin", "cached\n")
    os.symlink("../train.py", root / "scripts" / "train-link.py")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def storage_for(tmp_path, link_mode="hardlink", **access):
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    profile = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": str(shared), "container_path": "/shared"},
                {"host_path": str(tmp_path / "reference"), "container_path": "/reference",
                 "read_only": True},
            ],
            "defaults": {"image": "image", "pool": "pool"},
        }
    )
    config = StorageAccessConfig.from_dict(
        {"snapshots": {"root": "/shared/snapshots", "link_mode": link_mode}, **access}
    )
    return StorageService(profile, config, tmp_path / "unused-secrets.env")


def snapshot_root(tmp_path):
    return tmp_path / "shared" / "snapshots"


def test_dry_run_reports_identity_and_writes_nothing(tmp_path, repo):
    storage = storage_for(tmp_path)

    result = storage.snapshot(str(repo))

    assert result["dry_run"] is True
    assert result["revision"] == git(repo, "rev-parse", "HEAD")
    assert result["tree"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert result["workdir"] == f"/shared/snapshots/trees/{result['content_id']}"
    assert result["request_fields"] == {
        "workdir": result["workdir"],
        "code_revision": result["revision"],
    }
    assert result["manifest_path"] is None and result["manifest_sha256"] is None
    assert result["existing"] is False and result["tree_existing"] is False
    assert result["files"] == 5
    assert result["symlinks"] == 1
    assert result["new_objects"] == 4  # shared.txt and docs/notes.md are one object
    assert not snapshot_root(tmp_path).exists()


def test_execute_publishes_a_read_only_tree_and_manifest(tmp_path, repo):
    storage = storage_for(tmp_path)

    result = storage.snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    assert tree == snapshot_root(tmp_path) / "trees" / result["content_id"]
    assert (tree / "train.py").read_text() == "print('train')\n"
    assert stat.S_IMODE(os.stat(tree / "train.py").st_mode) == 0o444
    assert stat.S_IMODE(os.stat(tree / "scripts" / "run.sh").st_mode) == 0o555
    assert stat.S_IMODE(os.stat(tree / "scripts").st_mode) == 0o555
    assert stat.S_IMODE(os.stat(tree).st_mode) == 0o555
    assert os.readlink(tree / "scripts" / "train-link.py") == "../train.py"
    assert os.stat(tree / "shared.txt").st_ino == os.stat(tree / "docs" / "notes.md").st_ino
    manifest_file = snapshot_root(tmp_path) / "manifests" / f"{result['snapshot_key']}.json"
    payload = manifest_file.read_bytes()
    assert result["manifest_path"] == f"/shared/snapshots/manifests/{result['snapshot_key']}.json"
    assert result["manifest_sha256"] == hashlib.sha256(payload).hexdigest()
    manifest = json.loads(payload)
    assert manifest["schema_version"] == "determined-compute-snapshot-v1"
    assert manifest["content_id"] == result["content_id"]
    assert manifest["revision"] == result["revision"]
    assert manifest["files"]["scripts/run.sh"]["mode"] == "100755"
    assert manifest["symlinks"] == {"scripts/train-link.py": "../train.py"}
    assert manifest["sources"] == [
        {"kind": "git", "revision": result["revision"], "tree": result["tree"], "files": 5}
    ]
    assert "url" not in json.dumps(manifest)
    assert result["link_mode"] == "hardlink"
    assert result["new_objects"] == 4
    assert list((snapshot_root(tmp_path) / "tmp").iterdir()) == []


def test_repeated_snapshot_reuses_the_manifest_and_tree(tmp_path, repo):
    storage = storage_for(tmp_path)
    first = storage.snapshot(str(repo), dry_run=False)

    preview = storage.snapshot(str(repo))
    second = storage.snapshot(str(repo), dry_run=False, verify=True)

    assert preview["existing"] is True
    assert preview["manifest_sha256"] == first["manifest_sha256"]
    assert second["existing"] is True and second["tree_existing"] is True
    assert second["manifest_sha256"] == first["manifest_sha256"]
    assert second["new_objects"] == 0 and second["new_bytes"] == 0


def test_revisions_share_objects_and_identical_content_shares_the_workdir(tmp_path, repo):
    storage = storage_for(tmp_path)
    first = storage.snapshot(str(repo), dry_run=False)
    write(repo / "train.py", "print('train v2')\n")
    git(repo, "commit", "-q", "-am", "change")
    second = storage.snapshot(str(repo), dry_run=False)
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    third = storage.snapshot(str(repo), dry_run=False)

    assert second["content_id"] != first["content_id"]
    assert second["new_objects"] == 1
    first_tree, second_tree = Path(first["local_path"]), Path(second["local_path"])
    assert (
        os.stat(first_tree / "tokenizer.py").st_ino
        == os.stat(second_tree / "tokenizer.py").st_ino
    )
    assert third["workdir"] == second["workdir"]
    assert third["revision"] != second["revision"]
    assert third["manifest_path"] != second["manifest_path"]
    assert third["tree_existing"] is True


def test_copy_mode_skips_the_object_store(tmp_path, repo):
    result = storage_for(tmp_path, link_mode="copy").snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    assert result["link_mode"] == "copy" and result["new_objects"] == 0
    assert result["new_bytes"] == result["bytes"]
    assert os.stat(tree / "shared.txt").st_ino != os.stat(tree / "docs" / "notes.md").st_ino
    assert not (snapshot_root(tmp_path) / "objects" / "sha256").exists() or not any(
        (snapshot_root(tmp_path) / "objects" / "sha256").iterdir()
    )


def test_auto_mode_probes_the_filesystem(tmp_path, repo):
    result = storage_for(tmp_path, link_mode="auto").snapshot(str(repo), dry_run=False)

    assert result["link_mode"] in {"reflink", "hardlink", "copy"}
    assert (Path(result["local_path"]) / "train.py").read_text() == "print('train')\n"


def test_link_limit_falls_back_to_copies(tmp_path, repo, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError(errno.EMLINK, "Too many links")

    monkeypatch.setattr(snapshot_module.os, "link", refuse)

    result = storage_for(tmp_path).snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    assert result["link_fallbacks"] == result["files"]
    assert result["new_objects"] == 4
    assert (tree / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho run\n"
    assert stat.S_IMODE(os.stat(tree / "scripts" / "run.sh").st_mode) == 0o555


def test_secret_like_and_cache_paths_are_excluded_and_reported(tmp_path, repo):
    result = storage_for(tmp_path).snapshot(str(repo), exclude=["docs/"])

    assert result["excluded"] == [
        {"path": ".env", "reason": "secret_like", "rule": ".env*"},
        {"path": "api.token", "reason": "secret_like", "rule": "*.token"},
        {"path": "cache/table.bin", "reason": "cache", "rule": "cache/"},
        {"path": "config/credentials.json", "reason": "secret_like", "rule": "*credential*"},
        {"path": "docs/notes.md", "reason": "exclude_pattern", "rule": "docs/"},
    ]
    assert result["files"] == 4  # tokenizer.py is kept


def test_secret_like_include_is_an_error(tmp_path, repo):
    write(repo / ".env.local", "KEY=not-real\n")
    storage = storage_for(tmp_path)

    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), include=[".env.local"])

    assert caught.value.code == "secret_like_include"
    with pytest.raises(StorageError) as directory:
        storage.snapshot(str(repo), include=["."])
    assert directory.value.code == "secret_like_include"


def test_includes_override_tracked_files_and_add_working_tree_files(tmp_path, repo):
    write(repo / "train.py", "print('edited')\n")
    write(repo / "generated" / "table.json", "{}\n")
    write(repo / "generated" / "__pycache__" / "x.pyc", "bytecode")
    write(repo / "cache" / "table.bin", "restored\n")
    storage = storage_for(tmp_path)

    result = storage.snapshot(
        str(repo), include=["train.py", str(repo / "generated"), "cache/table.bin"], dry_run=False
    )

    tree = Path(result["local_path"])
    assert (tree / "train.py").read_text() == "print('edited')\n"
    assert (tree / "generated" / "table.json").read_text() == "{}\n"
    assert (tree / "cache" / "table.bin").read_text() == "restored\n"
    assert not (tree / "generated" / "__pycache__").exists()
    manifests = snapshot_root(tmp_path) / "manifests"
    manifest = json.loads((manifests / f"{result['snapshot_key']}.json").read_text())
    includes = {item["path"]: item for item in manifest["sources"] if item["kind"] == "include"}
    assert includes["train.py"]["overrides"] is True
    assert includes["cache/table.bin"]["overrides"] is True
    assert includes["generated/table.json"]["overrides"] is False
    pyc = {"path": "generated/__pycache__/x.pyc", "reason": "cache", "rule": "*.pyc"}
    assert pyc in result["excluded"]
    assert not any(item["path"] == "cache/table.bin" for item in result["excluded"])
    assert result["request_fields"]["code_revision"] == result["revision"]


@pytest.mark.parametrize(
    "value", ["../outside.txt", "/etc/hostname", "missing.txt", "link.txt", ".git", ".git/HEAD"]
)
def test_invalid_includes_are_rejected(tmp_path, repo, value):
    os.symlink("train.py", repo / "link.txt")
    with pytest.raises(StorageError) as caught:
        storage_for(tmp_path).snapshot(str(repo), include=[value])
    assert caught.value.code == "invalid_include"


@pytest.mark.parametrize("target", ["../../outside", "/etc/passwd"])
def test_unsafe_symlinks_fail(tmp_path, repo, target):
    os.symlink(target, repo / "scripts" / "escape")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "unsafe link")

    with pytest.raises(StorageError) as caught:
        storage_for(tmp_path).snapshot(str(repo))

    assert caught.value.code == "unsafe_symlink"


def commit_links(repo, links):
    for path, target in links.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / path)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")


@pytest.mark.parametrize(
    "links",
    [
        # Each target stays inside on its own; followed through the first link, it does not.
        {"a/b/up": "../..", "escape": "a/b/up/../.."},
        {
            "c/up1": "..",
            "a/b/up2": "../..",
            "e/f/g/up3": "../../..",
            "deep": "c/up1/a/b/up2/e/f/g/up3/..",
        },
        {"loop-a": "loop-b", "loop-b": "loop-a"},
        {"self": "self"},
    ],
    ids=["chain", "three_links", "loop", "self_loop"],
)
def test_symlink_chains_are_resolved_before_any_write(tmp_path, repo, links):
    commit_links(repo, links)
    storage = storage_for(tmp_path)

    for dry_run in (True, False):
        with pytest.raises(StorageError) as caught:
            storage.snapshot(str(repo), dry_run=dry_run)
        assert caught.value.code == "unsafe_symlink"
    assert not snapshot_root(tmp_path).exists()


def test_symlink_chains_that_stay_inside_are_kept(tmp_path, repo):
    commit_links(
        repo,
        {
            "a/b/up": "../..",
            "alias.py": "a/b/up/train.py",
            "docs/up": "..",
            "docs/run": "up/a/b/up/scripts/run.sh",
            "dangling": "a/missing/../b",
        },
    )

    result = storage_for(tmp_path).snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    assert result["symlinks"] == 6
    assert (tree / "alias.py").resolve() == (tree / "train.py").resolve()
    assert (tree / "docs" / "run").read_text() == "#!/bin/sh\necho run\n"
    assert os.readlink(tree / "dangling") == "a/missing/../b"


def test_materialized_links_are_checked_again_before_publishing(tmp_path, repo, monkeypatch):
    commit_links(repo, {"a/b/up": "../..", "escape": "a/b/up/../.."})
    monkeypatch.setattr(snapshot_module, "_check_symlink_chains", lambda symlinks: None)

    with pytest.raises(StorageError) as caught:
        storage_for(tmp_path).snapshot(str(repo), dry_run=False)

    assert caught.value.code == "unsafe_symlink"
    assert list((snapshot_root(tmp_path) / "trees").iterdir()) == []
    assert list((snapshot_root(tmp_path) / "manifests").iterdir()) == []
    assert [path.name for path in (snapshot_root(tmp_path) / "tmp").iterdir()] == []


@pytest.fixture
def plain_repo(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q")
    write(root / "train.py", "print('train')\n")
    write(root / "vendor" / "lib" / "code.py", "VALUE = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def test_include_dot_in_a_git_worktree_skips_git_metadata(tmp_path, plain_repo):
    worktree = tmp_path / "worktree"
    git(plain_repo, "worktree", "add", "-q", str(worktree))
    for root in (plain_repo, worktree):
        write(root / "generated.txt", "generated\n")
    storage = storage_for(tmp_path)

    from_worktree = storage.snapshot(str(worktree), include=["."], dry_run=False)
    from_checkout = storage.snapshot(str(plain_repo), include=["."])

    assert (worktree / ".git").is_file() and (plain_repo / ".git").is_dir()
    assert from_worktree["skipped"] == [{"path": ".git", "reason": "git_metadata"}]
    tree = Path(from_worktree["local_path"])
    assert (tree / "generated.txt").read_text() == "generated\n"
    assert not os.path.lexists(tree / ".git")
    # A .git file and a .git directory are reported alike, so the identities agree.
    assert from_checkout["skipped"] == from_worktree["skipped"]
    assert from_checkout["snapshot_key"] == from_worktree["snapshot_key"]
    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(worktree), include=[".git"])
    assert caught.value.code == "invalid_include"


def test_nested_git_file_in_an_included_directory_is_skipped_once(tmp_path, plain_repo):
    write(plain_repo / "vendor" / "lib" / ".git", "gitdir: ../../.git/modules/lib\n")

    result = storage_for(tmp_path).snapshot(str(plain_repo), include=[".", "vendor"])

    assert result["skipped"] == [
        {"path": ".git", "reason": "git_metadata"},
        {"path": "vendor/lib/.git", "reason": "git_metadata"},
    ]
    assert result["files"] == 2


def test_executable_and_plain_copies_of_one_blob_are_separate_objects(tmp_path, repo):
    write(repo / "tool.sh", "#!/bin/sh\necho run\n", 0o644)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "same blob")

    result = storage_for(tmp_path).snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    digest = hashlib.sha256(b"#!/bin/sh\necho run\n").hexdigest()
    objects = snapshot_root(tmp_path) / "objects" / "sha256" / digest[:2]
    assert sorted(
        path.name for path in objects.iterdir() if path.name.startswith(digest)
    ) == [digest, digest + ".x"]
    assert os.stat(tree / "tool.sh").st_ino != os.stat(tree / "scripts" / "run.sh").st_ino
    assert stat.S_IMODE(os.stat(tree / "tool.sh").st_mode) == 0o444


def test_submodules_are_skipped_and_lfs_pointers_warned(tmp_path, repo):
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    write(repo / "model.bin", "version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n")
    git(repo, "add", "model.bin")
    git(repo, "commit", "-q", "-m", "submodule and pointer")

    result = storage_for(tmp_path).snapshot(str(repo))

    assert result["skipped"] == [{"path": "vendor/lib", "reason": "submodule"}]
    assert [item["path"] for item in result["warnings"]] == ["model.bin"]
    assert result["warnings"][0]["code"] == "lfs_pointer"


def test_corrupted_tree_is_reported_and_never_repaired(tmp_path, repo):
    storage = storage_for(tmp_path)
    result = storage.snapshot(str(repo), dry_run=False)
    tree = Path(result["local_path"])
    target = tree / "train.py"
    target.chmod(0o644)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    target.chmod(0o444)
    before = (os.stat(tree).st_mtime_ns, os.stat(target).st_mtime_ns, target.read_text())

    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), dry_run=False)

    assert caught.value.code == "snapshot_corrupt"
    assert (os.stat(tree).st_mtime_ns, os.stat(target).st_mtime_ns, target.read_text()) == before


def test_same_size_corruption_needs_verify(tmp_path, repo):
    storage = storage_for(tmp_path)
    result = storage.snapshot(str(repo), dry_run=False)
    target = Path(result["local_path"]) / "train.py"
    target.chmod(0o644)
    target.write_text("print('TRAIN')\n")
    target.chmod(0o444)

    assert storage.snapshot(str(repo), dry_run=False)["existing"] is True
    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), dry_run=False, verify=True)
    assert caught.value.code == "snapshot_corrupt"


def test_concurrent_snapshots_publish_one_tree(tmp_path, repo):
    storage = storage_for(tmp_path)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(storage.snapshot(str(repo), dry_run=False))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len({item["content_id"] for item in results}) == 1
    assert len({item["manifest_sha256"] for item in results}) == 1
    assert len(list((snapshot_root(tmp_path) / "trees").iterdir())) == 1
    assert list((snapshot_root(tmp_path) / "tmp").iterdir()) == []


def test_ssh_only_access_requires_configuration(tmp_path, repo):
    storage = storage_for(tmp_path, mode="ssh", ssh={"host": "login"})

    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo))

    assert caught.value.code == "configuration_required"


@pytest.mark.parametrize(
    "snapshots",
    [
        {"root": "/shared"},
        {"root": "/reference/snapshots"},
        {"root": "/elsewhere/snapshots"},
        {"root": "/shared/snapshots", "link_mode": "symlink"},
        {"root": "/shared/snapshots", "unknown": True},
    ],
)
def test_invalid_snapshot_roots_are_rejected(tmp_path, snapshots):
    profile = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": str(tmp_path), "container_path": "/shared"},
                {"host_path": "/reference", "container_path": "/reference", "read_only": True},
            ],
            "defaults": {"image": "image", "pool": "pool"},
        }
    )
    with pytest.raises(StorageError) as caught:
        StorageService(profile, StorageAccessConfig.from_dict({"snapshots": snapshots}))
    assert caught.value.code == "invalid_storage_config"


def test_missing_configuration_revision_or_top_level_is_reported(tmp_path, repo):
    profile = ComputeProfile.from_dict(
        {"mounts": [{"host_path": str(tmp_path), "container_path": "/shared"}],
         "defaults": {"image": "image", "pool": "pool"}}
    )
    with pytest.raises(StorageError) as unconfigured:
        StorageService(profile, StorageAccessConfig()).snapshot(str(repo))
    assert unconfigured.value.code == "configuration_required"

    storage = storage_for(tmp_path)
    with pytest.raises(StorageError) as revision:
        storage.snapshot(str(repo), revision="no-such-branch")
    assert revision.value.code == "revision_not_found"
    with pytest.raises(StorageError) as option:
        storage.snapshot(str(repo), revision="--all")
    assert option.value.code == "invalid_request"
    with pytest.raises(StorageError) as nested:
        storage.snapshot(str(repo / "scripts"))
    assert nested.value.code == "invalid_repository"


def test_cli_snapshot_is_a_storage_command(monkeypatch, capsys):
    calls = []

    class Storage:
        def snapshot(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(compute_cli, "_resolve_storage", lambda args: Storage())
    monkeypatch.setattr(
        compute_cli, "_resolve_runtime",
        lambda args: (_ for _ in ()).throw(AssertionError("runtime resolved")),
    )

    assert compute_cli.main(["snapshot", "/work/repo"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": {"dry_run": True}}
    assert compute_cli.main([
        "snapshot", "/work/repo", "--revision", "v1", "--include", "a", "--include", "b",
        "--exclude", "*.log", "--execute", "--verify",
    ]) == 0
    assert calls == [
        (("/work/repo", "HEAD", None, None), {"dry_run": True, "verify": False}),
        (("/work/repo", "v1", ["a", "b"], ["*.log"]), {"dry_run": False, "verify": True}),
    ]


def test_mcp_registers_storage_snapshot_with_preview_default():
    pytest.importorskip("mcp")
    from mcp import Client

    from determined_compute.mcp_server import create_server

    calls = []

    class Storage:
        def snapshot(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"dry_run": kwargs["dry_run"]}

    class Service:
        pass

    async def exercise():
        async with Client(create_server(Service(), "alice", storage_service=Storage())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            tool = tools["storage_snapshot"]
            properties = tool.input_schema["properties"]
            assert set(tool.input_schema["required"]) == {"repo_dir"}
            assert properties["revision"]["default"] == "HEAD"
            assert properties["dry_run"]["default"] is True
            assert properties["verify"]["default"] is False
            assert "owner" not in properties
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is True
            result = await client.call_tool("storage_snapshot", {"repo_dir": "/work/repo"})
            assert result.structured_content == {"dry_run": True}

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
    assert calls == [(("/work/repo", "HEAD", None, None), {"dry_run": True, "verify": False})]
