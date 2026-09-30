from __future__ import annotations

import os
import pwd
import shutil
import sys
import time
from pathlib import Path

import pytest

from determined_compute.policy import Policy
from determined_compute.storage import StorageAccessConfig, StorageError, StorageService
from determined_compute.storage import service as storage_module


def profile(host_root: str = "/cluster/shared", allow_overwrite: bool = False) -> Policy:
    return Policy.from_dict(
        {
            "mounts": [{"host_path": host_root, "container_path": "/work"}],
            "defaults": {"image": "image", "pool": "pool", "slots": 1},
            "allow_overwrite": allow_overwrite,
        }
    )


def local_config(local_root: Path, mode: str = "auto") -> StorageAccessConfig:
    return StorageAccessConfig.from_dict(
        {
            "mode": mode,
            "local_mounts": [
                {"host_path": "/cluster/shared", "local_path": str(local_root)}
            ],
        }
    )


def test_storage_config_defaults_and_strict_validation(tmp_path):
    assert StorageAccessConfig() == StorageAccessConfig.from_dict(None)
    assert StorageAccessConfig().preserve_permissions is True
    path = tmp_path / "storage.yaml"
    path.write_text(
        "mode: ssh\n"
        "connect_timeout_seconds: 12\n"
        "timeout_seconds: 240\n"
        "ssh:\n"
        "  host: storage.example\n"
        "  user: alice\n"
        "  port: 2222\n"
        "  auth: openssh\n",
        encoding="utf-8",
    )
    config = StorageAccessConfig.from_file(path)
    assert config.ssh.host == "storage.example"
    assert config.ssh.user == "alice"
    assert config.connect_timeout_seconds == 12

    with pytest.raises(StorageError, match="ssh.host is invalid"):
        StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "-oProxyCommand=bad"}})
    with pytest.raises(StorageError, match="ssh.host is invalid"):
        StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example:22"}})
    with pytest.raises(StorageError, match="traversal"):
        StorageAccessConfig.from_dict(
            {"local_mounts": [{"host_path": "/cluster/../escape", "local_path": str(tmp_path)}]}
        )
    with pytest.raises(StorageError, match="between 1 and 3600"):
        StorageAccessConfig.from_dict({"timeout_seconds": 3601})
    with pytest.raises(StorageError, match="filesystem root"):
        StorageAccessConfig.from_dict(
            {"local_mounts": [{"host_path": "/cluster/shared", "local_path": "/"}]}
        )
    with pytest.raises(StorageError, match="must be a boolean"):
        StorageAccessConfig.from_dict({"preserve_permissions": "false"})


def test_check_translates_container_to_host_then_explicit_local_path(tmp_path, monkeypatch):
    local_root = tmp_path / "client-mount"
    target = local_root / "runs" / "one"
    target.mkdir(parents=True)
    service = StorageService(profile(), local_config(local_root))

    monkeypatch.setattr(storage_module, "_local_user", lambda: "alice")
    result = service.check("/work/runs/one")

    assert result == {
        "path": "/work/runs/one",
        "host_path": "/cluster/shared/runs/one",
        "local_path": str(target),
        "exists": True,
        "type": "directory",
        "readable": True,
        "writable": True,
        "read_only": False,
        "viewpoint": {
            "backend": "local",
            "local_root": str(local_root),
            "user": "alice",
            "note": storage_module._VIEWPOINT_NOTE,
        },
    }


def test_local_viewpoint_names_the_effective_user_and_never_credentials(tmp_path):
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root), secrets_path=tmp_path / "s.env")

    viewpoint = service.check("/work")["viewpoint"]

    assert viewpoint["user"] == pwd.getpwuid(os.geteuid()).pw_name
    assert set(viewpoint) == {"backend", "local_root", "user", "note"}


def test_auto_uses_same_host_path_only_when_root_exists(tmp_path):
    root = tmp_path / "mounted"
    root.mkdir()
    service = StorageService(profile(str(root)), StorageAccessConfig())
    result = service.check("/work/missing")
    assert result["viewpoint"]["backend"] == "local"
    assert result["exists"] is False
    assert result["local_path"] == str(root / "missing")

    unavailable = StorageService(profile("/definitely/not/mounted/here"), StorageAccessConfig())
    with pytest.raises(StorageError) as caught:
        unavailable.check("/work/task")
    assert caught.value.code == "configuration_required"
    assert caught.value.retryable is False


def test_local_mapping_symlink_cannot_escape_root(tmp_path):
    local_root = tmp_path / "client-mount"
    outside = tmp_path / "outside"
    local_root.mkdir()
    outside.mkdir()
    (local_root / "escape").symlink_to(outside, target_is_directory=True)
    service = StorageService(profile(), local_config(local_root))

    with pytest.raises(StorageError) as caught:
        service.check("/work/escape/file")
    assert caught.value.code == "invalid_storage_path"


