"""Local SSH access to running shells: keys, tunnel, generated ssh-mcp config, cleanup."""

from __future__ import annotations

import base64
import copy
import errno
import getpass
import hashlib
import json
import os
import queue
import shlex
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, ShellAccess
from determined_compute.compute import shell_access as module
from determined_compute.compute.shell_access import (
    SSH_MCP_CONFIG,
    abort_websocket,
    host_key_fingerprint,
    relay,
    ssh_option_path,
    toml_string,
    transport_error,
    websocket_opener,
)
from determined_compute.core.api_client import APIError


SHELL_ID = "5b9c2f3e-1a2b-4c3d-8e4f-0123456789ab"
OTHER_SHELL_ID = "5b9c2f3e-ffff-4c3d-8e4f-0123456789ab"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeGdQyQJm5H1EPEJyWPw7fWe35UfpiPUOtpxNmxblpZ"
FINGERPRINT = "SHA256:pYBj1r/m4zVSYegSBqqEqjHkxvchBSMc5FYh+emWEhg"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "fixture-private-key-material\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
BANNER = b"SSH-2.0-OpenSSH_9.6 fixture\r\n"
PROXY_VARIABLES = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


@pytest.fixture(autouse=True)
def _no_proxy_environment(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _private_control_directory(monkeypatch, tmp_path):
    """Keep multiplexing sockets in a short temporary directory, never the real ~/.ssh."""
    runtime = tempfile.mkdtemp(prefix="dc", dir="/tmp" if os.path.isdir("/tmp") else None)
    os.chmod(runtime, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield Path(runtime)
    shutil.rmtree(runtime, ignore_errors=True)


class FakeWebSocket:
    """A shell proxy that sends the sshd banner, then echoes binary frames."""

    def __init__(self, banner=(BANNER,)) -> None:
        self.incoming = queue.Queue()
        for chunk in banner:
            self.incoming.put((0x2, chunk))
        self.sent = []
        self.closed = threading.Event()

    def send_binary(self, data):
        if self.closed.is_set():
            raise OSError("closed")
        self.sent.append(data)
        self.incoming.put((0x2, data))

    def recv_data(self):
        item = self.incoming.get(timeout=5)
        if item is None:
            raise OSError("closed")
        return item

    def send_close(self):
        self.incoming.put((0x8, b""))

    def settimeout(self, timeout):
        pass

    def shutdown(self):
        self.closed.set()
        self.incoming.put(None)


class FakeClient:
    api_url = "http://det.example.test:8080"
    verify_ssl = False
    headers = {"Authorization": "Bearer fixture-token"}

    def __init__(self) -> None:
        self.user = {"id": "7", "username": "alice"}
        self.calls = []
        self.state = "STATE_RUNNING"
        self.owner = "7"
        self.ready = True
        self.keys = {"private_key": PRIVATE_KEY, "public_key": PUBLIC_KEY, "user": "alice"}

    def get_current_user(self):
        return copy.deepcopy(self.user)

    def get_task(self, kind, remote_id):
        self.calls.append(("get_task", kind, remote_id))
        return {"id": remote_id, "userId": self.owner, "state": self.state}

    def get_shell_keys(self, remote_id):
        self.calls.append(("get_shell_keys", remote_id))
        return {"id": remote_id, **self.keys}

    def get_task_info(self, task_id):
        self.calls.append(("get_task_info", task_id))
        return {
            "task_id": task_id,
            "allocations": [{"allocation_id": "a.1", "is_ready": self.ready, "end_time": None}],
        }


class Opener:
    def __init__(self, banner=(BANNER,)) -> None:
        self.banner = banner
        self.opened = []
        self.sockets = []

    def __call__(self, client):
        def open_websocket(shell_id, timeout):
            self.opened.append((shell_id, timeout))
            ws = FakeWebSocket(self.banner)
            self.sockets.append(ws)
            return ws

        return open_websocket


def _service(client):
    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    return ComputeService(client, profile)


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def opener():
    return Opener()


@pytest.fixture
def access(tmp_path, client, opener):
    manager = ShellAccess(_service(client), tmp_path / "access", opener)
    yield manager
    manager.close_all()


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _exchange(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        banner = b""
        while not banner.endswith(b"\r\n"):
            banner += connection.recv(1024)
        connection.sendall(payload)
        echoed = b""
        while len(echoed) < len(payload):
            echoed += connection.recv(1024)
        return banner, echoed


def _config(path):
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads(path.read_text())


# Helpers.


def test_fingerprint_matches_openssh():
    assert host_key_fingerprint(PUBLIC_KEY + " comment") == ("ssh-ed25519", FINGERPRINT)
    with pytest.raises(ValueError):
        host_key_fingerprint("ssh-ed25519")
    with pytest.raises(ValueError):
        host_key_fingerprint("ssh-ed25519 not*base64")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")
def test_fingerprint_matches_ssh_keygen_for_a_fresh_key(tmp_path):
    key = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)], check=True
    )
    listed = subprocess.run(
        ["ssh-keygen", "-lf", str(key) + ".pub", "-E", "sha256"],
        check=True, capture_output=True, text=True,
    ).stdout.split()[1]
    assert host_key_fingerprint((tmp_path / "key.pub").read_text())[1] == listed


