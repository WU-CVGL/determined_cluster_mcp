from __future__ import annotations

import asyncio
import ctypes
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
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService
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


def profile_for(tmp_path):
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": str(tmp_path / "shared"), "container_path": "/shared"},
                {"host_path": str(tmp_path / "reference"), "container_path": "/reference",
                 "read_only": True},
            ],
            "defaults": {"image": "image", "pool": "pool"},
        }
    )


def storage_for(tmp_path, link_mode="hardlink", **access):
    (tmp_path / "shared").mkdir(exist_ok=True)
    config = StorageAccessConfig.from_dict(
        {"snapshots": {"root": "/shared/snapshots", "link_mode": link_mode}, **access}
    )
    return StorageService(profile_for(tmp_path), config, tmp_path / "unused-secrets.env")


def snapshot_root(tmp_path):
    return tmp_path / "shared" / "snapshots"


def read_manifest(tmp_path, result):
    return json.loads(
        (snapshot_root(tmp_path) / "manifests" / f"{result['snapshot_key']}.json").read_text()
    )


def no_reflink(*_args, **_kwargs):
    raise OSError(errno.EOPNOTSUPP, "Operation not supported")


def refuse_links(monkeypatch, code=errno.EPERM):
    def refuse(*_args, **_kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(snapshot_module.os, "link", refuse)


def run_concurrently(count, function):
    barrier = threading.Barrier(count)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(function())
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    return results


# Publishing, identity and reuse


def test_preview_writes_nothing_and_execute_publishes_a_read_only_tree(tmp_path, repo):
    storage = storage_for(tmp_path)

    preview = storage.snapshot(str(repo))
    wrote_nothing = not snapshot_root(tmp_path).exists()
    result = storage.snapshot(str(repo), dry_run=False)

    assert wrote_nothing
    assert preview["dry_run"] is True and result["dry_run"] is False
    assert preview["revision"] == git(repo, "rev-parse", "HEAD")
    assert preview["tree"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert preview["workdir"] == f"/shared/snapshots/trees/{preview['content_id']}"
    assert preview["request_fields"] == {
        "workdir": preview["workdir"],
        "code_revision": preview["revision"],
    }
    assert preview["manifest_path"] is None and preview["manifest_sha256"] is None
    assert preview["existing"] is False and preview["tree_existing"] is False
    assert preview["files"] == 5 and preview["symlinks"] == 1
    assert preview["new_objects"] == 4  # shared.txt and docs/notes.md are one object
    # The preview names exactly what the publish creates.
    for key in ("content_id", "snapshot_key", "request_fields", "new_objects", "new_bytes"):
        assert result[key] == preview[key]

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


def test_objects_are_shared_by_content_and_mode_across_revisions(tmp_path, repo):
    storage = storage_for(tmp_path)
    first = storage.snapshot(str(repo), dry_run=False)
    write(repo / "train.py", "print('train v2')\n")
    write(repo / "tool.sh", "#!/bin/sh\necho run\n")  # run.sh's blob, without the exec bit
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    second = storage.snapshot(str(repo), dry_run=False)
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    third = storage.snapshot(str(repo), dry_run=False)

    assert second["content_id"] != first["content_id"]
    assert second["new_objects"] == 2  # the new train.py and a plain copy of run.sh
    first_tree, second_tree = Path(first["local_path"]), Path(second["local_path"])
    assert (
        os.stat(first_tree / "tokenizer.py").st_ino
        == os.stat(second_tree / "tokenizer.py").st_ino
    )
    # Hard links share one mode, so executable and plain copies of a blob are two objects.
    digest = hashlib.sha256(b"#!/bin/sh\necho run\n").hexdigest()
    objects = snapshot_root(tmp_path) / "objects" / "sha256" / digest[:2]
    assert sorted(
        path.name for path in objects.iterdir() if path.name.startswith(digest)
    ) == [digest, digest + ".x"]
    assert os.stat(second_tree / "tool.sh").st_ino != (
        os.stat(second_tree / "scripts" / "run.sh").st_ino
    )
    assert stat.S_IMODE(os.stat(second_tree / "tool.sh").st_mode) == 0o444
    # Identical content under a new commit shares the workdir but not the manifest.
    assert third["workdir"] == second["workdir"]
    assert third["revision"] != second["revision"]
    assert third["manifest_path"] != second["manifest_path"]
    assert third["tree_existing"] is True


# Link modes


def test_auto_mode_probes_the_filesystem(tmp_path, repo):
    result = storage_for(tmp_path, link_mode="auto").snapshot(str(repo), dry_run=False)

    assert result["link_mode"] in {"reflink", "copy"}
    assert (Path(result["local_path"]) / "train.py").read_text() == "print('train')\n"


@pytest.mark.parametrize("link_mode", ["auto", "copy"])
def test_copy_skips_the_object_store_and_the_preview_counts_every_file(
    tmp_path, repo, monkeypatch, link_mode
):
    monkeypatch.setattr(snapshot_module, "_reflink", no_reflink)
    storage = storage_for(tmp_path, link_mode=link_mode)

    preview = storage.snapshot(str(repo))
    wrote_nothing = not snapshot_root(tmp_path).exists()
    published = storage.snapshot(str(repo), dry_run=False)
    again = storage.snapshot(str(repo))

    assert wrote_nothing
    assert preview["link_mode"] == link_mode and published["link_mode"] == "copy"
    # shared.txt and docs/notes.md hold the same content, and a copy writes both.
    assert preview["new_objects"] == published["new_objects"] == 0
    assert preview["new_bytes"] == published["new_bytes"] == preview["bytes"]
    assert again["new_objects"] == again["new_bytes"] == 0
    # Without reflink, auto copies: it never hard-links tree files to each other or a store.
    tree = Path(published["local_path"])
    assert os.stat(tree / "train.py").st_nlink == 1
    assert os.stat(tree / "shared.txt").st_ino != os.stat(tree / "docs" / "notes.md").st_ino
    assert list((snapshot_root(tmp_path) / "objects" / "sha256").iterdir()) == []


def test_auto_preview_is_an_upper_bound_on_reflink_storage(tmp_path, repo, monkeypatch):
    def clone(source, destination):
        shutil.copyfile(source, destination)

    monkeypatch.setattr(snapshot_module, "_reflink", clone)
    storage = storage_for(tmp_path, link_mode="auto")

    preview = storage.snapshot(str(repo))
    published = storage.snapshot(str(repo), dry_run=False)

    assert preview["link_mode"] == "auto" and published["link_mode"] == "reflink"
    assert preview["new_objects"] == 0 and preview["new_bytes"] == preview["bytes"]
    assert published["new_objects"] == 4
    assert published["new_bytes"] < preview["new_bytes"]


def test_link_limit_falls_back_to_copies(tmp_path, repo, monkeypatch):
    refuse_links(monkeypatch, errno.EMLINK)

    result = storage_for(tmp_path).snapshot(str(repo), dry_run=False)

    tree = Path(result["local_path"])
    assert result["link_fallbacks"] == result["files"]
    assert result["new_objects"] == 4
    assert (tree / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho run\n"
    assert stat.S_IMODE(os.stat(tree / "scripts" / "run.sh").st_mode) == 0o555


# Secret-like, cache and excluded paths


def test_secret_like_and_cache_paths_are_excluded_and_reported(tmp_path, repo):
    write(repo / ".env.local", "KEY=not-real\n")
    storage = storage_for(tmp_path)

    result = storage.snapshot(str(repo), exclude=["docs/"])
    expanded = storage.snapshot(str(repo), include=["."])

    assert result["excluded"] == [
        {"path": ".env", "reason": "secret_like", "rule": ".env*"},
        {"path": "api.token", "reason": "secret_like", "rule": "*.token"},
        {"path": "cache/table.bin", "reason": "cache", "rule": "cache/"},
        {"path": "config/credentials.json", "reason": "secret_like", "rule": "*credential*"},
        {"path": "docs/notes.md", "reason": "exclude_pattern", "rule": "docs/"},
    ]
    assert result["files"] == 4  # tokenizer.py is kept
    # A directory include applies the same rules to working-tree files, without an error.
    assert {"path": ".env.local", "reason": "secret_like", "rule": ".env*"} in (
        expanded["excluded"]
    )
    assert {"path": ".env", "reason": "secret_like", "rule": ".env*"} in expanded["excluded"]
    assert expanded["warnings"] == []


@pytest.mark.parametrize(
    ("path", "skip"),
    [
        (".ssh/config", "/.ssh/"),  # a credential directory
        ("keys[1]/id_ecdsa", "/keys[[]1]/id_ecdsa"),  # a private key; the pattern is escaped
        ("cluster.conf", "/cluster.conf"),  # the configured secrets file
    ],
)
def test_hard_secret_rules_refuse_every_include(tmp_path, repo, path, skip):
    write(repo / path, "not-real\n")
    storage = storage_for(tmp_path)
    storage.secrets_path = repo / "cluster.conf"

    with pytest.raises(StorageError) as named:
        storage.snapshot(str(repo), include=[path])
    with pytest.raises(StorageError) as walked:
        storage.snapshot(str(repo), include=["."])
    # The exclude that the error suggests skips just that entry inside the directory.
    skipped = storage.snapshot(str(repo), include=["."], exclude=[skip])

    assert named.value.code == walked.value.code == "secret_like_include"
    assert f"add {skip!r} to exclude" in str(walked.value)
    assert {"path": skip[1:].replace("[[]", "["), "reason": "exclude_pattern", "rule": skip} in (
        skipped["excluded"]
    )
    assert skipped["files"] == 5


def test_an_explicit_include_overrides_soft_secret_rules(tmp_path, plain_repo):
    write(plain_repo / "src" / "pkg" / "__init__.py", "from .utils import secrets\n")
    write(plain_repo / "src" / "pkg" / "utils" / "__init__.py", "")
    write(plain_repo / "src" / "pkg" / "utils" / "secrets.py", "def load():\n    return None\n")
    write(plain_repo / "src" / "pkg" / "utils" / ".env", "KEY=not-real\n")
    write(plain_repo / "src" / "id_rsa_parser.py", "def parse():\n    return None\n")
    write(plain_repo / "tests" / "fixtures" / "id_ed25519.pub", "ssh-ed25519 AAAA test\n")
    git(plain_repo, "add", "-A")
    git(plain_repo, "commit", "-q", "-m", "secret-like names")
    write(plain_repo / "keys" / "id_rsa_deploy", "not-real\n")
    storage = storage_for(tmp_path)
    module = {"path": "src/pkg/utils/secrets.py", "reason": "secret_like", "rule": "*secret*"}
    parser = {"path": "src/id_rsa_parser.py", "reason": "secret_like", "rule": "id_rsa*"}
    fixture = {
        "path": "tests/fixtures/id_ed25519.pub", "reason": "secret_like", "rule": "id_ed25519*"
    }
    dotenv = {"path": "src/pkg/utils/.env", "reason": "secret_like", "rule": ".env*"}
    deploy = {"path": "keys/id_rsa_deploy", "reason": "secret_like", "rule": "id_rsa*"}

    default = storage.snapshot(str(plain_repo))
    walked = storage.snapshot(str(plain_repo), include=["."])
    explicit = storage.snapshot(
        str(plain_repo),
        include=["src/pkg/utils/secrets.py", "src/id_rsa_parser.py",
                 "tests/fixtures/id_ed25519.pub"],
        dry_run=False,
    )

    # Tracked and walked matches are excluded and reported, never an error.
    for result in (default, walked):
        assert all(item in result["excluded"] for item in (module, parser, fixture, dotenv))
        assert result["warnings"] == []
    assert default["files"] == 4
    assert deploy in walked["excluded"]
    # An include that names the file restores it, with a warning and a manifest record.
    tree = Path(explicit["local_path"])
    assert (tree / "src" / "pkg" / "utils" / "secrets.py").read_text() == (
        "def load():\n    return None\n"
    )
    assert (tree / "src" / "id_rsa_parser.py").exists()
    assert (tree / "tests" / "fixtures" / "id_ed25519.pub").exists()
    assert explicit["files"] == 7
    assert not any(item in explicit["excluded"] for item in (module, parser, fixture))
    assert dotenv in explicit["excluded"]
    assert [(item["code"], item["path"], item["rule"]) for item in explicit["warnings"]] == [
        ("secret_like_included", "src/id_rsa_parser.py", "id_rsa*"),
        ("secret_like_included", "src/pkg/utils/secrets.py", "*secret*"),
        ("secret_like_included", "tests/fixtures/id_ed25519.pub", "id_ed25519*"),
    ]
    sources = read_manifest(tmp_path, explicit)["sources"]
    assert {
        item["path"]: (item["included_despite"], item["overrides"])
        for item in sources if item["kind"] == "include"
    } == {
        "src/id_rsa_parser.py": ("id_rsa*", True),
        "src/pkg/utils/secrets.py": ("*secret*", True),
        "tests/fixtures/id_ed25519.pub": ("id_ed25519*", True),
    }


# Includes


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
    manifest = read_manifest(tmp_path, result)
    includes = {item["path"]: item for item in manifest["sources"] if item["kind"] == "include"}
    assert includes["train.py"]["overrides"] is True
    assert includes["cache/table.bin"]["overrides"] is True
    assert includes["generated/table.json"]["overrides"] is False
    pycache = {"path": "generated/__pycache__/", "reason": "cache", "rule": "__pycache__/"}
    assert pycache in result["excluded"]
    assert not any(item["path"] == "cache/table.bin" for item in result["excluded"])
    # The content differs from the commit, so the revision also names the manifest.
    assert result["request_fields"]["code_revision"] == (
        f"{result['revision']}+{result['snapshot_key']}"
    )
    # An include that adds nothing leaves the content, and the revision, as committed.
    only_cache = storage.snapshot(str(repo), include=["generated/__pycache__"])
    assert only_cache["request_fields"]["code_revision"] == only_cache["revision"]


def test_including_a_cache_like_directory_restores_it(tmp_path, plain_repo):
    write(plain_repo / "mylib" / "__init__.py", "from . import cache\n")
    write(plain_repo / "mylib" / "cache" / "__init__.py", "SIZE = 1\n")
    git(plain_repo, "add", "-A")
    git(plain_repo, "commit", "-q", "-m", "cache package")
    write(plain_repo / "mylib" / "cache" / "__pycache__" / "x.pyc", "bytecode")
    storage = storage_for(tmp_path)
    package = {"path": "mylib/cache/__init__.py", "reason": "cache", "rule": "cache/"}

    default = storage.snapshot(str(plain_repo))
    restored = storage.snapshot(str(plain_repo), include=["mylib/cache"], dry_run=False)
    excluded = storage.snapshot(str(plain_repo), include=["mylib/cache"], exclude=["cache/"])

    assert package in default["excluded"] and default["files"] == 3
    assert package not in restored["excluded"] and restored["files"] == 4
    assert (Path(restored["local_path"]) / "mylib" / "cache" / "__init__.py").exists()
    assert {"path": "mylib/cache/__pycache__/", "reason": "cache", "rule": "__pycache__/"} in (
        restored["excluded"]
    )
    # Exclude patterns still see the repository-relative path.
    assert excluded["files"] == 3
    assert "mylib/cache/__init__.py" in {item["path"] for item in excluded["excluded"]}


def test_directory_includes_prune_excluded_subtrees_before_checking_links(tmp_path, plain_repo):
    os.makedirs(plain_repo / ".venv" / "bin")
    os.symlink("/outside/bin/python3", plain_repo / ".venv" / "bin" / "python")
    os.makedirs(plain_repo / "gen" / "node_modules" / ".bin")
    os.symlink("../pkg/cli.js", plain_repo / "gen" / "node_modules" / ".bin" / "pkg")
    write(plain_repo / "gen" / "table.json", "{}\n")
    storage = storage_for(tmp_path)

    whole = storage.snapshot(str(plain_repo), include=["."], exclude=["node_modules/"])
    generated = storage.snapshot(str(plain_repo), include=["gen"], exclude=["node_modules/"])

    assert {"path": ".venv/", "reason": "cache", "rule": ".venv/"} in whole["excluded"]
    assert whole["files"] == generated["files"] == 3
    assert {"path": "gen/node_modules/", "reason": "exclude_pattern", "rule": "node_modules/"} in (
        generated["excluded"]
    )
    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(plain_repo), include=["gen"])
    assert caught.value.code == "invalid_include"


def test_directory_includes_keep_tracked_symlinks(tmp_path, repo):
    storage = storage_for(tmp_path)

    result = storage.snapshot(str(repo), include=["scripts"], dry_run=False)

    tree = Path(result["local_path"])
    assert os.readlink(tree / "scripts" / "train-link.py") == "../train.py"
    assert result["symlinks"] == 1
    os.unlink(repo / "scripts" / "train-link.py")
    os.symlink("../tokenizer.py", repo / "scripts" / "train-link.py")
    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), include=["scripts"])
    assert caught.value.code == "invalid_include"