def test_sync_builds_safe_local_rsync_and_excludes_nonstandard_secret(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    secret = source / "operator-creds.txt"
    secret.write_text("do not copy")
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root), secrets_path=secret)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"output": "preview", "truncated": False}

    monkeypatch.setattr(service, "_run", run)
    result = service.sync(str(source), "/work/tasks/task-1", dry_run=True)

    argv = calls[0][0]
    assert argv[:3] == ["rsync", "-a", "--safe-links"]
    assert "--mkpath" in argv
    assert "--itemize-changes" in argv
    assert "--dry-run" in argv
    assert "--delete" not in argv
    assert "--copy-links" not in argv
    assert "--exclude=/operator-creds.txt" in argv
    assert argv[-2] == str(source) + "/"
    assert argv[-1] == str(local_root / "tasks" / "task-1") + "/"
    assert not (local_root / "tasks").exists()
    assert result["dry_run"] is True
    assert result["completed"] is True
    assert result["host_path"] == "/cluster/shared/tasks/task-1"
    assert result["local_path"] == str(local_root / "tasks" / "task-1")
    assert result["excludes"][-1] == "/operator-creds.txt"
    assert result["truncated"] is False
    assert "--ignore-existing" in argv
    assert result["overwrite"] is False


@pytest.mark.parametrize("operation", ["sync", "fetch"])
def test_overwrite_needs_the_policy_and_drops_ignore_existing(tmp_path, monkeypatch, operation):
    source = tmp_path / "source"
    source.mkdir()
    local_root = tmp_path / "client-mount"
    (local_root / "task").mkdir(parents=True)

    def transfer(service, **kwargs):
        if operation == "sync":
            return service.sync(str(source), "/work/task", dry_run=True, **kwargs)
        return service.fetch("/work/task", str(tmp_path / "download"), dry_run=True, **kwargs)

    denied = StorageService(profile(), local_config(local_root))
    monkeypatch.setattr(denied, "_run", lambda *a, **k: pytest.fail("transport called"))
    with pytest.raises(StorageError) as caught:
        transfer(denied, overwrite=True)
    assert caught.value.code == "overwrite_not_allowed"
    with pytest.raises(StorageError) as caught:
        transfer(denied, overwrite="yes")
    assert caught.value.code == "invalid_request"

    allowed = StorageService(profile(allow_overwrite=True), local_config(local_root))
    calls = []
    monkeypatch.setattr(
        allowed, "_run", lambda argv, **k: calls.append(argv) or {"output": "", "truncated": False}
    )
    assert transfer(allowed, overwrite=True)["overwrite"] is True
    assert "--ignore-existing" not in calls[-1]
    assert transfer(allowed)["overwrite"] is False
    assert "--ignore-existing" in calls[-1]


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_real_sync_keeps_existing_files_unless_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    # Sizes differ, so rsync's size-and-time check cannot skip the file by itself.
    (source / "result.txt").write_text("new result", encoding="utf-8")
    (source / "added.txt").write_text("added", encoding="utf-8")
    local_root = tmp_path / "client-mount"
    shared = local_root / "task"
    shared.mkdir(parents=True)
    (shared / "result.txt").write_text("old", encoding="utf-8")

    StorageService(profile(), local_config(local_root)).sync(
        str(source), "/work/task", dry_run=False
    )
    assert (shared / "result.txt").read_text(encoding="utf-8") == "old"
    assert (shared / "added.txt").read_text(encoding="utf-8") == "added"

    StorageService(profile(allow_overwrite=True), local_config(local_root)).sync(
        str(source), "/work/task", dry_run=False, overwrite=True
    )
    assert (shared / "result.txt").read_text(encoding="utf-8") == "new result"


def test_sync_excludes_credentials_at_any_depth_and_escapes_secret_filter(tmp_path, monkeypatch):
    source = tmp_path / "source"
    secret = source / "config" / "creds[prod].toml"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret", encoding="utf-8")
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root), secrets_path=secret)
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: calls.append(argv) or {"output": "", "truncated": False},
    )

    service.sync(str(source), "/work/task", dry_run=True)

    argv = calls[0]
    for pattern in (
        ".git/",
        ".local/",
        ".ssh/",
        ".env*",
        "*.env.*",
        ".secrets*",
        ".venv/",
        "__pycache__/",
        "*.pem",
        "*.key",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_ed25519",
        "id_rsa",
    ):
        assert f"--exclude={pattern}" in argv
    assert r"--exclude=/config/creds\[prod\].toml" in argv


