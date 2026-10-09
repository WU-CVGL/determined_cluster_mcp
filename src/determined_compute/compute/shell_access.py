"""Local SSH access to running Determined shells through the master's proxy.

A Determined shell runs sshd behind the master. ``det shell open`` reaches it with
``ssh -o ProxyCommand="python -m determined.cli.tunnel <master> %h"``, which carries the TCP
stream over a WebSocket to ``<master>/proxy/<shell id>/``. An SSH client that cannot run a
ProxyCommand, such as an SSH MCP server built on a native SSH library, needs a TCP port
instead. ShellAccess listens on 127.0.0.1 for each connected shell, relays every
connection over that WebSocket, and writes the shell's private key, a known_hosts entry,
and an ssh-mcp profile into a private directory that this process alone uses. The relay
runs in this process and stops with it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import socket
import socketserver
import ssl
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .models import APIError, ConflictError, ValidationError

LOOPBACK = "127.0.0.1"
SSH_MCP_CONFIG = "ssh-mcp.toml"
_LOCK = ".lock"
# The only files a shell's subdirectory holds; nothing else is ever deleted.
_SHELL_FILES = frozenset({"key", "known_hosts"})
# How long a WebSocket may take to open, and the probe to read the sshd banner.
_CONNECT_TIMEOUT = 30
_PROBE_TIMEOUT = 15
_PROBE_MAX_BYTES = 4096
# After the SSH client closes, how long to wait for the proxy to answer the close frame.
_CLOSE_TIMEOUT = 10
_OPCODE_CLOSE = 0x8
# ssh-mcp's policy tier for the generated profiles; without one it guesses the tier from
# the profile name and falls back to its strictest.
_SSH_MCP_GROUP = "dev"
# websocket-client reads the proxy environment unless it is given a no-proxy list; one that
# matches no host makes it use exactly the proxy it is given.
_NO_ENVIRONMENT_PROXY = ["\x00"]

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


def ssh_option_path(path: str) -> str:
    """Quote a path for an OpenSSH ``-o`` option, which OpenSSH parses again.

    OpenSSH splits an option's value at spaces unless it is double-quoted, reads ``\\``
    escapes inside the quotes, and expands ``%`` tokens in file paths.
    """
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def transport_error(error: BaseException) -> str:
    """Describe a WebSocket failure without the response headers or body it may carry.

    A refused handshake keeps only its HTTP status; the master's or a proxy's answer can set
    cookies or echo request data.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"the WebSocket handshake was refused with HTTP {status}"
    name = type(error).__name__
    if isinstance(error, ssl.SSLCertVerificationError):
        return f"{name}: {error.verify_message}"
    if isinstance(error, ssl.SSLError):
        return f"{name}: {error.reason}" if error.reason else name
    if isinstance(error, OSError) and error.strerror:
        return f"{name}: {error.strerror}"
    if isinstance(error, socket.timeout):
        return f"{name}: timed out"
    if name == "WebSocketProxyException" and str(error).startswith("failed CONNECT via proxy"):
        return f"{name}: {error}"
    return name


def _ssl_context(verify: bool) -> ssl.SSLContext:
    """TLS settings as Requests applies them: its CA bundle variables, or no verification."""
    if not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    import requests.certs

    bundle = (
        os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or requests.certs.where()
    )
    if os.path.isdir(bundle):
        return ssl.create_default_context(capath=bundle)
    return ssl.create_default_context(cafile=bundle)


def _proxy_for(url: str) -> Optional[str]:
    """The proxy Requests would use for ``url``, from the same environment variables."""
    import requests.utils

    return requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url))


# What websocket-client sets on the sockets it opens itself: no Nagle delay for keystrokes,
# and keepalives so that a middlebox does not drop an idle session.
_SOCKET_OPTIONS = tuple(
    (level, getattr(socket, name), value)
    for level, name, value in (
        (socket.IPPROTO_TCP, "TCP_NODELAY", 1),
        (socket.SOL_SOCKET, "SO_KEEPALIVE", 1),
        (socket.IPPROTO_TCP, "TCP_KEEPIDLE", 30),
        (socket.IPPROTO_TCP, "TCP_KEEPINTVL", 10),
        (socket.IPPROTO_TCP, "TCP_KEEPCNT", 3),
    )
    if hasattr(socket, name)
)


