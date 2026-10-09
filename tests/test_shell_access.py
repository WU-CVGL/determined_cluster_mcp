"""Local SSH access to running shells: keys, tunnel, generated ssh-mcp config, cleanup."""

from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading

import pytest

from determined_compute.compute import ComputeProfile, ComputeService, ShellAccess
from determined_compute.compute.shell_access import (
    SSH_MCP_CONFIG,
    host_key_fingerprint,
    relay,
    toml_string,
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


class FakeWebSocket:
    """A shell proxy that sends the sshd banner, then echoes binary frames."""

    def __init__(self, banner=BANNER) -> None:
        self.incoming = queue.Queue()
        if banner:
            self.incoming.put((0x2, banner))
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
        self.keys_user = "root"

    def get_current_user(self):
        return copy.deepcopy(self.user)

    def get_task(self, kind, remote_id):
        self.calls.append(("get_task", kind, remote_id))
        return {"id": remote_id, "userId": self.owner, "state": self.state}

    def get_shell_keys(self, remote_id):
        self.calls.append(("get_shell_keys", remote_id))
        return {
            "id": remote_id,
            "private_key": PRIVATE_KEY,
            "public_key": PUBLIC_KEY,
            "user": self.keys_user,
        }

    def get_task_info(self, task_id):
        self.calls.append(("get_task_info", task_id))
        return {
            "task_id": task_id,
            "allocations": [{"allocation_id": "a.1", "is_ready": self.ready, "end_time": None}],
        }


class Opener:
    def __init__(self) -> None:
        self.opened = []
        self.sockets = []

    def __call__(self, client):
        def open_websocket(shell_id, timeout):
            self.opened.append((shell_id, timeout))
            ws = FakeWebSocket()
            self.sockets.append(ws)
            return ws

        return open_websocket


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def opener():
    return Opener()


@pytest.fixture
def access(tmp_path, client, opener):
    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    manager = ShellAccess(ComputeService(client, profile), tmp_path / "access", opener)
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


def test_connect_writes_private_key_and_relays_to_the_shell(access, client, opener, tmp_path):
    result = access.connect(SHELL_ID)

    directory = tmp_path / "access"
    key_path = directory / SHELL_ID / "key"
    assert _mode(directory) == 0o700
    assert _mode(directory / SHELL_ID) == 0o700
    assert _mode(key_path) == 0o600
    assert key_path.read_text() == PRIVATE_KEY + "\n"
    # The private key never leaves the key file.
    assert "fixture-private-key-material" not in json.dumps(result)

    port = result["port"]
    assert result["host"] == "127.0.0.1" and isinstance(port, int) and port > 0
    assert result["kind"] == "shell" and result["id"] == SHELL_ID
    assert result["state"] == "STATE_RUNNING" and result["ready"] is True
    assert result["reused"] is False
    assert result["user"] == "root"
    assert result["key_path"] == str(key_path)
    assert result["host_key_type"] == "ssh-ed25519"
    assert result["host_key_fingerprint"] == FINGERPRINT
    assert result["probe"] == {"ok": True, "banner": "SSH-2.0-OpenSSH_9.6 fixture"}
    assert (directory / SHELL_ID / "known_hosts").read_text() == (
        f"[127.0.0.1]:{port} {PUBLIC_KEY}\n"
    )
    assert "StrictHostKeyChecking=yes" in result["ssh_command"]
    assert f"UserKnownHostsFile={result['known_hosts_path']}" in result["ssh_command"]
    assert result["ssh_command"].endswith("root@127.0.0.1")
    codes = {advisory["code"] for advisory in result["advisories"]}
    assert codes == {"tunnel_lifetime", "ssh_mcp_reload", "root_login"}

    banner, echoed = _exchange(port, b"client bytes")
    assert banner == BANNER and echoed == b"client bytes"
    # The probe and the connection each opened the shell's proxy, without a timeout for SSH.
    assert opener.opened == [(SHELL_ID, 15), (SHELL_ID, None)]


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
    assert result["ssh_mcp"]["config_path"] == str(path)
    assert result["ssh_mcp"]["profile"] == "det-shell-5b9c2f3e"
    assert _mode(path) == 0o600
    text = path.read_text()
    assert "fixture-private-key-material" not in text
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads(text)
    snippet = tomllib.loads(result["ssh_mcp"]["profile_toml"])
    assert config["defaults"] == {"defaultProfile": "det-shell-5b9c2f3e"}
    assert config["profiles"] == snippet["profiles"] == [{
        "name": "det-shell-5b9c2f3e",
        "host": "127.0.0.1",
        "port": result["port"],
        "user": "root",
        "auth": "key",
        "keyRef": result["key_path"],
        "trustedHostKey": FINGERPRINT,
        "group": "dev",
    }]


def test_profile_names_widen_when_prefixes_collide(access, tmp_path):
    access.connect(SHELL_ID)
    second = access.connect(OTHER_SHELL_ID)
    assert second["ssh_mcp"]["profile"] == f"det-shell-{OTHER_SHELL_ID}"
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads((tmp_path / "access" / SSH_MCP_CONFIG).read_text())
    assert config["defaults"]["defaultProfile"] == f"det-shell-{OTHER_SHELL_ID}"
    assert sorted(profile["name"] for profile in config["profiles"]) == [
        f"det-shell-{SHELL_ID}", f"det-shell-{OTHER_SHELL_ID}",
    ]


def test_connect_again_reuses_the_tunnel_and_a_new_port_replaces_it(access, client):
    first = access.connect(SHELL_ID)
    again = access.connect(SHELL_ID)
    assert again["reused"] is True and again["port"] == first["port"]
    assert [call for call in client.calls if call[0] == "get_shell_keys"] == [
        ("get_shell_keys", SHELL_ID)
    ]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    moved = access.connect(SHELL_ID, free_port)
    assert moved["reused"] is False and moved["port"] == free_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", first["port"]), timeout=1).close()


def test_disconnect_stops_the_tunnel_and_deletes_key_and_config(access, tmp_path):
    result = access.connect(SHELL_ID)
    banner, _ = _exchange(result["port"], b"x")
    assert access.disconnect(SHELL_ID) == {"kind": "shell", "id": SHELL_ID, "disconnected": True}
    assert not (tmp_path / "access" / SHELL_ID).exists()
    assert not (tmp_path / "access" / SSH_MCP_CONFIG).exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", result["port"]), timeout=1).close()
    assert access.disconnect(SHELL_ID)["disconnected"] is False


def test_disconnect_drops_open_connections(access, opener):
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


def test_a_failed_probe_is_reported_and_keeps_the_tunnel(tmp_path, client):
    def failing(_client):
        def open_websocket(shell_id, timeout):
            raise ConnectionError("Handshake status 502 Bad Gateway")
        return open_websocket

    profile = ComputeProfile.from_dict({
        "mounts": [{"host_path": "/shared", "container_path": "/shared"}],
        "defaults": {"image": "image", "pool": "pool"},
    })
    manager = ShellAccess(ComputeService(client, profile), tmp_path / "access", failing)
    try:
        result = manager.connect(SHELL_ID)
        assert result["probe"] == {
            "ok": False, "error": "ConnectionError: Handshake status 502 Bad Gateway",
        }
        assert (tmp_path / "access" / SHELL_ID / "key").exists()
    finally:
        manager.close_all()


def test_an_agent_user_drops_the_root_advisory(access, client):
    client.keys_user = "alice"
    result = access.connect(SHELL_ID)
    assert result["user"] == "alice"
    assert "root_login" not in {advisory["code"] for advisory in result["advisories"]}


@pytest.mark.parametrize("port", [True, 0, 80, 65536, "2222"])
def test_local_port_is_validated(access, client, port):
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID, port)
    assert caught.value.code == "invalid_request"
    assert client.calls == []


