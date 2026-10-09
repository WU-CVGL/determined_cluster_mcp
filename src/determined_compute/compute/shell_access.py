"""Local SSH access to running Determined shells through the master's proxy.

A Determined shell runs sshd behind the master. ``det shell open`` reaches it with
``ssh -o ProxyCommand="python -m determined.cli.tunnel <master> %h"``, which carries the TCP
stream over a WebSocket to ``<master>/proxy/<shell id>/``. An SSH client that cannot run a
ProxyCommand, such as an SSH MCP server built on a native SSH library, needs a TCP port
instead. ShellAccess listens on 127.0.0.1 for each connected shell, relays every
connection over that WebSocket, and writes the shell's private key, a known_hosts entry,
and an ssh-mcp profile into a private directory. The relay runs in this process and stops
with it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import socket
import socketserver
import ssl
import sys
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from .models import APIError, ConflictError, ValidationError

LOOPBACK = "127.0.0.1"
SSH_MCP_CONFIG = "ssh-mcp.toml"
_RECORD = "tunnel.json"
_LOCK = ".lock"
# The only files a shell's subdirectory holds; the sweep deletes nothing else.
_SHELL_FILES = frozenset({"key", "known_hosts", _RECORD})
# How long a WebSocket may take to open, and the probe to read the sshd banner.
_CONNECT_TIMEOUT = 30
_PROBE_TIMEOUT = 15
# After the SSH client closes, how long to wait for the proxy to answer the close frame.
_CLOSE_TIMEOUT = 10
_OPCODE_CLOSE = 0x8
# ssh-mcp's policy tier for the generated profiles; without one it guesses the tier from
# the profile name and falls back to its strictest.
_SSH_MCP_GROUP = "dev"

OpenWebSocket = Callable[[str, Optional[float]], Any]


def host_key_fingerprint(public_key: str) -> Tuple[str, str]:
    """Return the key type and OpenSSH SHA256 fingerprint of an authorized_keys line."""
    parts = public_key.split()
    if len(parts) < 2:
        raise ValueError("public key is not an authorized_keys line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("public key is not an authorized_keys line") from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return parts[0], f"SHA256:{digest}"


def toml_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string."""
    quoted = ['"']
    for character in value:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("value is not valid Unicode")
        if character in '"\\':
            quoted.append("\\" + character)
        elif code < 0x20 or code == 0x7F:
            quoted.append(f"\\u{code:04X}")
        else:
            quoted.append(character)
    quoted.append('"')
    return "".join(quoted)


def websocket_opener(client: Any) -> OpenWebSocket:
    """Return a function that opens a WebSocket to a shell's proxy on the client's master.

    It uses the client's master URL, bearer token, and TLS verification setting; with
    verification on, the CA bundle comes from REQUESTS_CA_BUNDLE or CURL_CA_BUNDLE as for
    Requests. websocket-client reads the same proxy variables as Requests.
    """
    try:
        import websocket
    except ImportError as exc:
        raise APIError(
            "shell access needs the websocket-client package; install determined-compute[mcp]",
            code="unsupported",
        ) from exc
    parts = urlsplit(client.api_url)
    secure = parts.scheme == "https"
    base = urlunsplit(("wss" if secure else "ws", parts.netloc, parts.path.rstrip("/"), "", ""))
    headers = dict(getattr(client, "headers", None) or {})
    sslopt: Dict[str, Any] = {}
    if secure and client.verify_ssl:
        import requests.certs

        bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
        sslopt["ca_certs"] = bundle or requests.certs.where()
    elif secure:
        sslopt.update(cert_reqs=ssl.CERT_NONE, check_hostname=False)

    def open_websocket(shell_id: str, timeout: Optional[float]) -> Any:
        connection = websocket.create_connection(
            f"{base}/proxy/{quote(shell_id, safe='')}/",
            header=headers,
            sslopt=sslopt,
            timeout=_CONNECT_TIMEOUT if timeout is None else timeout,
            enable_multithread=True,
            # A followed redirect would resend the bearer token to its target.
            redirect_limit=0,
        )
        connection.settimeout(timeout)
        return connection

    return open_websocket