def test_includes_that_escape_or_are_not_files_are_rejected(tmp_path, repo):
    os.symlink("train.py", repo / "link.txt")
    storage = storage_for(tmp_path)

    for value in (
        "../outside.txt",
        str(tmp_path / "outside.txt"),
        ".git/HEAD",
        "missing.txt",
        "link.txt",
    ):
        with pytest.raises(StorageError) as caught:
            storage.snapshot(str(repo), include=[value])
        assert caught.value.code == "invalid_include", value


def test_git_metadata_in_included_directories_is_skipped(tmp_path, plain_repo):
    worktree = tmp_path / "worktree"
    git(plain_repo, "worktree", "add", "-q", str(worktree))
    for root in (plain_repo, worktree):
        write(root / "generated.txt", "generated\n")
        write(root / "vendor" / "lib" / ".git", "gitdir: ../../.git/modules/lib\n")
    storage = storage_for(tmp_path)

    # "vendor" overlaps "."; each .git entry is still reported once.
    from_worktree = storage.snapshot(str(worktree), include=[".", "vendor"], dry_run=False)
    from_checkout = storage.snapshot(str(plain_repo), include=[".", "vendor"])

    assert (worktree / ".git").is_file() and (plain_repo / ".git").is_dir()
    assert from_worktree["skipped"] == [
        {"path": ".git", "reason": "git_metadata"},
        {"path": "vendor/lib/.git", "reason": "git_metadata"},
    ]
    assert from_worktree["files"] == 3
    tree = Path(from_worktree["local_path"])
    assert (tree / "generated.txt").read_text() == "generated\n"
    assert not os.path.lexists(tree / ".git")
    assert not os.path.lexists(tree / "vendor" / "lib" / ".git")
    # A .git file and a .git directory are reported alike, so the identities agree.
    assert from_checkout["skipped"] == from_worktree["skipped"]
    assert from_checkout["snapshot_key"] == from_worktree["snapshot_key"]
    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(worktree), include=[".git"])
    assert caught.value.code == "invalid_include"


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