def test_toml_string_escapes_quotes_backslashes_and_controls():
    assert toml_string('a"b\\c\nd\x7f') == '"a\\"b\\\\c\\u000Ad\\u007F"'
    assert toml_string("ünï") == '"ünï"'
    with pytest.raises(ValueError):
        toml_string("\udc80")


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
def _resolved(result):
    """What OpenSSH makes of the generated config for the result's alias."""
    arguments = shlex.split(result["ssh_command"])
    printed = subprocess.run(
        [arguments[0], "-G", *arguments[1:]], check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    return printed


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
def test_ssh_config_paths_survive_openssh_parsing(access, tmp_path, _private_control_directory):
    odd = tmp_path / 'with space "quote" 100%'
    manager = ShellAccess(access.service, odd / "access", Opener())
    try:
        result = manager.connect(SHELL_ID)
        printed = _resolved(result)
        assert f"userknownhostsfile {result['known_hosts_path']}" in printed
        # ssh -G prints IdentityFile before it expands % tokens.
        assert f"identityfile {result['key_path'].replace('%', '%%')}" in printed
        assert ssh_option_path('a b"c\\%') == '"a b\\"c\\\\%%"'
    finally:
        manager.close_all()


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
def test_ssh_config_gives_the_alias_everything_it_needs(access, _private_control_directory):
    result = access.connect(SHELL_ID)
    assert result["ssh_alias"] == "det-5b9c2f3e"
    assert result["ssh_command"] == shlex.join(["ssh", "-F", result["ssh_config_path"], "det-5b9c2f3e"])
    assert _mode(result["ssh_config_path"]) == 0o600
    control = _private_control_directory / "determined-compute"
    assert result["control_path_dir"] == str(control)
    assert _mode(control) == 0o700
    printed = _resolved(result)
    for line in (
        "hostname 127.0.0.1", f"port {result['port']}", "user alice",
        f"identityfile {result['key_path']}", "identitiesonly yes",
        f"userknownhostsfile {result['known_hosts_path']}", "stricthostkeychecking true",
        "batchmode yes", "loglevel ERROR", "serveraliveinterval 30",
        "controlmaster auto", "controlpersist 600",
    ):
        assert line in printed, line
    control_path = next(line for line in printed if line.startswith("controlpath "))
    assert control_path.startswith(f"controlpath {control}/") and len(control_path.split(" ", 1)[1]) < 104 - 17
    assert "fixture-private-key-material" not in Path(result["ssh_config_path"]).read_text()


def test_ssh_config_lists_open_tunnels_and_goes_with_the_last(access, tmp_path):
    first = access.connect(SHELL_ID)
    second = access.connect(OTHER_SHELL_ID)
    # Both IDs start with 5b9c2f3e: the second alias uses the full ID, and keeps it.
    assert second["ssh_alias"] == f"det-{OTHER_SHELL_ID}"
    text = Path(first["ssh_config_path"]).read_text()
    assert "Host det-5b9c2f3e\n" in text and f"Host det-{OTHER_SHELL_ID}\n" in text
    access.disconnect(SHELL_ID)
    assert access.connect(OTHER_SHELL_ID)["ssh_alias"] == f"det-{OTHER_SHELL_ID}"
    assert "Host det-5b9c2f3e\n" not in Path(first["ssh_config_path"]).read_text()
    access.disconnect(OTHER_SHELL_ID)
    assert not os.path.exists(first["ssh_config_path"])
    assert not Path(first["ssh_config_path"]).with_name("ssh_hosts").exists()


def test_the_control_directory_falls_back_in_order(client, tmp_path, monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="dc", dir="/tmp"))
    try:
        # A short shell-access directory holds its own sockets.
        manager = ShellAccess(_service(client), short / "a", Opener())
        assert manager.connect(SHELL_ID)["control_path_dir"] == str(short / "a" / "cm")
        manager.close_all()
        # Without a usable runtime directory, ~/.ssh/det-cm, if ~/.ssh exists and fits.
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent-runtime")
        monkeypatch.setenv("HOME", str(short / "h"))
        (short / "h" / ".ssh").mkdir(parents=True)
        manager = ShellAccess(_service(client), tmp_path / "long" / "access", Opener())
        assert manager.connect(SHELL_ID)["control_path_dir"] == str(short / "h" / ".ssh" / "det-cm")
        manager.close_all()
        # Nothing fits: no multiplexing, and the config says nothing about it.
        monkeypatch.setenv("HOME", str(tmp_path / "home-without-ssh"))
        manager = ShellAccess(_service(client), tmp_path / "long2" / "access", Opener())
        result = manager.connect(SHELL_ID)
        assert result["control_path_dir"] is None
        assert "Control" not in Path(result["ssh_config_path"]).read_text()
        manager.close_all()
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_a_control_directory_open_to_others_is_not_used(client, monkeypatch, _private_control_directory):
    shared = _private_control_directory / "determined-compute"
    shared.mkdir(mode=0o700)
    os.chmod(shared, 0o755)
    manager = ShellAccess(_service(client), Path("/nonexistent-but-long-enough-to-skip") / "x" / "access", Opener())
    try:
        assert manager._control_directory() is None
    finally:
        manager.close_all()


class _Clock:
    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.sleeps = []
        monkeypatch.setattr(module, "_now", lambda: self.now)
        monkeypatch.setattr(module, "_sleep", self.sleep)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _StartingClient(FakeClient):
    def __init__(self, states, readiness=()):
        super().__init__()
        self.states = list(states)
        self.readiness = list(readiness)

    def get_task(self, kind, remote_id):
        item = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(item, BaseException):
            raise item
        self.state = item
        return super().get_task(kind, remote_id)

    def get_task_info(self, task_id):
        if self.readiness:
            self.ready = self.readiness.pop(0)
        return super().get_task_info(task_id)