def test_a_busy_port_is_reported(access):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(APIError) as caught:
            access.connect(SHELL_ID, busy.getsockname()[1])
    assert caught.value.code == "port_unavailable"


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _record(directory, shell_id, pid, port=40000):
    (directory / shell_id).mkdir(parents=True)
    (directory / shell_id / "key").write_text("stale")
    (directory / shell_id / "tunnel.json").write_text(json.dumps({
        "pid": pid, "shell_id": shell_id, "port": port, "user": "root",
        "key_path": str(directory / shell_id / "key"), "host_key_type": "ssh-ed25519",
        "host_key_fingerprint": FINGERPRINT, "connected_at": "2026-10-09T00:00:00+00:00",
        "known_hosts_path": str(directory / shell_id / "known_hosts"),
    }))


@pytest.mark.skipif(os.name == "nt", reason="process liveness is not checked on Windows")
def test_sweep_removes_what_ended_processes_left_and_keeps_live_ones(access, tmp_path):
    directory = tmp_path / "access"
    _record(directory, SHELL_ID, _dead_pid())
    _record(directory, OTHER_SHELL_ID, os.getppid(), port=40001)
    access.sweep()
    assert not (directory / SHELL_ID).exists()
    assert (directory / OTHER_SHELL_ID / "key").exists()
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads((directory / SSH_MCP_CONFIG).read_text())
    assert [profile["port"] for profile in config["profiles"]] == [40001]