def abort_websocket(ws: Any) -> None:
    """Close a WebSocket at once, waking a thread blocked in ``ws.recv_data()``.

    ``ws.shutdown()`` only closes the socket, which does not wake a blocked receive on
    Linux; shutting the socket down first does.
    """
    sock = getattr(ws, "sock", None)
    if sock is not None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with suppress(Exception):
        ws.shutdown()


def relay(connection: socket.socket, ws: Any) -> None:
    """Copy bytes both ways between a TCP connection and a WebSocket until either closes."""
    finished = threading.Event()

    def upstream() -> None:
        try:
            while True:
                data = connection.recv(65536)
                if not data:
                    break
                ws.send_binary(data)
        except Exception:
            abort_websocket(ws)
            return
        # The SSH client is done: ask the proxy to close, and wait a bounded time for it.
        try:
            ws.send_close()
        except Exception:
            abort_websocket(ws)
            return
        if not finished.wait(_CLOSE_TIMEOUT):
            abort_websocket(ws)

    sender = threading.Thread(target=upstream, name="shell-relay-upstream", daemon=True)
    sender.start()
    try:
        while True:
            opcode, data = ws.recv_data()
            if opcode == _OPCODE_CLOSE:
                break
            if data:
                connection.sendall(data)
    except Exception:
        pass
    finally:
        finished.set()
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        abort_websocket(ws)
        sender.join(timeout=_CLOSE_TIMEOUT)


class _RelayHandler(socketserver.BaseRequestHandler):
    server: "_RelayServer"

    def handle(self) -> None:
        ws = self.server.open_websocket()
        with self.server.tracking(self.request, ws) as admitted:
            if admitted:
                relay(self.request, ws)


class _RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, open_websocket: Callable[[], Any]) -> None:
        self.open_websocket = open_websocket
        self._active: Dict[int, Tuple[socket.socket, Any]] = {}
        self._active_lock = threading.Lock()
        self._closing = False
        super().__init__((LOOPBACK, port), _RelayHandler)

    @contextmanager
    def tracking(self, connection: socket.socket, ws: Any) -> Iterator[bool]:
        """Register a relayed connection; yield False when the tunnel is already closing."""
        with self._active_lock:
            admitted = not self._closing
            if admitted:
                self._active[id(connection)] = (connection, ws)
        if not admitted:
            # The WebSocket opened while the tunnel stopped; drop_connections missed it.
            abort_websocket(ws)
        try:
            yield admitted
        finally:
            with self._active_lock:
                self._active.pop(id(connection), None)

    def drop_connections(self) -> None:
        with self._active_lock:
            self._closing = True
            active = list(self._active.values())
        for connection, ws in active:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            abort_websocket(ws)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # stdout carries MCP frames; one line on stderr, without a traceback.
        error = sys.exc_info()[1]
        print(
            f"determined-compute-mcp: shell tunnel connection failed: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )


def _held_elsewhere(record: Dict[str, Any]) -> bool:
    """Whether a record belongs to another host, whose processes cannot be checked here."""
    return record.get("host") not in (None, socket.gethostname())


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) sends CTRL_C_EVENT on Windows; assume another process is alive.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _write_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class _Tunnel:
    def __init__(
        self, shell_id: str, directory: Path, server: _RelayServer, record: Dict[str, Any]
    ) -> None:
        self.shell_id, self.directory, self.server, self.record = shell_id, directory, server, record
        self.thread = threading.Thread(
            target=server.serve_forever, name=f"shell-tunnel-{shell_id[:8]}", daemon=True
        )

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.drop_connections()
        shutil.rmtree(self.directory, ignore_errors=True)