# Symlinks


def commit_links(repo, links):
    for path, target in links.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / path)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")


@pytest.mark.parametrize(
    "links",
    [
        {"scripts/escape": "/outside"},
        {"scripts/escape": "../../outside"},
        # Each target stays inside on its own; followed through the first link, it does not.
        {"a/b/up": "../..", "escape": "a/b/up/../.."},
        {"loop-a": "loop-b", "loop-b": "loop-a"},
    ],
    ids=["absolute", "parent", "chain", "loop"],
)
def test_unsafe_symlinks_fail_before_any_write(tmp_path, repo, links):
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
    assert list((snapshot_root(tmp_path) / "tmp").iterdir()) == []


# Corruption and verify


def test_corrupted_tree_is_reported_and_never_repaired(tmp_path, repo):
    storage = storage_for(tmp_path)
    tree = Path(storage.snapshot(str(repo), dry_run=False)["local_path"])
    target = tree / "train.py"
    target.chmod(0o644)
    target.write_text("print('TRAIN')\n")
    target.chmod(0o444)

    # Reuse checks sizes; a same-size change needs verify, which also hashes the content.
    assert storage.snapshot(str(repo), dry_run=False)["existing"] is True
    with pytest.raises(StorageError) as same_size:
        storage.snapshot(str(repo), dry_run=False, verify=True)
    target.chmod(0o644)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    target.chmod(0o444)
    before = (os.stat(tree).st_mtime_ns, os.stat(target).st_mtime_ns, target.read_text())
    with pytest.raises(StorageError) as resized:
        storage.snapshot(str(repo), dry_run=False)

    assert same_size.value.code == resized.value.code == "snapshot_corrupt"
    assert (os.stat(tree).st_mtime_ns, os.stat(target).st_mtime_ns, target.read_text()) == before