@pytest.mark.skipif(os.name == "nt", reason="process liveness is not checked on Windows")
def test_a_tunnel_held_by_another_live_process_is_not_taken_over(access, client, tmp_path):
    directory = tmp_path / "access"
    _record(directory, SHELL_ID, os.getppid())
    with pytest.raises(APIError) as caught:
        access.connect(SHELL_ID)
    assert caught.value.code == "shell_access_conflict"
    assert (directory / SHELL_ID / "key").read_text() == "stale"


def test_sweep_leaves_a_missing_directory_alone(tmp_path, client):
    manager = ShellAccess(object(), tmp_path / "absent")
    manager.sweep()
    assert not (tmp_path / "absent").exists()


def test_an_open_directory_is_made_private(access, tmp_path):
    directory = tmp_path / "access"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    access.connect(SHELL_ID)
    assert _mode(directory) == 0o700


def test_relay_ends_on_close_frame_and_forwards_client_eof():
    left, right = socket.socketpair()
    ws = FakeWebSocket(banner=b"")
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


def test_websocket_opener_targets_the_shell_proxy_with_the_clients_auth(monkeypatch):
    websocket = pytest.importorskip("websocket")
    calls = []

    class Connection:
        def settimeout(self, timeout):
            calls.append(("settimeout", timeout))

    def create_connection(url, **options):
        calls.append((url, options))
        return Connection()

    monkeypatch.setattr(websocket, "create_connection", create_connection)
    client = FakeClient()
    client.api_url = "http://det.example.test:8080/base/"
    websocket_opener(client)(SHELL_ID, None)
    url, options = calls[0]
    assert url == f"ws://det.example.test:8080/base/proxy/{SHELL_ID}/"
    assert options["header"] == {"Authorization": "Bearer fixture-token"}
    assert options["sslopt"] == {}
    assert options["timeout"] == 30 and options["enable_multithread"] is True
    assert calls[1] == ("settimeout", None)

    calls.clear()
    client.api_url = "https://det.example.test"
    websocket_opener(client)(SHELL_ID, 15)
    url, options = calls[0]
    assert url == f"wss://det.example.test/proxy/{SHELL_ID}/"
    assert options["sslopt"] == {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    assert options["timeout"] == 15

    calls.clear()
    client.verify_ssl = True
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/org-ca.pem")
    websocket_opener(client)(SHELL_ID, 15)
    assert calls[0][1]["sslopt"] == {"ca_certs": "/etc/org-ca.pem"}


def test_missing_websocket_client_is_reported_as_unsupported(monkeypatch, access, client):
    monkeypatch.setitem(sys.modules, "websocket", None)
    with pytest.raises(APIError) as caught:
        websocket_opener(client)
    assert caught.value.code == "unsupported"
    assert "determined-compute[mcp]" in str(caught.value)