# A direct connection opens its own socket: websocket-client ignores http_no_proxy unless it
# is also given a proxy, so it would otherwise apply its own reading of the proxy
# environment where Requests decided to connect directly.
def _direct_socket(
    host: str, port: int, context: Optional[ssl.SSLContext], timeout: float
) -> socket.socket:
    connection = socket.create_connection((host, port), timeout=timeout)
    try:
        for option in _SOCKET_OPTIONS:
            connection.setsockopt(*option)
        if context is None:
            return connection
        return context.wrap_socket(connection, server_hostname=host)
    except BaseException:
        connection.close()
        raise


def websocket_opener(client: Any) -> OpenWebSocket:
    """Return a function that opens a WebSocket to a shell's proxy on the client's master.

    It uses the client's master URL, bearer token, and TLS verification, and reaches the
    master as Requests does: the proxy that Requests picks from the environment, or a direct
    connection. With verification on, the CA bundle is REQUESTS_CA_BUNDLE, else
    CURL_CA_BUNDLE (a file or a directory), else Requests' bundle. Only an http:// proxy is
    supported, through HTTP CONNECT.
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
    if not parts.hostname:
        raise ValidationError("the master URL has no host")
    host, port = parts.hostname, parts.port or (443 if secure else 80)
    base = urlunsplit(("wss" if secure else "ws", parts.netloc, parts.path.rstrip("/"), "", ""))
    headers = dict(getattr(client, "headers", None) or {})
    context = _ssl_context(bool(client.verify_ssl)) if secure else None
    proxy = _proxy_for(client.api_url)
    proxy_options: Dict[str, Any] = {}
    if proxy:
        proxy_parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        if proxy_parts.scheme != "http" or not proxy_parts.hostname:
            raise APIError(
                f"shell access reaches the master only through an http:// proxy; the "
                f"environment selects a {proxy_parts.scheme}:// proxy for it",
                code="unsupported",
            )
        proxy_options = {
            "http_proxy_host": proxy_parts.hostname,
            "http_proxy_port": proxy_parts.port or 80,
            "http_proxy_auth": (
                (unquote(proxy_parts.username), unquote(proxy_parts.password or ""))
                if proxy_parts.username
                else None
            ),
            "http_no_proxy": _NO_ENVIRONMENT_PROXY,
            "proxy_type": "http",
        }

    def open_websocket(shell_id: str, timeout: Optional[float]) -> Any:
        connect_timeout = _CONNECT_TIMEOUT if timeout is None else timeout
        options: Dict[str, Any] = {
            "header": headers,
            "timeout": connect_timeout,
            "enable_multithread": True,
            # A followed redirect would resend the bearer token to its target.
            "redirect_limit": 0,
        }
        if proxy_options:
            options.update(proxy_options)
            if context is not None:
                options["sslopt"] = {"context": context}
        else:
            # websocket-client would otherwise apply its own reading of the environment.
            options["socket"] = _direct_socket(host, port, context, connect_timeout)
        try:
            connection = websocket.create_connection(
                f"{base}/proxy/{quote(shell_id, safe='')}/", **options
            )
        except BaseException:
            if "socket" in options:
                options["socket"].close()
            raise
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
        # stdout carries MCP frames; one line on stderr, without a traceback or the
        # response a refused handshake carries.
        error = sys.exc_info()[1]
        description = transport_error(error) if error is not None else "unknown error"
        print(
            f"determined-compute-mcp: shell tunnel connection failed: {description}",
            file=sys.stderr,
        )


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
        # The identity of the key and known_hosts files as written.
        self.files = {
            name: self._identity(directory / name) for name in sorted(_SHELL_FILES)
        }

    @staticmethod
    def _identity(path: Path) -> Optional[Tuple[int, int]]:
        try:
            status = os.lstat(path)
        except OSError:
            return None
        return (status.st_dev, status.st_ino) if stat.S_ISREG(status.st_mode) else None

    def files_unchanged(self) -> bool:
        """Whether the shell's files are still the ones this tunnel wrote."""
        return all(
            identity is not None and self._identity(self.directory / name) == identity
            for name, identity in self.files.items()
        )

    def stop(self) -> None:
        """Close the listener and its connections; the files are ShellAccess's to remove."""
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.server.drop_connections()