def test_wait_seconds_waits_for_the_shell_and_its_sshd(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(
        ["STATE_QUEUED", "STATE_PULLING", "STATE_STARTING", "STATE_RUNNING"], [False, False, True]
    )
    opener = Opener()
    manager = ShellAccess(_service(client), tmp_path / "access", opener)
    try:
        result = manager.connect(SHELL_ID, wait_seconds=120)
    finally:
        manager.close_all()
    assert result["ready"] is True and result["probe"]["ok"] is True
    assert clock.sleeps == [5, 5, 5, 5, 5]
    assert len(opener.opened) == 1


def test_wait_seconds_reports_a_shell_that_ended_at_once(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_QUEUED", "STATE_TERMINATED"])
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID, wait_seconds=300)
    assert caught.value.code == "shell_not_running"
    assert "STATE_TERMINATED" in str(caught.value)
    assert clock.sleeps == [5]


def test_wait_seconds_runs_out_while_queued(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_QUEUED"])
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID, wait_seconds=12)
    assert caught.value.code == "shell_not_running"
    assert "after waiting 12 s" in str(caught.value) and "connect again" in str(caught.value)
    assert clock.sleeps == [5, 5, 2]
    assert not (tmp_path / "access").exists()


def test_wait_seconds_returns_the_tunnel_when_sshd_is_still_starting(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_RUNNING"], [False] * 10)
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    try:
        result = manager.connect(SHELL_ID, wait_seconds=8)
        assert result["ready"] is False and result["probe"]["ok"] is False
        assert clock.sleeps == [5, 3]
        assert os.path.exists(result["key_path"])
    finally:
        manager.close_all()


def _transient():
    return APIError("Determined request failed", code="transport_error", retryable=True)


def test_wait_seconds_rides_out_transient_lookup_errors_while_queued(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient([_transient(), "STATE_QUEUED", _transient(), "STATE_RUNNING"])
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    try:
        assert manager.connect(SHELL_ID, wait_seconds=60)["probe"]["ok"] is True
    finally:
        manager.close_all()
    assert clock.sleeps == [5, 5, 5]


def test_a_definitive_lookup_error_while_queued_is_raised_at_once(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient([APIError("404 not found", code=404), "STATE_RUNNING"])
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID, wait_seconds=60)
    assert caught.value.code == 404 and clock.sleeps == []


def test_a_definitive_lookup_error_while_sshd_starts_closes_the_new_tunnel(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(
        ["STATE_RUNNING", "STATE_RUNNING", APIError("404 not found", code=404)], [False] * 10
    )
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID, wait_seconds=300)
    assert caught.value.code == 404
    assert clock.sleeps == [5]
    assert manager._tunnels == {} and not (tmp_path / "access" / SHELL_ID).exists()


def test_an_unverifiable_state_is_reported_as_unknown_not_running(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_RUNNING", "STATE_RUNNING", _transient()], [False] * 10)
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    try:
        result = manager.connect(SHELL_ID, wait_seconds=12)
        assert result["state"] is None
        assert "state" in result["context_unavailable"]
        assert result["probe"]["ok"] is False
        assert os.path.exists(result["key_path"])
    finally:
        manager.close_all()
    assert sum(clock.sleeps) == 12


def test_the_probe_gets_no_more_than_the_wait_has_left(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_RUNNING"])
    opener = Opener(banner=(b"HTTP/1.1 502 Bad Gateway\r\n",))
    manager = ShellAccess(_service(client), tmp_path / "access", opener)
    try:
        result = manager.connect(SHELL_ID, wait_seconds=8)
    finally:
        manager.close_all()
    assert result["probe"]["ok"] is False
    # One last probe at the deadline gets the 1-second floor, never the usual 15 seconds.
    assert [timeout for _, timeout in opener.opened] == [8, 3, 1]
    assert clock.sleeps == [5, 3]


@pytest.mark.parametrize("failing", [".ssh_hosts.tmp", ".ssh_config.tmp", ".ssh-mcp.toml.tmp"])
def test_a_full_disk_while_adding_a_shell_leaves_every_config_listing_only_the_others(
    access, tmp_path, monkeypatch, failing
):
    access.connect(OTHER_SHELL_ID)
    directory = tmp_path / "access"
    before = {name: (directory / name).read_text() for name in ("ssh_hosts", "ssh_config", SSH_MCP_CONFIG)}
    write, full = module._write_private, []

    def flaky(path, text):
        # The disk fills at the named file and stays full, the rollback's writes included.
        if full or path.name == failing:
            full.append(path.name)
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, text)

    monkeypatch.setattr(module, "_write_private", flaky)
    with pytest.raises(OSError):
        access.connect(SHELL_ID)
    assert {name: (directory / name).read_text() for name in before} == before
    assert not any(directory.glob(".*.tmp"))
    assert not (directory / SHELL_ID).exists() and list(access._tunnels) == [OTHER_SHELL_ID]


def test_a_failed_rename_leaves_no_configs_that_disagree(access, tmp_path, monkeypatch):
    access.connect(OTHER_SHELL_ID)
    directory = tmp_path / "access"
    replace, calls = os.replace, []

    def flaky(source, target):
        calls.append(Path(target).name)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error")
        replace(source, target)

    monkeypatch.setattr(module.os, "replace", flaky)
    with pytest.raises(OSError):
        access.connect(SHELL_ID)
    # The rollback rewrote the configs for the remaining tunnel alone.
    for name in ("ssh_hosts", "ssh_config", SSH_MCP_CONFIG):
        text = (directory / name).read_text()
        assert OTHER_SHELL_ID in text and SHELL_ID not in text
    assert not any(directory.glob(".*.tmp"))


def test_without_wait_seconds_a_queued_shell_says_how_to_wait(access, client):
    client.state = "STATE_QUEUED"
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert "pass wait_seconds" in str(caught.value)


@pytest.mark.parametrize("wait", [-1, 601, True, "5", 1.5])
def test_wait_seconds_is_validated(access, client, wait):
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID, wait_seconds=wait)
    assert caught.value.code == "invalid_request"
    assert client.calls == []


@pytest.mark.parametrize("user", ["x\nMatch exec touch /tmp/pwned #", "a\rb", "a${HOME}"])
def test_a_login_user_that_could_inject_ssh_config_is_refused(access, client, tmp_path, user):
    client.keys = {**client.keys, "user": user}
    with pytest.raises(ValueError):
        access.connect(SHELL_ID)
    directory = tmp_path / "access"
    assert not (directory / "ssh_config").exists()
    assert not (directory / SHELL_ID).exists()
    assert access._tunnels == {}


@pytest.mark.parametrize("name", ["a${HOME}b", "a\nb"])
def test_a_shell_access_directory_openssh_cannot_quote_is_refused(client, tmp_path, name):
    manager = ShellAccess(_service(client), tmp_path / name, Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID)
    assert caught.value.code == "invalid_request"
    assert not (tmp_path / name).exists()


def test_the_socket_budget_counts_bytes(client, _private_control_directory):
    short = Path(tempfile.mkdtemp(prefix="dc", dir="/tmp"))
    try:
        # Few characters, but more than 42 bytes once encoded.
        wide = short / ("目" * 10)
        assert len(str(wide / "cm")) <= 42 < len(os.fsencode(wide / "cm"))
        manager = ShellAccess(_service(client), wide, Opener())
        assert manager.connect(SHELL_ID)["control_path_dir"] == str(
            _private_control_directory / "determined-compute"
        )
        manager.close_all()
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_a_removed_control_directory_is_made_again(access):
    first = access.connect(SHELL_ID)
    control = Path(first["control_path_dir"])
    shutil.rmtree(control)
    again = access.connect(SHELL_ID)
    assert again["control_path_dir"] == str(control) and control.is_dir()
    assert _mode(control) == 0o700


def test_wait_seconds_stops_when_the_shell_ends_while_sshd_starts(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    client = _StartingClient(["STATE_RUNNING", "STATE_RUNNING", "STATE_TERMINATED"], [False] * 10)
    manager = ShellAccess(_service(client), tmp_path / "access", Opener())
    with pytest.raises(APIError) as caught:
        manager.connect(SHELL_ID, wait_seconds=300)
    assert caught.value.code == "shell_not_running"
    assert "stopped while waiting" in str(caught.value)
    assert clock.sleeps == [5]
    assert manager._tunnels == {} and not (tmp_path / "access" / SHELL_ID).exists()


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
def test_an_alias_without_a_tunnel_fails_at_once_without_a_lookup(access):
    result = access.connect(SHELL_ID)
    for alias in ("det-deadbeef", f"det-{OTHER_SHELL_ID}"):
        completed = subprocess.run(
            ["ssh", "-F", result["ssh_config_path"], alias, "true"],
            capture_output=True, text=True, timeout=20,
        )
        assert completed.returncode == 255
        assert "127.0.0.1 port 1" in completed.stderr
    # A real tunnel's block comes first, so the fallback changes nothing for it.
    printed = _resolved(result)
    assert f"port {result['port']}" in printed and "hostname 127.0.0.1" in printed


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
def test_including_ssh_hosts_leaves_the_users_own_hosts_alone(access, tmp_path):
    result = access.connect(SHELL_ID)
    hosts = Path(result["ssh_config_path"]).with_name("ssh_hosts")
    assert _mode(hosts) == 0o600
    assert "Port 1\n" not in hosts.read_text()
    user_config = tmp_path / "user_config"
    user_config.write_text(
        f'Include "{hosts}"\n\nHost det-abcdef12\n  HostName real.example\n  Port 2222\n'
    )

    def resolve(alias):
        return subprocess.run(
            ["ssh", "-G", "-F", str(user_config), alias], check=True, capture_output=True, text=True
        ).stdout.splitlines()

    own = resolve("det-abcdef12")
    assert "hostname real.example" in own and "port 2222" in own
    tunnel = resolve(result["ssh_alias"])
    assert "hostname 127.0.0.1" in tunnel and f"port {result['port']}" in tunnel


def test_a_reused_connect_returns_no_static_advisories(access):
    assert access.connect(SHELL_ID)["advisories"]
    again = access.connect(SHELL_ID)
    assert again["reused"] is True and again["advisories"] == []
    assert "profile_toml" not in again["ssh_mcp"]


def test_handshake_errors_keep_only_the_status():
    websocket = pytest.importorskip("websocket")
    refused = websocket.WebSocketBadStatusException(
        "Handshake status 401 Unauthorized -+-+- {'set-cookie': 'session=SECRET'} -+-+- body",
        401, "Unauthorized", {"set-cookie": "session=SECRET"}, b"SECRET body",
    )
    assert transport_error(refused) == "the WebSocket handshake was refused with HTTP 401"
    assert transport_error(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")) == (
        "ConnectionRefusedError: Connection refused"
    )
    assert transport_error(TimeoutError("timed out")) == "TimeoutError: timed out"
    assert transport_error(websocket.WebSocketException("Redirect to http://x/?token=SECRET")) == (
        "WebSocketException"
    )
    assert transport_error(
        websocket.WebSocketProxyException("failed CONNECT via proxy status: 407")
    ) == "WebSocketProxyException: failed CONNECT via proxy status: 407"


# Connecting.


def test_connect_writes_private_key_and_relays_to_the_shell(access, client, opener, tmp_path):
    result = access.connect(SHELL_ID)

    directory = tmp_path / "access"
    key_path = directory / SHELL_ID / "key"
    assert _mode(directory) == 0o700
    assert _mode(directory / SHELL_ID) == 0o700
    assert _mode(key_path) == 0o600
    assert key_path.read_text() == PRIVATE_KEY + "\n"
    assert sorted(path.name for path in (directory / SHELL_ID).iterdir()) == ["key", "known_hosts"]
    # The private key never leaves the key file.
    assert "fixture-private-key-material" not in json.dumps(result)

    port = result["port"]
    assert result["host"] == "127.0.0.1" and isinstance(port, int) and port > 0
    assert result["kind"] == "shell" and result["id"] == SHELL_ID
    assert result["state"] == "STATE_RUNNING" and result["ready"] is True
    assert result["reused"] is False
    assert result["user"] == "alice"
    assert result["key_path"] == str(key_path)
    assert result["host_key_type"] == "ssh-ed25519"
    assert result["host_key_fingerprint"] == FINGERPRINT
    assert result["probe"] == {"ok": True, "banner": "SSH-2.0-OpenSSH_9.6 fixture"}
    assert (directory / SHELL_ID / "known_hosts").read_text() == (
        f"[127.0.0.1]:{port} {PUBLIC_KEY}\n"
    )
    assert result["ssh_command"] == shlex.join(
        ["ssh", "-F", str(directory / "ssh_config"), "det-5b9c2f3e"]
    )
    assert {advisory["code"] for advisory in result["advisories"]} == {
        "tunnel_lifetime", "ssh_mcp_reload",
    }

    banner, echoed = _exchange(port, b"client bytes")
    assert banner == BANNER and echoed == b"client bytes"
    # The probe and the connection each opened the shell's proxy, without a timeout for SSH.
    assert opener.opened == [(SHELL_ID, 15), (SHELL_ID, None)]


def test_a_relative_directory_gives_absolute_paths(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = ShellAccess(_service(client), "relative-access", Opener())
    try:
        result = manager.connect(SHELL_ID)
    finally:
        manager.close_all()
    for path in (result["key_path"], result["known_hosts_path"], result["ssh_mcp"]["config_path"]):
        assert os.path.isabs(path) and path.startswith(str(tmp_path / "relative-access"))


def test_tunnel_listens_on_loopback_only(access):
    port = access.connect(SHELL_ID)["port"]
    addresses = {
        info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    } - {"127.0.0.1", "127.0.1.1"}
    for address in addresses:
        with pytest.raises(OSError):
            socket.create_connection((address, port), timeout=1).close()


def test_generated_ssh_mcp_config_uses_only_its_schema_keys(access, tmp_path):
    result = access.connect(SHELL_ID)
    path = tmp_path / "access" / SSH_MCP_CONFIG
    name = f"det-shell-{SHELL_ID}"
    assert result["ssh_mcp"]["config_path"] == str(path)
    assert result["ssh_mcp"]["profile"] == name
    assert _mode(path) == 0o600
    assert "fixture-private-key-material" not in path.read_text()
    config = _config(path)
    assert "profile_toml" not in result["ssh_mcp"]
    # No defaultProfile: a caller names the profile it means.
    assert "defaults" not in config
    assert config["profiles"] == [{
        "name": name,
        "host": "127.0.0.1",
        "port": result["port"],
        "user": "alice",
        "auth": "key",
        "keyRef": result["key_path"],
        "trustedHostKey": FINGERPRINT,
        "group": "dev",
    }]


def test_profile_names_are_full_shell_ids_and_stay_stable(access, tmp_path):
    first = access.connect(SHELL_ID)
    second = access.connect(OTHER_SHELL_ID)
    assert first["ssh_mcp"]["profile"] == f"det-shell-{SHELL_ID}"
    assert second["ssh_mcp"]["profile"] == f"det-shell-{OTHER_SHELL_ID}"
    assert sorted(p["name"] for p in _config(tmp_path / "access" / SSH_MCP_CONFIG)["profiles"]) == [
        f"det-shell-{SHELL_ID}", f"det-shell-{OTHER_SHELL_ID}",
    ]
    access.disconnect(OTHER_SHELL_ID)
    assert access.connect(SHELL_ID)["ssh_mcp"]["profile"] == f"det-shell-{SHELL_ID}"


def test_connect_again_reuses_the_tunnel_and_another_port_is_a_conflict(access, client):
    first = access.connect(SHELL_ID)
    again = access.connect(SHELL_ID)
    assert again["reused"] is True and again["port"] == first["port"]
    assert access.connect(SHELL_ID, first["port"])["reused"] is True
    assert [call for call in client.calls if call[0] == "get_shell_keys"] == [
        ("get_shell_keys", SHELL_ID)
    ]
    other_port = first["port"] + 1 if first["port"] < 65535 else 1024
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID, other_port)
    assert caught.value.code == "shell_access_conflict"
    # The open tunnel is untouched.
    assert _exchange(first["port"], b"x")[1] == b"x"


def test_disconnect_stops_the_tunnel_and_deletes_key_and_config(access, tmp_path):
    result = access.connect(SHELL_ID)
    _exchange(result["port"], b"x")
    assert access.disconnect(SHELL_ID) == {"kind": "shell", "id": SHELL_ID, "disconnected": True}
    assert not (tmp_path / "access" / SHELL_ID).exists()
    assert not (tmp_path / "access" / SSH_MCP_CONFIG).exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", result["port"]), timeout=1).close()
    assert access.disconnect(SHELL_ID)["disconnected"] is False


def test_disconnect_drops_open_connections(access):
    result = access.connect(SHELL_ID)
    connection = socket.create_connection(("127.0.0.1", result["port"]), timeout=5)
    try:
        assert connection.recv(1024) == BANNER
        access.disconnect(SHELL_ID)
        assert connection.recv(1024) == b""
    finally:
        connection.close()


@pytest.mark.parametrize("state", ["STATE_QUEUED", "STATE_TERMINATED", None])
def test_only_a_running_shell_is_connected(access, client, tmp_path, state):
    client.state = state
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "shell_not_running"
    assert ("get_shell_keys", SHELL_ID) not in client.calls
    assert not (tmp_path / "access").exists()


def test_another_accounts_shell_is_refused_before_its_keys_are_read(access, client, tmp_path):
    client.owner = "8"
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "ownership_mismatch"
    assert ("get_shell_keys", SHELL_ID) not in client.calls
    assert not (tmp_path / "access").exists()


def test_a_shell_that_is_not_ready_is_connected_without_a_probe(access, client, opener):
    client.ready = False
    result = access.connect(SHELL_ID)
    assert result["ready"] is False
    assert result["probe"]["ok"] is False
    assert opener.opened == []


@pytest.mark.parametrize(
    "banner, expected",
    [
        ((b"SS", b"H-2.0-OpenSSH_9.6 fixture\r\n"), {"ok": True, "banner": "SSH-2.0-OpenSSH_9.6 fixture"}),
        ((b"SSH-", b"2.0-OpenSSH_9.6 fixture\r\n"), {"ok": True, "banner": "SSH-2.0-OpenSSH_9.6 fixture"}),
        ((b"HTTP/1.1 502\r\n",), {"ok": False, "error": "the shell's proxy did not answer with an SSH banner"}),
    ],
)
def test_the_probe_reads_the_banner_across_messages(client, tmp_path, banner, expected):
    manager = ShellAccess(_service(client), tmp_path / "access", Opener(banner))
    try:
        assert manager.connect(SHELL_ID)["probe"] == expected
    finally:
        manager.close_all()


def test_the_probe_makes_sshd_close_its_side(access, opener):
    access.connect(SHELL_ID)
    assert opener.sockets[0].sent == [b"probe\r\n"]
    assert opener.sockets[0].closed.is_set()


def test_a_refused_handshake_reports_only_its_status(client, tmp_path, capsys):
    websocket = pytest.importorskip("websocket")

    def refusing(_client):
        def open_websocket(shell_id, timeout):
            raise websocket.WebSocketBadStatusException(
                "Handshake status 401 -+-+- {'set-cookie': 'session=SECRET'} -+-+- SECRET",
                401, "Unauthorized", {"set-cookie": "session=SECRET"}, b"SECRET",
            )
        return open_websocket

    manager = ShellAccess(_service(client), tmp_path / "access", refusing)
    try:
        result = manager.connect(SHELL_ID)
        assert result["probe"] == {
            "ok": False, "error": "the WebSocket handshake was refused with HTTP 401",
        }
        assert (tmp_path / "access" / SHELL_ID / "key").exists()
        # A relayed connection fails the same way, and stderr gets the status alone.
        with socket.create_connection(("127.0.0.1", result["port"]), timeout=5) as connection:
            assert connection.recv(1) == b""
        time.sleep(0.2)
        assert "SECRET" not in capsys.readouterr().err
    finally:
        manager.close_all()


@pytest.mark.parametrize("port", [True, 0, 80, 65536, "2222"])
def test_local_port_is_validated(access, client, port):
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID, port)
    assert caught.value.code == "invalid_request"
    assert client.calls == []


def test_a_busy_port_is_reported_and_leaves_nothing(access, tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(APIError) as caught:
            access.connect(SHELL_ID, busy.getsockname()[1])
    assert caught.value.code == "port_unavailable"
    assert not (tmp_path / "access" / SHELL_ID).exists()
    assert access._tunnels == {}


@pytest.mark.parametrize("failing", ["known_hosts", SSH_MCP_CONFIG])
def test_a_failed_creation_leaves_nothing_and_the_next_connect_works(
    access, tmp_path, monkeypatch, failing
):
    write = module._write_private

    def flaky(path, text):
        if path.name == failing or path.name == f".{failing}.tmp":
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, text)

    monkeypatch.setattr(module, "_write_private", flaky)
    with pytest.raises(OSError):
        access.connect(SHELL_ID)
    directory = tmp_path / "access"
    assert not (directory / SHELL_ID).exists()
    assert not (directory / SSH_MCP_CONFIG).exists()
    assert not (directory / f".{SSH_MCP_CONFIG}.tmp").exists()
    assert access._tunnels == {}

    monkeypatch.setattr(module, "_write_private", write)
    result = access.connect(SHELL_ID)
    assert result["reused"] is False
    assert os.path.exists(result["ssh_mcp"]["config_path"])
    assert os.path.exists(result["key_path"])


def test_a_failed_config_write_keeps_the_other_tunnels_profiles(access, tmp_path, monkeypatch):
    access.connect(OTHER_SHELL_ID)
    write = module._write_private

    def flaky(path, text):
        if path.name == "known_hosts":
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, text)

    monkeypatch.setattr(module, "_write_private", flaky)
    with pytest.raises(OSError):
        access.connect(SHELL_ID)
    profiles = _config(tmp_path / "access" / SSH_MCP_CONFIG)["profiles"]
    assert [profile["name"] for profile in profiles] == [f"det-shell-{OTHER_SHELL_ID}"]


# The directory.


def test_one_process_uses_a_directory(access, client, tmp_path):
    access.connect(SHELL_ID)
    other = ShellAccess(_service(client), tmp_path / "access", Opener())
    try:
        with pytest.raises(APIError) as caught:
            other.connect(OTHER_SHELL_ID)
        assert caught.value.code == "shell_access_conflict"
        assert "--shell-access-dir" in str(caught.value)
        # Its startup sweep leaves the directory of a running instance alone.
        other.sweep()
        assert (tmp_path / "access" / SHELL_ID / "key").exists()
        assert (tmp_path / "access" / SSH_MCP_CONFIG).exists()
    finally:
        other.close_all()
    access.close_all()
    # Released at exit: another instance can take the directory over.
    replacement = ShellAccess(_service(client), tmp_path / "access", Opener())
    try:
        assert replacement.connect(OTHER_SHELL_ID)["reused"] is False
    finally:
        replacement.close_all()


def test_sweep_and_claim_delete_only_shell_directories(access, tmp_path):
    directory = tmp_path / "access"
    directory.mkdir(mode=0o700)
    (directory / "project" / "src").mkdir(parents=True)
    (directory / "project" / "src" / "main.py").write_text("keep")
    (directory / OTHER_SHELL_ID).mkdir()
    (directory / OTHER_SHELL_ID / "notes.txt").write_text("keep")
    (directory / SHELL_ID.upper()).mkdir()
    (directory / "notes.txt").write_text("keep")
    stale = directory / "00000000-0000-4000-8000-000000000000"
    stale.mkdir()
    (stale / "key").write_text("stale")
    (directory / SSH_MCP_CONFIG).write_text("stale")
    access.sweep()
    assert not stale.exists()
    assert not (directory / SSH_MCP_CONFIG).exists()
    assert (directory / "project" / "src" / "main.py").read_text() == "keep"
    assert (directory / OTHER_SHELL_ID / "notes.txt").exists()
    assert (directory / SHELL_ID.upper()).exists()
    assert (directory / "notes.txt").exists()


def test_connect_never_deletes_a_foreign_directory_named_by_the_shell(access, client, tmp_path):
    directory = tmp_path / "access"
    directory.mkdir(mode=0o700)
    (directory / SHELL_ID).mkdir()
    (directory / SHELL_ID / "notes.txt").write_text("keep")
    access.sweep()
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "shell_access_conflict"
    assert (directory / SHELL_ID / "notes.txt").read_text() == "keep"
    assert access._tunnels == {}


@pytest.mark.parametrize("name", ["key", "known_hosts"])
def test_a_shell_named_directory_holding_a_subdirectory_is_never_deleted(access, tmp_path, name):
    directory = tmp_path / "access"
    directory.mkdir(mode=0o700)
    (directory / SHELL_ID / name).mkdir(parents=True)
    (directory / SHELL_ID / name / "thesis.tex").write_text("keep")
    access.sweep()
    assert (directory / SHELL_ID / name / "thesis.tex").read_text() == "keep"
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "shell_access_conflict"
    assert (directory / SHELL_ID / name / "thesis.tex").read_text() == "keep"


def test_a_deleted_directory_is_claimed_again_so_it_is_never_shared(access, client, tmp_path):
    directory = tmp_path / "access"
    access.connect(SHELL_ID)
    access.disconnect(SHELL_ID)
    for entry in directory.iterdir():
        entry.unlink()
    directory.rmdir()
    other = ShellAccess(_service(client), directory, Opener())
    try:
        other.connect(OTHER_SHELL_ID)
        with pytest.raises(APIError) as caught:
            access.connect(SHELL_ID)
        assert caught.value.code == "shell_access_conflict"
        assert (directory / OTHER_SHELL_ID / "key").exists()
    finally:
        other.close_all()
    # Once the other server is gone, this one takes the directory again.
    assert access.connect(SHELL_ID)["reused"] is False


def test_a_failed_config_rewrite_on_disconnect_drops_the_config(access, tmp_path, monkeypatch):
    access.connect(SHELL_ID)
    access.connect(OTHER_SHELL_ID)

    def failing(path, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    write = module._write_private
    monkeypatch.setattr(module, "_write_private", failing)
    assert access.disconnect(SHELL_ID)["disconnected"] is True
    directory = tmp_path / "access"
    assert not (directory / SSH_MCP_CONFIG).exists()
    assert not (directory / "ssh_config").exists() and not (directory / "ssh_hosts").exists()
    assert not (directory / f".{SSH_MCP_CONFIG}.tmp").exists()
    assert not (directory / SHELL_ID).exists()

    # Once writes work again, a reused connect restores the config from the open tunnels.
    monkeypatch.setattr(module, "_write_private", write)
    result = access.connect(OTHER_SHELL_ID)
    assert result["reused"] is True
    profiles = _config(Path(result["ssh_mcp"]["config_path"]))["profiles"]
    assert [profile["name"] for profile in profiles] == [f"det-shell-{OTHER_SHELL_ID}"]


def test_a_removed_lock_file_without_another_server_is_locked_again(access, tmp_path):
    directory = tmp_path / "access"
    access.connect(SHELL_ID)
    access.connect(OTHER_SHELL_ID)
    (directory / ".lock").unlink()
    assert access.connect(SHELL_ID)["reused"] is True
    assert (directory / ".lock").exists()
    # The new lock file is held: another server cannot take the directory.
    other = ShellAccess(access.service, directory, Opener())
    try:
        with pytest.raises(APIError) as caught:
            other.connect(SHELL_ID)
        assert "another determined-compute-mcp process" in str(caught.value)
    finally:
        other.close_all()
    assert access.disconnect(SHELL_ID)["disconnected"] is True
    assert not (directory / SHELL_ID).exists()
    profiles = _config(directory / SSH_MCP_CONFIG)["profiles"]
    assert [profile["name"] for profile in profiles] == [f"det-shell-{OTHER_SHELL_ID}"]
    access.close_all()
    assert sorted(path.name for path in directory.iterdir()) == [".lock"]


def test_files_another_server_left_behind_are_not_adopted(access, client, tmp_path):
    directory = tmp_path / "access"
    access.connect(SHELL_ID)
    (directory / ".lock").unlink()
    # Another server takes the directory and the same shell, then vanishes without cleaning
    # up: its lock is gone, but its key and known_hosts remain under the same names.
    other = ShellAccess(_service(client), directory, Opener())
    other.connect(SHELL_ID)
    other_known_hosts = (directory / SHELL_ID / "known_hosts").read_text()
    other._tunnels[SHELL_ID].server.server_close()
    other._release_directory()
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "shell_access_conflict"
    assert "removed or taken over" in str(caught.value)
    # The other server's files are left alone.
    assert (directory / SHELL_ID / "known_hosts").read_text() == other_known_hosts


@pytest.mark.parametrize("removed", ["directory", "lock"])
def test_a_server_that_lost_its_directory_leaves_the_new_owners_files_alone(
    access, client, tmp_path, removed
):
    directory = tmp_path / "access"
    first = access.connect(SHELL_ID)
    if removed == "directory":
        for path in sorted(directory.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        directory.rmdir()
    else:
        (directory / ".lock").unlink()
    owner = ShellAccess(_service(client), directory, Opener())
    try:
        taken = owner.connect(SHELL_ID)
        owned = [directory / SHELL_ID / "key", directory / SHELL_ID / "known_hosts", directory / SSH_MCP_CONFIG]
        assert all(path.exists() for path in owned)

        for shell_id in (SHELL_ID, OTHER_SHELL_ID):
            with pytest.raises(APIError) as caught:
                access.connect(shell_id)
            assert caught.value.code == "shell_access_conflict"
            assert "removed or taken over while this server had open tunnels" in str(caught.value)

        assert access.disconnect(SHELL_ID)["disconnected"] is True
        assert all(path.exists() for path in owned)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", first["port"]), timeout=1).close()
        assert _exchange(taken["port"], b"still mine")[1] == b"still mine"

        access.close_all()
        assert all(path.exists() for path in owned)
        # Without tunnels it may try again, and finds the directory taken.
        with pytest.raises(APIError) as caught:
            access.connect(SHELL_ID)
        assert "another determined-compute-mcp process" in str(caught.value)
    finally:
        owner.close_all()


def test_direct_connections_disable_nagle_and_keep_alive():
    with socket.create_server(("127.0.0.1", 0)) as listener:
        connection = module._direct_socket("127.0.0.1", listener.getsockname()[1], None, 5)
        try:
            assert connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            assert connection.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            if hasattr(socket, "TCP_KEEPIDLE"):
                assert connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
        finally:
            connection.close()


def test_connect_replaces_a_stale_shell_directory(access, tmp_path):
    directory = tmp_path / "access"
    directory.mkdir(mode=0o700)
    (directory / SHELL_ID).mkdir()
    (directory / SHELL_ID / "key").write_text("stale")
    result = access.connect(SHELL_ID)
    assert (directory / SHELL_ID / "key").read_text() == PRIVATE_KEY + "\n"
    assert result["reused"] is False


def test_sweep_leaves_a_missing_directory_alone(tmp_path):
    manager = ShellAccess(object(), tmp_path / "absent")
    manager.sweep()
    assert not (tmp_path / "absent").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_directory_open_to_other_users_is_refused_unchanged(access, tmp_path):
    directory = tmp_path / "access"
    directory.mkdir()
    os.chmod(directory, 0o755)
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "invalid_request"
    assert _mode(directory) == 0o755
    assert list(directory.iterdir()) == []


# The relay.


def test_relay_ends_on_close_frame_and_forwards_client_eof():
    left, right = socket.socketpair()
    ws = FakeWebSocket(banner=())
    worker = threading.Thread(target=relay, args=(right, ws))
    worker.start()
    left.sendall(b"abc")
    assert left.recv(3) == b"abc"
    left.shutdown(socket.SHUT_WR)
    # Client EOF asks the proxy to close; its close frame ends the relay.
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert ws.closed.is_set()
    assert left.recv(1) == b""
    left.close()
    right.close()


def test_relay_ends_when_the_proxy_never_answers_the_close(monkeypatch):
    monkeypatch.setattr(module, "_CLOSE_TIMEOUT", 0.2)
    proxy_side, proxy_peer = socket.socketpair()

    class SilentWebSocket:
        sock = proxy_side

        def send_binary(self, data):
            pass

        def send_close(self):
            pass

        def recv_data(self):
            data = proxy_side.recv(1024)
            if not data:
                raise OSError("closed")
            return 0x2, data

        def shutdown(self):
            proxy_side.close()

    left, right = socket.socketpair()
    worker = threading.Thread(target=relay, args=(right, SilentWebSocket()))
    worker.start()
    left.shutdown(socket.SHUT_WR)
    worker.join(timeout=5)
    assert not worker.is_alive()
    for item in (left, right, proxy_peer):
        item.close()


def test_abort_wakes_a_receive_blocked_on_the_websocket_socket():
    proxy_side, proxy_peer = socket.socketpair()

    class BlockedWebSocket:
        sock = proxy_side

        def shutdown(self):
            proxy_side.close()

    received = []
    reader = threading.Thread(target=lambda: received.append(proxy_side.recv(1)))
    reader.start()
    abort_websocket(BlockedWebSocket())
    reader.join(timeout=5)
    assert not reader.is_alive() and received == [b""]
    proxy_peer.close()


def test_a_connection_opened_while_the_tunnel_stops_is_not_relayed(access):
    access.connect(SHELL_ID)
    server = access._tunnels[SHELL_ID].server
    server.drop_connections()
    late = FakeWebSocket()
    left, right = socket.socketpair()
    with server.tracking(right, late) as admitted:
        assert admitted is False
    assert late.closed.is_set()
    left.close()
    right.close()


# The WebSocket opener.


class _Recorder:
    def __init__(self, monkeypatch):
        websocket = pytest.importorskip("websocket")
        self.calls = []
        self.sockets = []

        class Connection:
            def settimeout(inner, timeout):
                self.calls.append(("settimeout", timeout))

        def create_connection(url, **options):
            self.calls.append((url, options))
            return Connection()

        def direct_socket(host, port, context, timeout):
            self.sockets.append((host, port, context, timeout))
            return object()

        monkeypatch.setattr(websocket, "create_connection", create_connection)
        monkeypatch.setattr(module, "_direct_socket", direct_socket)


def test_websocket_opener_targets_the_shell_proxy_directly(monkeypatch):
    recorder = _Recorder(monkeypatch)
    client = FakeClient()
    client.api_url = "http://det.example.test:8080/base/"
    websocket_opener(client)(SHELL_ID, None)
    url, options = recorder.calls[0]
    assert url == f"ws://det.example.test:8080/base/proxy/{SHELL_ID}/"
    assert options["header"] == {"Authorization": "Bearer fixture-token"}
    assert options["timeout"] == 30 and options["enable_multithread"] is True
    assert options["redirect_limit"] == 0
    assert "http_proxy_host" not in options
    assert recorder.sockets == [("det.example.test", 8080, None, 30)]
    assert recorder.calls[1] == ("settimeout", None)


def test_websocket_opener_verifies_tls_as_requests_does(monkeypatch, tmp_path):
    recorder = _Recorder(monkeypatch)
    client = FakeClient()
    client.api_url = "https://det.example.test"
    websocket_opener(client)(SHELL_ID, 15)
    assert recorder.calls[0][0] == f"wss://det.example.test/proxy/{SHELL_ID}/"
    host, port, context, timeout = recorder.sockets[0]
    assert (host, port, timeout) == ("det.example.test", 443, 15)
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False

    client.verify_ssl = True
    certificates = tmp_path / "certificates"
    certificates.mkdir()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificates))
    websocket_opener(client)(SHELL_ID, 15)
    context = recorder.sockets[1][2]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


@pytest.mark.parametrize(
    "environment, proxied",
    [
        ({"HTTPS_PROXY": "http://proxy.test:3128"}, True),
        ({"ALL_PROXY": "http://proxy.test:3128"}, True),
        ({"HTTPS_PROXY": "http://proxy.test:3128", "NO_PROXY": "det.example.test:8443"}, False),
        ({"HTTPS_PROXY": "http://proxy.test:3128", "NO_PROXY": "example.test"}, False),
    ],
)
def test_websocket_opener_uses_the_proxy_requests_would(monkeypatch, environment, proxied):
    recorder = _Recorder(monkeypatch)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    client = FakeClient()
    client.api_url = "https://det.example.test:8443"
    websocket_opener(client)(SHELL_ID, None)
    options = recorder.calls[0][1]
    if proxied:
        assert options["http_proxy_host"] == "proxy.test"
        assert options["http_proxy_port"] == 3128
        assert options["proxy_type"] == "http"
        assert options["http_no_proxy"] and "det.example.test" not in options["http_no_proxy"]
        assert isinstance(options["sslopt"]["context"], ssl.SSLContext)
        assert recorder.sockets == []
    else:
        assert "http_proxy_host" not in options
        assert recorder.sockets[0][:2] == ("det.example.test", 8443)


def test_websocket_opener_passes_proxy_credentials(monkeypatch):
    recorder = _Recorder(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://us%40er:p%3Ass@proxy.test")
    websocket_opener(FakeClient())(SHELL_ID, None)
    options = recorder.calls[0][1]
    assert options["http_proxy_auth"] == ("us@er", "p:ss")
    assert options["http_proxy_port"] == 80


def test_websocket_opener_refuses_a_proxy_it_cannot_use(monkeypatch):
    _Recorder(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.test:1080")
    with pytest.raises(APIError) as caught:
        websocket_opener(FakeClient())
    assert caught.value.code == "unsupported"
    assert "socks5" in str(caught.value)


def test_missing_websocket_client_is_reported_as_unsupported(monkeypatch, client):
    monkeypatch.setitem(sys.modules, "websocket", None)
    with pytest.raises(APIError) as caught:
        websocket_opener(client)
    assert caught.value.code == "unsupported"
    assert "determined-compute[mcp]" in str(caught.value)


# OpenSSH through the relay and a real WebSocket to a real sshd.

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(connection, size):
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _frame(opcode, payload):
    head = bytes([0x80 | opcode])
    if len(payload) < 126:
        head += bytes([len(payload)])
    elif len(payload) < 65536:
        head += bytes([126]) + len(payload).to_bytes(2, "big")
    else:
        head += bytes([127]) + len(payload).to_bytes(8, "big")
    return head + payload


class ShellProxy:
    """A master's shell proxy: WebSocket at /proxy/<id>/, bridged to a TCP port.

    It splits sshd's first bytes across two messages, as a proxy may.
    """

    def __init__(self, shell_id, target_port):
        self.path = f"/proxy/{shell_id}/"
        self.target_port = target_port
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.authorizations = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._bridge, args=(connection,), daemon=True).start()

    def _bridge(self, connection):
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        lines = request.decode("latin-1").split("\r\n")
        headers = {
            line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
            for line in lines[1:] if ":" in line
        }
        if lines[0].split()[1] != self.path:
            connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            connection.close()
            return
        self.authorizations.append(headers.get("authorization"))
        accept = base64.b64encode(
            hashlib.sha1(headers["sec-websocket-key"].encode() + _GUID).digest()
        ).decode()
        connection.sendall(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        sshd = socket.create_connection(("127.0.0.1", self.target_port))
        send_lock = threading.Lock()

        def downstream():
            first = True
            try:
                while True:
                    data = sshd.recv(65536)
                    if not data:
                        break
                    with send_lock:
                        if first and len(data) > 2:
                            connection.sendall(_frame(0x2, data[:2]) + _frame(0x2, data[2:]))
                        else:
                            connection.sendall(_frame(0x2, data))
                    first = False
                with send_lock:
                    connection.sendall(_frame(0x8, b""))
            except OSError:
                pass

        threading.Thread(target=downstream, daemon=True).start()
        try:
            while True:
                head = _recv_exact(connection, 2)
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(_recv_exact(connection, 2), "big")
                elif length == 127:
                    length = int.from_bytes(_recv_exact(connection, 8), "big")
                mask = _recv_exact(connection, 4) if head[1] & 0x80 else b"\0\0\0\0"
                payload = bytes(
                    byte ^ mask[index % 4]
                    for index, byte in enumerate(_recv_exact(connection, length))
                )
                if opcode == 0x8:
                    with send_lock:
                        connection.sendall(_frame(0x8, b""))
                    break
                if opcode == 0x9:
                    with send_lock:
                        connection.sendall(_frame(0xA, payload))
                elif opcode in (0x0, 0x1, 0x2):
                    sshd.sendall(payload)
        except (EOFError, OSError):
            pass
        finally:
            for item in (sshd, connection):
                with_suppress_close(item)

    def close(self):
        self.listener.close()


def with_suppress_close(item):
    try:
        item.close()
    except OSError:
        pass


@pytest.fixture
def sshd(tmp_path):
    binary = shutil.which("sshd") or "/usr/sbin/sshd"
    if not os.path.exists(binary) or shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("OpenSSH client and server are not installed")
    keys = tmp_path / "sshd"
    keys.mkdir()
    # Determined uses one generated pair for the host key and the login key.
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(keys / "pair")],
        check=True,
    )
    shutil.copy(keys / "pair.pub", keys / "authorized_keys")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (keys / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {keys / 'pair'}\n"
        f"AuthorizedKeysFile {keys / 'authorized_keys'}\nPidFile none\nStrictModes no\n"
        "UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    )
    process = subprocess.Popen(
        [binary, "-D", "-e", "-f", str(keys / "sshd_config")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.skip(f"sshd did not start: {process.stderr.read().decode()[:200]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        pytest.skip("sshd did not start listening")
    yield {
        "port": port,
        "private_key": (keys / "pair").read_text(),
        "public_key": (keys / "pair.pub").read_text().strip(),
    }
    process.kill()
    process.wait()


def test_openssh_logs_in_through_the_relay_and_a_real_websocket(sshd, client, tmp_path):
    proxy = ShellProxy(SHELL_ID, sshd["port"])
    client.api_url = f"http://127.0.0.1:{proxy.port}"
    client.keys = {
        "private_key": sshd["private_key"],
        "public_key": sshd["public_key"],
        "user": getpass.getuser(),
    }
    relayed = []

    def counting(c):
        open_websocket = websocket_opener(c)

        def opened(shell_id, timeout):
            if timeout is None:
                relayed.append(shell_id)
            return open_websocket(shell_id, timeout)

        return opened

    # A path with a space checks the quoting of the generated config for real.
    manager = ShellAccess(_service(client), tmp_path / "access dir", counting)
    try:
        result = manager.connect(SHELL_ID, wait_seconds=30)
        assert result["probe"]["ok"] is True, result["probe"]
        assert result["probe"]["banner"].startswith("SSH-2.0-")
        assert result["control_path_dir"] is not None
        ssh = shlex.split(result["ssh_command"])
        payload = os.urandom(1 << 20)
        completed = subprocess.run(
            ssh + ["id -un && sha256sum"], input=payload, capture_output=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        user, digest = completed.stdout.decode().split("\n", 1)
        assert user == getpass.getuser()
        assert digest.split()[0] == hashlib.sha256(payload).hexdigest()
        # Later commands share the master: nothing but their own output, one relayed connection.
        for index in range(4):
            completed = subprocess.run(ssh + [f"echo run{index}"], capture_output=True, timeout=60)
            assert (completed.returncode, completed.stdout, completed.stderr) == (
                0, f"run{index}\n".encode(), b""
            )
        assert relayed == [SHELL_ID]
        check = subprocess.run(ssh[:-1] + ["-O", "check", ssh[-1]], capture_output=True, text=True)
        assert "Master running" in check.stderr
        assert set(proxy.authorizations) == {"Bearer fixture-token"}
        # Disconnecting drops the relayed connection, so the master ends with it.
        manager.disconnect(SHELL_ID)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(
            ssh[:-1] + ["-O", "check", ssh[-1]], capture_output=True
        ).returncode == 0:
            time.sleep(0.1)
        assert not any(Path(result["control_path_dir"]).iterdir())
    finally:
        manager.close_all()
        proxy.close()