def add_output_file(tree: Path) -> None:
    tree.chmod(0o755)
    write(tree / "outputs" / "last.ckpt", "weights\n")


def add_output_directory(tree: Path) -> None:
    tree.chmod(0o755)
    (tree / "lightning_logs").mkdir()


def make_executable(tree: Path) -> None:
    (tree / "train.py").chmod(0o555)


def replace_link_with_file(tree: Path) -> None:
    (tree / "scripts").chmod(0o755)
    os.unlink(tree / "scripts" / "train-link.py")
    write(tree / "scripts" / "train-link.py", "print('train')\n")


@pytest.mark.parametrize(
    "change", [add_output_file, add_output_directory, make_executable, replace_link_with_file]
)
def test_verify_checks_the_whole_tree(tmp_path, repo, change):
    storage = storage_for(tmp_path)
    change(Path(storage.snapshot(str(repo), dry_run=False)["local_path"]))

    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), verify=True)

    assert caught.value.code == "snapshot_corrupt"


def test_verify_checks_an_existing_object_before_reusing_it(tmp_path, repo):
    storage = storage_for(tmp_path)
    storage.snapshot(str(repo), dry_run=False)
    digest = hashlib.sha256(b"print('train')\n").hexdigest()
    obj = snapshot_root(tmp_path) / "objects" / "sha256" / digest[:2] / digest
    obj.chmod(0o555)  # the object of a non-executable file gains the exec bit
    write(repo / "tokenizer.py", "TOKENS = 2\n")
    git(repo, "commit", "-q", "-am", "change")

    with pytest.raises(StorageError) as caught:
        storage.snapshot(str(repo), dry_run=False, verify=True)

    assert caught.value.code == "snapshot_corrupt"
    assert len(list((snapshot_root(tmp_path) / "trees").iterdir())) == 1