class ShellAccess:
    """Open and close local SSH tunnels to the account's running shells."""

    def __init__(
        self,
        service: Any,
        directory: Path,
        opener: Optional[Callable[[Any], OpenWebSocket]] = None,
    ) -> None:
        self.service = service
        self.directory = Path(directory).expanduser()
        self._opener = opener or websocket_opener
        self._tunnels: Dict[str, _Tunnel] = {}
        self._lock = threading.Lock()

    # Directory and records.

    def _prepare_directory(self) -> Path:
        directory = self.directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = os.lstat(directory)
        if not os.path.isdir(directory) or os.path.islink(directory):
            raise ValidationError(f"shell access directory {directory} is not a directory")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise ValidationError(f"shell access directory {directory} belongs to another user")
        if os.name != "nt" and status.st_mode & 0o077:
            # Never loosen or tighten a directory the user named; ask for a private one.
            raise ValidationError(
                f"shell access directory {directory} is accessible to other users; use a "
                "dedicated directory with mode 0700"
            )
        return directory

    @contextmanager
    def _directory_lock(self) -> Iterator[None]:
        """Serialize directory changes with other determined-compute-mcp processes."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            yield
            return
        descriptor = os.open(self.directory / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _is_shell_directory(entry: Path) -> bool:
        """Whether ``entry`` is a shell's subdirectory: named by its UUID, holding only our files."""
        try:
            if entry.is_symlink() or not entry.is_dir() or str(uuid.UUID(entry.name)) != entry.name:
                return False
            return {child.name for child in entry.iterdir()} <= _SHELL_FILES
        except (ValueError, OSError):
            return False

    def _records(self) -> List[Dict[str, Any]]:
        """Records of the tunnels that live processes on this host hold, this one included."""
        records = []
        for entry in sorted(self.directory.iterdir()):
            path = entry / _RECORD
            if not self._is_shell_directory(entry) or not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("shell_id") != entry.name:
                continue
            # Another host's 127.0.0.1 ports are unreachable from here.
            if _held_elsewhere(record) or not _pid_alive(record.get("pid")):
                continue
            if record["pid"] == os.getpid() and record.get("shell_id") not in self._tunnels:
                continue
            records.append(record)
        return records

    def sweep(self) -> None:
        """Delete the key directories that ended processes left behind."""
        if not self.directory.is_dir():
            return
        self._prepare_directory()
        with self._directory_lock():
            for entry in self.directory.iterdir():
                if not self._is_shell_directory(entry):
                    continue
                try:
                    record = json.loads((entry / _RECORD).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    record = None
                if not isinstance(record, dict):
                    record = {}
                if _held_elsewhere(record):
                    continue
                pid = record.get("pid")
                ours = pid == os.getpid() and entry.name in self._tunnels
                if not ours and (pid == os.getpid() or not _pid_alive(pid)):
                    shutil.rmtree(entry, ignore_errors=True)
            self._write_ssh_mcp_config()

    @staticmethod
    def _profile_names(records: List[Dict[str, Any]]) -> Dict[str, str]:
        prefixes: Dict[str, int] = {}
        for record in records:
            prefix = record["shell_id"][:8]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        return {
            record["shell_id"]: "det-shell-" + (
                record["shell_id"][:8]
                if prefixes[record["shell_id"][:8]] == 1
                else record["shell_id"]
            )
            for record in records
        }

    @staticmethod
    def profile_toml(name: str, record: Dict[str, Any]) -> str:
        """One ssh-mcp ``[[profiles]]`` entry for a tunnel record."""
        lines = [
            "[[profiles]]",
            f"name = {toml_string(name)}",
            f"host = {toml_string(LOOPBACK)}",
            f"port = {int(record['port'])}",
            f"user = {toml_string(record['user'])}",
            'auth = "key"',
            f"keyRef = {toml_string(record['key_path'])}",
            f"trustedHostKey = {toml_string(record['host_key_fingerprint'])}",
            f"group = {toml_string(_SSH_MCP_GROUP)}",
        ]
        return "\n".join(lines) + "\n"

    def _write_ssh_mcp_config(self) -> Path:
        """Rewrite the ssh-mcp config for every live tunnel; remove it when there is none."""
        path = self.directory / SSH_MCP_CONFIG
        records = self._records()
        if not records:
            with suppress(FileNotFoundError):
                path.unlink()
            return path
        names = self._profile_names(records)
        newest = max(records, key=lambda record: str(record.get("connected_at", "")))
        parts = [
            "# Written by determined-compute-mcp for its open shell tunnels and rewritten on\n"
            "# every compute_shell_connect and compute_shell_disconnect; edits are lost.\n",
            f"[defaults]\ndefaultProfile = {toml_string(names[newest['shell_id']])}\n",
        ]
        parts.extend(
            self.profile_toml(names[record["shell_id"]], record)
            for record in sorted(records, key=lambda record: record["shell_id"])
        )
        temporary = self.directory / f".{SSH_MCP_CONFIG}.{os.getpid()}.tmp"
        with suppress(FileNotFoundError):
            temporary.unlink()
        _write_private(temporary, "\n".join(parts))
        os.replace(temporary, path)
        return path

    # Tool operations.

    def connect(self, shell_id: Any, local_port: Any = None) -> Dict[str, Any]:
        """Open, or return, the local tunnel to one of the account's running shells."""
        if local_port is not None and (
            isinstance(local_port, bool)
            or not isinstance(local_port, int)
            or not 1024 <= local_port <= 65535
        ):
            raise ValidationError("local_port must be an integer from 1024 to 65535")
        kind, remote_id, entity = self.service._owned("shell", shell_id)
        state = entity.get("state")
        if state != "STATE_RUNNING":
            raise ConflictError(
                f"shell {remote_id} is {state or 'in an unknown state'}; only a running shell "
                "can be connected (check compute_status)",
                code="shell_not_running",
            )
        client = self.service.client
        open_websocket = self._opener(client)
        with self._lock:
            tunnel = self._tunnels.get(remote_id)
            reused = tunnel is not None and local_port in (None, tunnel.record["port"])
            if tunnel is not None and not reused:
                self._disconnect(remote_id)
            if not reused:
                tunnel = self._open(remote_id, client, open_websocket, local_port)
            record = dict(tunnel.record)
        ready, unavailable = self._ready(client, remote_id)
        result = self._result(record, state, ready, reused)
        if unavailable:
            result["context_unavailable"] = unavailable
        result["probe"] = (
            self._probe(open_websocket, remote_id)
            if ready is not False
            else {"ok": False, "error": "the shell is not ready yet; sshd has not started"}
        )
        return result

    def _open(
        self, remote_id: str, client: Any, open_websocket: OpenWebSocket, local_port: Optional[int]
    ) -> _Tunnel:
        keys = client.get_shell_keys(remote_id)
        if keys.get("id") != remote_id:
            raise APIError("Remote shell identity does not match the request", code="invalid_response")
        try:
            key_type, fingerprint = host_key_fingerprint(keys["public_key"])
        except ValueError as exc:
            raise APIError("Determined returned a malformed shell public key", code="invalid_response") from exc
        root = self._prepare_directory()
        with self._directory_lock():
            directory = root / remote_id
            try:
                held = json.loads((directory / _RECORD).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = None
            if isinstance(held, dict) and (
                _held_elsewhere(held)
                or (held.get("pid") != os.getpid() and _pid_alive(held.get("pid")))
            ):
                where = f"on host {held.get('host')} " if _held_elsewhere(held) else ""
                raise ConflictError(
                    f"another determined-compute-mcp process {where}(pid {held.get('pid')}) "
                    f"holds this shell's tunnel on {LOOPBACK}:{held.get('port')}; use or "
                    "disconnect it there",
                    code="shell_access_conflict",
                )
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(mode=0o700)
            key_path = directory / "key"
            private_key = keys["private_key"]
            _write_private(key_path, private_key if private_key.endswith("\n") else private_key + "\n")
            try:
                server = _RelayServer(
                    local_port or 0, lambda: open_websocket(remote_id, None)
                )
            except OSError as exc:
                shutil.rmtree(directory, ignore_errors=True)
                raise ConflictError(
                    f"cannot listen on {LOOPBACK}:{local_port or 0}: {exc.strerror or exc}",
                    code="port_unavailable",
                ) from exc
            port = server.server_address[1]
            known_hosts = directory / "known_hosts"
            _write_private(known_hosts, f"[{LOOPBACK}]:{port} {keys['public_key']}\n")
            record = {
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "shell_id": remote_id,
                "port": port,
                "user": keys["user"],
                "key_path": str(key_path),
                "known_hosts_path": str(known_hosts),
                "host_key_type": key_type,
                "host_key_fingerprint": fingerprint,
                "connected_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_private(directory / _RECORD, json.dumps(record, indent=2) + "\n")
            tunnel = _Tunnel(remote_id, directory, server, record)
            tunnel.thread.start()
            self._tunnels[remote_id] = tunnel
            self._write_ssh_mcp_config()
        return tunnel

    def _ready(self, client: Any, remote_id: str) -> Tuple[Optional[bool], List[str]]:
        """Whether sshd is up, as det shell open decides it: the allocation is ready."""
        try:
            info = client.get_task_info(remote_id)
        except APIError:
            return None, ["ready"]
        return any(
            allocation.get("is_ready") is True and not allocation.get("end_time")
            for allocation in info.get("allocations", [])
        ), []

    @staticmethod
    def _probe(open_websocket: OpenWebSocket, remote_id: str) -> Dict[str, Any]:
        """Open the shell's proxy once and read the sshd identification line."""
        ws = None
        try:
            ws = open_websocket(remote_id, _PROBE_TIMEOUT)
            opcode, data = ws.recv_data()
            # A line that is no SSH identification makes sshd close its side now, rather than
            # hold an unauthenticated connection for its LoginGraceTime.
            with suppress(Exception):
                ws.send_binary(b"probe\r\n")
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:512]}
        finally:
            if ws is not None:
                abort_websocket(ws)
        line = b"" if opcode == _OPCODE_CLOSE else bytes(data or b"").split(b"\n", 1)[0].rstrip(b"\r")
        if not line.startswith(b"SSH-"):
            return {"ok": False, "error": "the shell's proxy did not answer with an SSH banner"}
        return {"ok": True, "banner": line[:255].decode("ascii", "replace")}

    def _result(
        self, record: Dict[str, Any], state: Any, ready: Optional[bool], reused: bool
    ) -> Dict[str, Any]:
        names = self._profile_names(self._records() or [record])
        name = names.get(record["shell_id"], f"det-shell-{record['shell_id'][:8]}")
        user, port = record["user"], record["port"]
        ssh_command = shlex.join([
            "ssh", "-p", str(port),
            "-i", record["key_path"],
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={record['known_hosts_path']}",
            f"{user}@{LOOPBACK}",
        ])
        advisories = [
            {
                "code": "tunnel_lifetime",
                "message": (
                    "The tunnel runs inside this determined-compute-mcp process. It stops when "
                    "the process exits, on compute_shell_disconnect, and on compute_cancel of "
                    "the shell; the shell itself keeps running until it is cancelled."
                ),
            },
            {
                "code": "ssh_mcp_reload",
                "message": (
                    "ssh-mcp reads its config only when it starts: restart or reconnect it "
                    "after each connect or disconnect so that it sees the current profiles."
                ),
            },
        ]
        return {
            "kind": "shell",
            "id": record["shell_id"],
            "state": state,
            "ready": ready,
            "reused": reused,
            "host": LOOPBACK,
            "port": port,
            "user": user,
            "key_path": record["key_path"],
            "known_hosts_path": record["known_hosts_path"],
            "host_key_type": record["host_key_type"],
            "host_key_fingerprint": record["host_key_fingerprint"],
            "ssh_command": ssh_command,
            "ssh_mcp": {
                "config_path": str(self.directory / SSH_MCP_CONFIG),
                "profile": name,
                "profile_toml": self.profile_toml(name, record),
            },
            "advisories": advisories,
        }

    def disconnect(self, shell_id: Any) -> Dict[str, Any]:
        """Stop a shell's tunnel in this process and delete its key; the shell keeps running."""
        remote_id = self.service._canonical_id("shell", shell_id)
        with self._lock:
            closed = self._disconnect(remote_id)
        return {"kind": "shell", "id": remote_id, "disconnected": closed}

    def _disconnect(self, remote_id: str) -> bool:
        tunnel = self._tunnels.pop(remote_id, None)
        if tunnel is None:
            return False
        tunnel.stop()
        if self.directory.is_dir():
            with self._directory_lock():
                self._write_ssh_mcp_config()
        return True

    def close_all(self) -> None:
        """Stop every tunnel of this process; for process exit."""
        with self._lock:
            for remote_id in list(self._tunnels):
                with suppress(Exception):
                    self._disconnect(remote_id)


__all__ = ["ShellAccess", "host_key_fingerprint", "relay", "toml_string", "websocket_opener"]