def test_sync_rejects_shared_root_and_never_creates_missing_copy_root(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    missing_root = tmp_path / "missing-root"
    service = StorageService(profile(), local_config(missing_root, mode="local"))

    with pytest.raises(StorageError, match="task subdirectory"):
        service.sync(str(source), "/work", dry_run=False)
    with pytest.raises(StorageError) as caught:
        service.sync(str(source), "/work/task", dry_run=False)
    assert caught.value.code == "configuration_required"
    assert not missing_root.exists()


def test_non_dry_local_sync_creates_only_destination_below_existing_root(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root))
    monkeypatch.setattr(service, "_run", lambda *a, **k: {"output": "", "truncated": False})

    service.sync(str(source), "/work/tasks/new", dry_run=False)

    assert (local_root / "tasks" / "new").is_dir()


def test_permission_preservation_can_be_disabled_for_restrictive_mounts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    config = StorageAccessConfig.from_dict(
        {
            "local_mounts": [
                {"host_path": "/cluster/shared", "local_path": str(local_root)}
            ],
            "preserve_permissions": False,
        }
    )
    service = StorageService(profile(), config)
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: calls.append(argv) or {"output": "", "truncated": False},
    )

    result = service.sync(str(source), "/work/task", dry_run=True)

    for option in ("--no-owner", "--no-group", "--no-perms", "--omit-dir-times"):
        assert option in calls[0]
    assert result["preserve_permissions"] is False


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_real_local_copy_with_permission_preservation_disabled(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello", encoding="utf-8")
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    config = StorageAccessConfig.from_dict(
        {
            "local_mounts": [
                {"host_path": "/cluster/shared", "local_path": str(local_root)}
            ],
            "preserve_permissions": False,
        }
    )

    result = StorageService(profile(), config).sync(
        str(source), "/work/task", dry_run=False
    )

    assert (local_root / "task" / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert result["preserve_permissions"] is False


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_real_local_nested_dry_run_is_reviewable_and_creates_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "new-file.txt").write_text("preview", encoding="utf-8")
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root))

    result = service.sync(str(source), "/work/missing/parents/task", dry_run=True)

    assert not (local_root / "missing").exists()
    assert "new-file.txt" in result["output"]


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_real_local_rsync_round_trip_uses_directory_contents(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.txt").write_text("complete", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (source / "unsafe-link").symlink_to(outside)
    local_root = tmp_path / "client-mount"
    local_root.mkdir()
    service = StorageService(profile(), local_config(local_root))

    service.sync(str(source), "/work/tasks/task-1", dry_run=False)
    shared = local_root / "tasks" / "task-1"
    assert (shared / "result.txt").read_text(encoding="utf-8") == "complete"
    assert not (shared / "unsafe-link").exists()

    download = tmp_path / "download"
    service.fetch("/work/tasks/task-1", str(download), dry_run=False)
    assert (download / "result.txt").read_text(encoding="utf-8") == "complete"


def test_fetch_requires_shared_directory_and_local_output_directory(tmp_path, monkeypatch):
    local_root = tmp_path / "client-mount"
    shared = local_root / "results"
    shared.mkdir(parents=True)
    destination = tmp_path / "download"
    service = StorageService(profile(), local_config(local_root))
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: calls.append(argv) or {"output": "", "truncated": False},
    )

    service.fetch("/work/results", str(destination), dry_run=False)

    assert destination.is_dir()
    assert calls[0][-2:] == [str(shared) + "/", str(destination) + "/"]
    file_destination = tmp_path / "not-a-dir"
    file_destination.write_text("x")
    with pytest.raises(StorageError, match="must be a directory"):
        service.fetch("/work/results", str(file_destination))
    with pytest.raises(StorageError, match="filesystem root"):
        service.fetch("/work/results", "/")


def test_ssh_check_uses_fixed_quoted_script_and_no_shell(tmp_path, monkeypatch):
    config = StorageAccessConfig.from_dict(
        {
            "mode": "ssh",
            "ssh": {"host": "storage.example", "user": "alice", "auth": "openssh"},
        }
    )
    service = StorageService(profile(), config)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return {
            "output": "exists=1\ntype=directory\nreadable=1\nwritable=0\nuser=alice\n",
            "truncated": False,
        }

    monkeypatch.setattr(service, "_run", run)
    result = service.check("/work/a path/with'quote")

    argv, kwargs = calls[0]
    assert argv[0] == "ssh"
    assert "-l" in argv and "alice" in argv
    assert argv[-2] == "storage.example"
    assert argv[-1].startswith("sh -c ")
    assert "a path" in argv[-1]
    assert set(kwargs["env_overrides"]) <= {"SSH_AUTH_SOCK"}
    assert "id -un" in argv[-1]
    assert result["viewpoint"] == {
        "backend": "ssh",
        "ssh_host": "storage.example",
        "user": "alice",
        "note": storage_module._VIEWPOINT_NOTE,
    }
    assert result["writable"] is False
    assert "user" not in result