# Publishing without hard links, and concurrent callers


def test_manifest_fallback_never_replaces_a_published_manifest(tmp_path, monkeypatch):
    manifest, staging = tmp_path / "manifest.json", tmp_path / "tmp"
    staging.mkdir()
    manifest.write_bytes(b"published first\n")
    refuse_links(monkeypatch)
    exists = Path.exists
    # The competing writer published after this caller's existence check.
    monkeypatch.setattr(
        Path, "exists", lambda self, **kwargs: self != manifest and exists(self, **kwargs)
    )

    payload = snapshot_module._publish_manifest(manifest, staging, {"created_utc": "later"})

    assert payload == manifest.read_bytes() == b"published first\n"
    assert list(staging.iterdir()) == []


def test_manifest_fallback_returns_the_published_bytes(tmp_path, monkeypatch):
    staging = tmp_path / "tmp"
    staging.mkdir()
    refuse_links(monkeypatch)

    def publish_twice(manifest):
        first = snapshot_module._publish_manifest(manifest, staging, {"created_utc": "first"})
        second = snapshot_module._publish_manifest(manifest, staging, {"created_utc": "second"})
        assert first == second == manifest.read_bytes()
        assert json.loads(first) == {"created_utc": "first"}

    publish_twice(tmp_path / "noreplace.json")
    # Without renameat2, the fallback is a check followed by a plain rename.
    monkeypatch.setattr(snapshot_module, "_renameat2", lambda: None)
    publish_twice(tmp_path / "plain.json")
    assert list(staging.iterdir()) == []


