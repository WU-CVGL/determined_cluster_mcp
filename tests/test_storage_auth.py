from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from determined_compute.storage import askpass
from determined_compute.storage import auth as auth_module
from determined_compute.storage.auth import SSHAuthError, ssh_auth


def _secrets_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "credentials.env"
    path.write_text(text, encoding="utf-8")
    return path


def _run_askpass(environment: dict[str, str], prompt: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [environment["SSH_ASKPASS"], prompt],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, **environment},
        start_new_session=True,
        check=False,
    )


def test_openssh_uses_safe_argv_and_forwards_existing_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/agent/test-agent.sock")
    config = SimpleNamespace(
        host="storage-alias",
        user="alice",
        port=2222,
        identity_file=Path("/keys/storage identity"),
        config_file=Path("/config/ssh config"),
        auth="openssh",
        keyring_service=None,
    )

    with ssh_auth(config) as (environment, options):
        assert environment == {"SSH_AUTH_SOCK": "/agent/test-agent.sock"}
        assert options == (
            "-F",
            "/config/ssh config",
            "-l",
            "alice",
            "-p",
            "2222",
            "-i",
            "/keys/storage identity",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
        )
        assert "storage-alias" not in options


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"host": "-proxy-command"}, id="host-option"),
        pytest.param({"config_file": "bad\x00config"}, id="control-character"),
        pytest.param({"port": True}, id="port-bool"),
        pytest.param({"port": "22x"}, id="port-not-integer"),
        pytest.param({"port": 0}, id="port-below-range"),
        pytest.param({"port": 65536}, id="port-above-range"),
    ],
)
def test_rejects_option_injection_control_characters_and_invalid_ports(
    override: dict[str, object],
) -> None:
    with pytest.raises(SSHAuthError):
        with ssh_auth({"host": "storage", "auth": "openssh", **override}):
            pass


def test_password_askpass_keeps_secret_out_of_argv_env_and_launcher_and_answers_once(
    tmp_path: Path,
) -> None:
    secret = "test password $() with spaces"
    secrets_path = _secrets_file(
        tmp_path,
        f"export SSH_USERNAME='storage-user'\nSSH_PASSWORD=\"{secret}\"\n",
    )
    config = {"host": "storage", "auth": "password"}

    with ssh_auth(config, secrets_path) as (environment, options):
        launcher = Path(environment["SSH_ASKPASS"])
        assert launcher.is_file()
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o700
        assert secret not in launcher.read_text(encoding="utf-8")
        assert secret not in repr(options)
        assert secret not in repr(environment)
        assert options[options.index("-l") + 1] == "storage-user"
        assert "StrictHostKeyChecking=yes" in options
        assert "NumberOfPasswordPrompts=1" in options
        assert "PreferredAuthentications=keyboard-interactive,password" in options
        assert "PubkeyAuthentication=no" in options

        # A non-password prompt is refused without using up the one answer.
        rejected = _run_askpass(environment, "Enter passphrase for key '/keys/id':")
        assert rejected.returncode == 1
        assert secret not in rejected.stdout + rejected.stderr

        accepted = _run_askpass(environment, "storage-user@storage's password:")
        assert accepted.returncode == 0
        assert accepted.stdout.rstrip("\n") == secret

        second = _run_askpass(environment, "Password:")
        assert second.returncode == 1
        assert second.stdout == ""
        assert secret not in second.stderr


def test_password_username_comes_from_profile_and_must_match_secrets(tmp_path: Path) -> None:
    config = {"host": "storage", "user": "profile-user", "auth": "password"}
    password_only = _secrets_file(tmp_path, "SSH_PASSWORD=fake-password\n")
    with ssh_auth(config, password_only) as (environment, options):
        assert options[options.index("-l") + 1] == "profile-user"
        assert environment["DETERMINED_COMPUTE_SSH_ASKPASS_USERNAME"] == "profile-user"

    mismatched = _secrets_file(
        tmp_path, "SSH_USERNAME=file-user\nSSH_PASSWORD=fake-password\n"
    )
    with pytest.raises(SSHAuthError, match="does not match"):
        with ssh_auth(config, mismatched):
            pass