def _remove_shell_directory(directory: Path) -> None:
    """Delete a shell's subdirectory without recursing: anything unexpected makes it fail."""
    for name in _SHELL_FILES:
        with suppress(FileNotFoundError):
            os.unlink(directory / name)
    os.rmdir(directory)


def profile_name(shell_id: str) -> str:
    """The ssh-mcp profile name of a shell's tunnel."""
    return f"det-shell-{shell_id}"


class ShellAccess:
    """Open and close local SSH tunnels to the account's running shells.

    One process uses a directory: the first connect takes a lock on it for the life of the
    process, and another process that finds the lock taken fails instead of sharing it.
    """

    def __init__(
        self,
        service: Any,
        directory: Path,
        opener: Optional[Callable[[Any], OpenWebSocket]] = None,
    ) -> None:
        self.service = service
        # Absolute, so the key and config paths in results work from any directory;
        # not resolved, so a directory given as a symbolic link is still refused.
        self.directory = Path(directory).expanduser().absolute()
        self._opener = opener or websocket_opener
        self._tunnels: Dict[str, _Tunnel] = {}
        self._lock = threading.Lock()
        self._claim: Optional[int] = None

    # The directory.

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

    def _try_lock(self) -> Optional[int]:
        """Take the directory's lock without waiting; None when another process holds it."""
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.directory / _LOCK, flags, 0o600)
        try:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return None
        return descriptor

    def _claim_is_current(self) -> bool:
        """Whether the held lock is still the directory's lock file, not a deleted one."""
        try:
            held = os.fstat(self._claim)
            current = os.lstat(self.directory / _LOCK)
        except (OSError, TypeError):
            return False
        return held.st_nlink > 0 and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)

    def _owns_directory(self) -> bool:
        """Whether this process holds the lock on the directory's current lock file.

        When only the lock file was removed and no other server locked its replacement, this
        locks it again, so it changes the claim: call it with ``self._lock`` held.
        """
        if self._claim is None:
            return False
        if self._claim_is_current():
            return True
        # Only the very files this process wrote prove that no other server took the
        # directory in between: one that did may have written its own under the same names.
        if not all(
            self._is_shell_directory(tunnel.directory) and tunnel.files_unchanged()
            for tunnel in self._tunnels.values()
        ):
            return False
        try:
            self._prepare_directory()
            descriptor = self._try_lock()
        except (OSError, ValidationError):
            descriptor = None
        if descriptor is None:
            return False
        os.close(self._claim)
        self._claim = descriptor
        return True

    def _claim_directory(self) -> None:
        """Take the directory for this process, removing what an ended one left behind."""
        if self._claim is not None:
            if self._claim_is_current():
                return
            # The directory or its lock file was removed under this process: claim it again,
            # so that no second server can share it.
            self._release_directory()
        self._prepare_directory()
        descriptor = self._try_lock()
        if descriptor is None:
            raise ConflictError(
                f"another determined-compute-mcp process uses the shell access directory "
                f"{self.directory}; give each MCP server its own --shell-access-dir",
                code="shell_access_conflict",
            )
        self._claim = descriptor
        self._clean()

    def _release_directory(self) -> None:
        if self._claim is not None:
            os.close(self._claim)
            self._claim = None

    @staticmethod
    def _is_shell_directory(entry: Path) -> bool:
        """Whether ``entry`` is a shell's subdirectory: named by its UUID, holding only our files."""
        try:
            if entry.is_symlink() or not entry.is_dir() or str(uuid.UUID(entry.name)) != entry.name:
                return False
            return all(
                child.name in _SHELL_FILES and stat.S_ISREG(child.lstat().st_mode)
                for child in entry.iterdir()
            )
        except (ValueError, OSError):
            return False

    def _clean(self) -> None:
        """Delete shell subdirectories without a tunnel here; write or remove the ssh-mcp config."""
        for entry in self.directory.iterdir():
            if entry.name not in self._tunnels and self._is_shell_directory(entry):
                with suppress(OSError):
                    _remove_shell_directory(entry)
        self._write_ssh_mcp_config()

    def sweep(self) -> None:
        """Remove what an ended process left behind, unless a running one uses the directory."""
        if not self.directory.is_dir():
            return
        with self._lock:
            if self._claim is not None:
                return
            self._prepare_directory()
            descriptor = self._try_lock()
            if descriptor is None:
                return
            try:
                self._clean()
            finally:
                os.close(descriptor)

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

    def _write_ssh_mcp_config(self) -> None:
        """Rewrite the ssh-mcp config for this process's tunnels; remove it when there is none."""
        path = self.directory / SSH_MCP_CONFIG
        temporary = self.directory / f".{SSH_MCP_CONFIG}.tmp"
        with suppress(FileNotFoundError):
            temporary.unlink()
        if not self._tunnels:
            with suppress(FileNotFoundError):
                path.unlink()
            return
        parts = [
            "# Written by determined-compute-mcp for its open shell tunnels and rewritten on\n"
            "# every compute_shell_connect and compute_shell_disconnect; edits are lost.\n",
        ]
        parts.extend(
            self.profile_toml(profile_name(shell_id), self._tunnels[shell_id].record)
            for shell_id in sorted(self._tunnels)
        )
        try:
            _write_private(temporary, "\n".join(parts))
            os.replace(temporary, path)
        except BaseException:
            with suppress(OSError):
                temporary.unlink()
            raise

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
            if self._tunnels and not self._owns_directory():
                # Its files may now belong to another server; this one must not touch them.
                raise ConflictError(
                    f"the shell access directory {self.directory} was removed or taken over "
                    "while this server had open tunnels; disconnect them with "
                    "compute_shell_disconnect, or restart the server, before connecting again",
                    code="shell_access_conflict",
                )
            tunnel = self._tunnels.get(remote_id)
            reused = tunnel is not None
            if tunnel is not None and local_port not in (None, tunnel.record["port"]):
                raise ConflictError(
                    f"shell {remote_id} already has a tunnel on {LOOPBACK}:"
                    f"{tunnel.record['port']}; call compute_shell_disconnect first to use "
                    "another port",
                    code="shell_access_conflict",
                )
            if tunnel is None:
                self._claim_directory()
                tunnel = self._open(remote_id, client, open_websocket, local_port)
            else:
                # The config derives from the open tunnels; restore it if a failed write on
                # disconnect removed it.
                self._write_ssh_mcp_config()
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
        """Create a tunnel completely, or leave nothing of it behind."""
        keys = client.get_shell_keys(remote_id)
        if keys.get("id") != remote_id:
            raise APIError("Remote shell identity does not match the request", code="invalid_response")
        try:
            key_type, fingerprint = host_key_fingerprint(keys["public_key"])
        except ValueError as exc:
            raise APIError("Determined returned a malformed shell public key", code="invalid_response") from exc
        directory = self.directory / remote_id
        if os.path.lexists(directory):
            if not self._is_shell_directory(directory):
                raise ConflictError(
                    f"{directory} exists and is not a shell access directory; remove it or "
                    "use a dedicated --shell-access-dir",
                    code="shell_access_conflict",
                )
            # A shell subdirectory without a tunnel: this process holds the lock, so an
            # ended process left it behind.
            _remove_shell_directory(directory)
        directory.mkdir(mode=0o700)
        server: Optional[_RelayServer] = None
        try:
            key_path = directory / "key"
            private_key = keys["private_key"]
            _write_private(key_path, private_key if private_key.endswith("\n") else private_key + "\n")
            try:
                server = _RelayServer(local_port or 0, lambda: open_websocket(remote_id, None))
            except OSError as exc:
                raise ConflictError(
                    f"cannot listen on {LOOPBACK}:{local_port or 0}: {exc.strerror or exc}",
                    code="port_unavailable",
                ) from exc
            port = server.server_address[1]
            known_hosts = directory / "known_hosts"
            _write_private(known_hosts, f"[{LOOPBACK}]:{port} {keys['public_key']}\n")
            record = {
                "shell_id": remote_id,
                "port": port,
                "user": keys["user"],
                "key_path": str(key_path),
                "known_hosts_path": str(known_hosts),
                "host_key_type": key_type,
                "host_key_fingerprint": fingerprint,
            }
            tunnel = _Tunnel(remote_id, directory, server, record)
            self._tunnels[remote_id] = tunnel
            self._write_ssh_mcp_config()
            tunnel.thread.start()
        except BaseException:
            self._tunnels.pop(remote_id, None)
            if server is not None:
                # serve_forever has not started, so only the socket needs closing.
                server.server_close()
            with suppress(OSError):
                _remove_shell_directory(directory)
            with suppress(OSError):
                self._write_ssh_mcp_config()
            raise
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
        """Open the shell's proxy once and read sshd's identification line.

        A banner shows only that sshd answers; logging in is left to the SSH client.
        """
        deadline = time.monotonic() + _PROBE_TIMEOUT
        ws = None
        received = b""
        try:
            ws = open_websocket(remote_id, _PROBE_TIMEOUT)
            # The proxy may split the line across WebSocket messages.
            while b"\n" not in received and len(received) < _PROBE_MAX_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ws.settimeout(remaining)
                opcode, data = ws.recv_data()
                if opcode == _OPCODE_CLOSE:
                    break
                received += bytes(data or b"")
            # A line that is no SSH identification makes sshd close its side now, rather than
            # hold an unauthenticated connection for its LoginGraceTime.
            with suppress(Exception):
                ws.send_binary(b"probe\r\n")
        except Exception as exc:
            return {"ok": False, "error": transport_error(exc)}
        finally:
            if ws is not None:
                abort_websocket(ws)
        line = received.split(b"\n", 1)[0].rstrip(b"\r") if b"\n" in received else b""
        if not line.startswith(b"SSH-"):
            return {"ok": False, "error": "the shell's proxy did not answer with an SSH banner"}
        return {"ok": True, "banner": line[:255].decode("ascii", "replace")}

    def _result(
        self, record: Dict[str, Any], state: Any, ready: Optional[bool], reused: bool
    ) -> Dict[str, Any]:
        name = profile_name(record["shell_id"])
        user, port = record["user"], record["port"]
        ssh_command = shlex.join([
            "ssh", "-p", str(port),
            "-o", f"IdentityFile={ssh_option_path(record['key_path'])}",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={ssh_option_path(record['known_hosts_path'])}",
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
                    "ssh-mcp reads its config only when it starts: start or reconnect it "
                    "after a profile is added, removed, or changed, so that it sees the "
                    "current profiles."
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
        owner = self._owns_directory()
        tunnel.stop()
        if not owner:
            # Another server may have taken the directory over: leave its files alone.
            return True
        with suppress(OSError):
            _remove_shell_directory(tunnel.directory)
        try:
            self._write_ssh_mcp_config()
        except OSError:
            # The tunnel is gone; a config that still listed it would mislead ssh-mcp.
            with suppress(OSError):
                (self.directory / SSH_MCP_CONFIG).unlink()
        return True

    def close_all(self) -> None:
        """Stop every tunnel of this process and release the directory; for process exit."""
        with self._lock:
            for remote_id in list(self._tunnels):
                with suppress(Exception):
                    self._disconnect(remote_id)
            self._release_directory()


__all__ = [
    "ShellAccess",
    "abort_websocket",
    "host_key_fingerprint",
    "profile_name",
    "relay",
    "ssh_option_path",
    "toml_string",
    "transport_error",
    "websocket_opener",
]