def test_rename_noreplace_falls_back_when_unsupported(tmp_path, monkeypatch):
    def unsupported(*_args):
        ctypes.set_errno(errno.EINVAL)
        return -1

    monkeypatch.setattr(snapshot_module, "_renameat2", lambda: unsupported)
    source, destination, taken = tmp_path / "new", tmp_path / "free", tmp_path / "taken"
    source.write_text("new\n")
    taken.write_text("old\n")

    assert snapshot_module._rename_noreplace(source, destination) is True
    source.write_text("again\n")
    assert snapshot_module._rename_noreplace(source, taken) is False
    assert destination.read_text() == "new\n" and taken.read_text() == "old\n"


def test_concurrent_snapshots_without_hard_links_agree_on_the_manifest(
    tmp_path, repo, monkeypatch
):
    refuse_links(monkeypatch)
    storage = storage_for(tmp_path, link_mode="copy")
    storage.snapshot(str(repo), dry_run=False)
    # Same content under a new commit: every caller reuses the tree and races to publish.
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")

    results = run_concurrently(4, lambda: storage.snapshot(str(repo), dry_run=False))

    [key] = {item["snapshot_key"] for item in results}
    on_disk = (snapshot_root(tmp_path) / "manifests" / f"{key}.json").read_bytes()
    assert {item["manifest_sha256"] for item in results} == {hashlib.sha256(on_disk).hexdigest()}