def test_password_auth_never_reuses_determined_credentials(tmp_path: Path) -> None:
    secrets_path = _secrets_file(
        tmp_path, "DET_USERNAME=cluster-user\nDET_PASSWORD=cluster-password\n"
    )
    with pytest.raises(SSHAuthError, match="unavailable") as raised:
        with ssh_auth({"host": "storage", "auth": "password"}, secrets_path):
            pass
    assert raised.value.code == "ssh_auth_error"
    assert raised.value.retryable is False
    assert "cluster-password" not in str(raised.value)


def test_password_secret_read_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_to_load(_path: Path) -> dict[str, str]:
        raise RuntimeError("backend exposed a secret value")

    monkeypatch.setattr(auth_module, "load_secrets", fail_to_load)
    with pytest.raises(SSHAuthError) as raised:
        with ssh_auth(
            {"host": "storage", "user": "alice", "auth": "password"},
            tmp_path / "credentials.env",
        ):
            pass
    assert raised.value.code == "ssh_auth_error"
    assert "secret value" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_keyring_backend_uses_service_and_profile_user_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def get_password(service: str, username: str) -> str:
        calls.append((service, username))
        return "keyring-password"

    fake_keyring = SimpleNamespace(get_password=get_password)
    monkeypatch.setattr(
        askpass.importlib,
        "import_module",
        lambda name: fake_keyring if name == "keyring" else None,
    )
    environment = {
        "DETERMINED_COMPUTE_SSH_ASKPASS_MODE": "keyring",
        "DETERMINED_COMPUTE_SSH_ASKPASS_SERVICE": "determined-storage",
        "DETERMINED_COMPUTE_SSH_ASKPASS_USERNAME": "alice",
        "DETERMINED_COMPUTE_SSH_ASKPASS_STATE": str(tmp_path / "used"),
    }

    assert askpass.resolve_password("Password:", environment) == "keyring-password"
    assert calls == [("determined-storage", "alice")]
    with pytest.raises(askpass._AskpassFailure):
        askpass.resolve_password("Password:", environment)


def test_keyring_context_requires_profile_fields_and_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SSHAuthError, match="keyring_service"):
        with ssh_auth({"host": "storage", "user": "alice", "auth": "keyring"}):
            pass
    with pytest.raises(SSHAuthError, match="user"):
        with ssh_auth(
            {"host": "storage", "auth": "keyring", "keyring_service": "service"}
        ):
            pass

    monkeypatch.setattr(auth_module.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(SSHAuthError, match="keyring support is unavailable"):
        with ssh_auth(
            {
                "host": "storage",
                "user": "alice",
                "auth": "keyring",
                "keyring_service": "determined-storage",
            }
        ):
            pass


def test_askpass_main_uses_generic_error_without_trace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_prompt: str) -> str:
        raise RuntimeError("backend included a secret value")

    monkeypatch.setattr(askpass, "resolve_password", fail)
    assert askpass.main(["Password:"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret value" not in captured.err
    assert "Traceback" not in captured.err


def test_askpass_launcher_is_cleaned_up_on_context_error(tmp_path: Path) -> None:
    secrets_path = _secrets_file(
        tmp_path, "SSH_USERNAME=user\nSSH_PASSWORD=fake-password\n"
    )
    launcher: Path
    with pytest.raises(RuntimeError):
        with ssh_auth(
            {"host": "storage", "auth": "password"}, secrets_path
        ) as (environment, _options):
            launcher = Path(environment["SSH_ASKPASS"])
            assert launcher.exists()
            raise RuntimeError("simulated operation failure")
    assert not launcher.exists()