@pytest.mark.parametrize(
    ("output", "user"),
    [
        ("exists=0\ntype=missing\nreadable=0\nwritable=0\nuser=\n", None),
        ("exists=0\ntype=missing\nreadable=0\nwritable=0\nuser=bob\x1b[0m\n", None),
    ],
)
def test_ssh_viewpoint_user_is_unknown_when_the_host_cannot_name_it(monkeypatch, output, user):
    config = StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example"}})
    service = StorageService(profile(), config)
    monkeypatch.setattr(service, "_run", lambda argv, **kwargs: {"output": output})

    assert service.check("/work/x")["viewpoint"]["user"] is user


def test_ssh_check_rejects_a_response_without_the_user_line(monkeypatch):
    config = StorageAccessConfig.from_dict({"mode": "ssh", "ssh": {"host": "storage.example"}})
    service = StorageService(profile(), config)
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: {"output": "exists=1\ntype=file\nreadable=1\nwritable=1\n"},
    )
    with pytest.raises(StorageError) as caught:
        service.check("/work/x")
    assert caught.value.code == "storage_check_failed"


def test_remote_rsync_uses_secluded_args_and_safe_links(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    config = StorageAccessConfig.from_dict(
        {
            "mode": "ssh",
            "ssh": {"host": "storage.example", "auth": "openssh"},
            "preserve_permissions": False,
        }
    )
    service = StorageService(profile(), config)
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or {"output": "", "truncated": False},
    )

    result = service.sync(str(source), "/work/task one", dry_run=True)

    argv, _kwargs = calls[0]
    assert argv[:3] == ["rsync", "-a", "--safe-links"]
    assert "--mkpath" in argv
    assert "--itemize-changes" in argv
    assert "-s" in argv
    assert "-e" in argv
    assert "--dry-run" in argv
    assert argv[-1] == "storage.example:/cluster/shared/task one/"
    assert "--delete" not in argv
    assert "--copy-links" not in argv
    assert "--ignore-existing" in argv
    for option in ("--no-owner", "--no-group", "--no-perms", "--omit-dir-times"):
        assert option in argv
    assert result["ssh_host"] == "storage.example"
    assert result["host_path"] == "/cluster/shared/task one"
    assert result["truncated"] is False
    assert result["preserve_permissions"] is False


def test_remote_rsync_brackets_raw_ipv6_host(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    config = StorageAccessConfig.from_dict(
        {"mode": "ssh", "ssh": {"host": "2001:db8::1", "auth": "openssh"}}
    )
    service = StorageService(profile(), config)
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: calls.append(argv) or {"output": "", "truncated": False},
    )

    service.sync(str(source), "/work/task", dry_run=True)

    assert calls[0][-1] == "[2001:db8::1]:/cluster/shared/task/"


def test_subprocess_output_is_bounded_and_stderr_not_returned():
    result = StorageService._run(
        [
            sys.executable,
            "-c",
            "import sys; print('x' * 20000); print('sensitive stderr', file=sys.stderr)",
        ],
        timeout=10,
    )
    assert len(result["output"]) == 16_384
    assert result["truncated"] is True
    assert "sensitive stderr" not in str(result)


def test_subprocess_reader_cleanup_is_bounded_when_grandchild_keeps_pipe_open():
    started = time.monotonic()
    result = StorageService._run(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                "print('parent complete')"
            ),
        ],
        timeout=10,
    )
    assert time.monotonic() - started < 3
    assert "parent complete" in result["output"]


def test_local_path_maps_a_container_path_through_a_local_mount(tmp_path):
    root = tmp_path / "mounted"
    (root / "repos" / "app").mkdir(parents=True)
    service = StorageService(profile(), local_config(root))

    assert service.local_path("/work/repos/app") == (root / "repos" / "app").resolve()
    with pytest.raises(StorageError) as caught:
        service.local_path("/elsewhere/app", "code.repo")
    assert caught.value.code == "invalid_storage_path"
    assert "code.repo /elsewhere/app is not under a mounted root" in str(caught.value)


@pytest.mark.parametrize("mode", ["auto", "ssh"])
def test_local_path_refuses_what_only_ssh_reaches(tmp_path, mode):
    config = {"mode": mode, "ssh": {"host": "storage.example"}}
    if mode == "ssh":
        root = tmp_path / "mounted"
        root.mkdir()
        config["local_mounts"] = [{"host_path": "/cluster/shared", "local_path": str(root)}]
    service = StorageService(profile(), StorageAccessConfig.from_dict(config))

    with pytest.raises(StorageError) as caught:
        service.local_path("/work/app", "code.repo")
    assert caught.value.code == "storage_not_local"
    assert "code.repo /work/app is not readable through a local mount" in str(caught.value)