def test_concurrent_snapshots_publish_one_tree(tmp_path, repo):
    storage = storage_for(tmp_path)

    results = run_concurrently(2, lambda: storage.snapshot(str(repo), dry_run=False))

    assert len({item["content_id"] for item in results}) == 1
    assert len({item["manifest_sha256"] for item in results}) == 1
    assert len(list((snapshot_root(tmp_path) / "trees").iterdir())) == 1
    assert list((snapshot_root(tmp_path) / "tmp").iterdir()) == []


# Configuration and requests


def test_invalid_snapshot_roots_are_rejected(tmp_path):
    for snapshots in (
        {"root": "/shared"},  # a mount root, not a subdirectory
        {"root": "/reference/snapshots"},  # a read-only mount
        {"root": "/elsewhere/snapshots"},  # outside every mount
        {"root": "/shared/snapshots", "link_mode": "symlink"},
        {"root": "/shared/snapshots", "unknown": True},
    ):
        with pytest.raises(StorageError) as caught:
            StorageService(profile_for(tmp_path), StorageAccessConfig.from_dict(
                {"snapshots": snapshots}
            ))
        assert caught.value.code == "invalid_storage_config", snapshots


def test_missing_configuration_revision_or_top_level_is_reported(tmp_path, repo):
    with pytest.raises(StorageError) as unconfigured:
        StorageService(profile_for(tmp_path), StorageAccessConfig()).snapshot(str(repo))
    assert unconfigured.value.code == "configuration_required"
    ssh_only = storage_for(tmp_path, mode="ssh", ssh={"host": "login"})
    with pytest.raises(StorageError) as no_local_view:
        ssh_only.snapshot(str(repo))
    assert no_local_view.value.code == "configuration_required"

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
