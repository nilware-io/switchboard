"""Machines that connect out to the hub (docs/SWITCHBOARD.md, "Outbound agents").

A machine runs `switchboard-connect`, which opens an SSH connection to the hub's relay account and
runs the dispatcher in serve mode on it. sshd starts the relay (`remote/relay.py`) as the forced
command of that machine's key; the relay connects to the Unix socket served here and names the
machine in a preamble line. The name is trusted because only the relay account may connect
(`SO_PEERCRED`) and it takes the name from the root-owned `authorized_keys`; everything after the
preamble comes from the machine and is untrusted, size-capped and parsed strictly.

`AgentHub` holds one live connection per machine (a newer one replaces the older), pings each one,
and offers `AgentTransport`, which implements `Transport.call` over that connection, so every
remote tool works unchanged. A control socket in `stateDir` answers `status` and `check <machine>`
for the CLI (`voice-coding-mcp status`, `remote-check`), which runs in another process.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import pwd
import re
import socket
import stat
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..clock import now_ms
from ..log import Logger
from .config import MACHINE_NAME_RE, MachineConfig, RemoteConfig
from .protocol import ARG_RE, AttachmentStream, ResponseError, parse_response, request_tokens, response_cap
from .transport import TransportError, TransportResult, dispatcher_error

PROTOCOL = 1
MAX_MACHINE_FRAME_BYTES = 5 * 1024 * 1024
"""Longest frame from a machine (also the relay's cap)."""
MAX_HUB_FRAME_BYTES = 2 * 1024 * 1024
"""Longest frame to a machine (the dispatcher's cap)."""
MAX_FRAME_ID = 2**53 - 1
MAX_IN_FLIGHT = 4
"""Requests in flight per machine; the dispatcher runs at most this many at once."""
PREAMBLE_SECONDS = 5.0
HELLO_SECONDS = 10.0
PONG_SECONDS = 10.0
WRITE_STALL_SECONDS = 5.0
"""Longest wait for the write lock and the drain of one frame. A machine that stops reading its
channel fills the socket, relay and SSH buffers; the hub then closes the connection
(`write_stalled`) instead of waiting forever. SSH keepalives cannot see this: they pass even when
the channel window is never adjusted."""
MAX_UNFINISHED_HANDSHAKES = 2
"""Connections per machine still waiting for their hello, so one machine key cannot hold every slot."""
FLAP_WINDOW_MS = 60_000
FLAP_LIMIT = 3
"""More replacements of one machine than this within FLAP_WINDOW_MS log a warning."""
HOUR_MS = 3_600_000
READ_ONLY_VERBS = ("version", "list", "capture", "usage", "attachments", "attachment", "health")
"""Retried once on the new connection when the one they went out on was replaced."""
SOCKET_DIR_MODE = 0o2750
CONTROL_SOCKET_NAME = "agents-control.sock"
MAX_CONTROL_BYTES = 64 * 1024
CONTROL_SECONDS = 30.0
MAX_UNIX_PATH = 107
FRAME_ERRORS = ("bad_frame", "frame_too_large", "duplicate_id", "busy", "crashed")
NOT_RUN_ERRORS = ("busy", "bad_frame", "duplicate_id")
"""Frame-level refusals that mean the dispatcher did not run the request."""
VERSION_RE = re.compile(r"[A-Za-z0-9._+-]{1,32}")


def _frame_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_FRAME_ID


def encode_frame(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _object(line: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Preamble:
    machine: str
    peer: str | None


def parse_preamble(line: bytes) -> Preamble | None:
    """The relay's first line: `{"t": "relay", "protocol": 1, "machine": <name>, "peer": <ip or null>}`."""
    data = _object(line)
    if data is None or set(data) != {"t", "protocol", "machine", "peer"}:
        return None
    if data["t"] != "relay" or type(data["protocol"]) is not int or data["protocol"] != PROTOCOL:
        return None
    machine, peer = data["machine"], data["peer"]
    if not isinstance(machine, str) or not MACHINE_NAME_RE.fullmatch(machine):
        return None
    if peer is not None and not isinstance(peer, str):
        return None
    shown: str | None = None
    if isinstance(peer, str):
        with contextlib.suppress(ValueError):
            shown = str(ipaddress.ip_address(peer))
    return Preamble(machine=machine, peer=shown)


@dataclass(frozen=True)
class Hello:
    version: str
    verbs: tuple[str, ...]
    allow_send: bool
    allow_usage: bool
    policy: str
    """`ok`, or `bad_policy` when the dispatcher's command line is broken (it refuses everything)."""
    allow_attachments: bool = False
    """Sent (as true) only by a dispatcher 0.7.0 or newer started with `--allow-attachments`."""


def parse_hello(line: bytes) -> Hello | None:
    data = _object(line)
    keys = {"t", "protocol", "version", "verbs", "allowSend", "allowUsage", "policy"}
    if data is None or not keys <= set(data) <= keys | {"allowAttachments"} or data["t"] != "hello":
        return None
    if data.get("allowAttachments", True) is not True:
        return None
    if type(data["protocol"]) is not int or data["protocol"] != PROTOCOL:
        return None
    version, verbs = data["version"], data["verbs"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        return None
    if not isinstance(verbs, list) or len(verbs) > 32:
        return None
    if not all(isinstance(verb, str) and ARG_RE.fullmatch(verb) for verb in verbs):
        return None
    if not isinstance(data["allowSend"], bool) or not isinstance(data["allowUsage"], bool):
        return None
    if data["policy"] not in ("ok", "bad_policy"):
        return None
    return Hello(
        version=version,
        verbs=tuple(verbs),
        allow_send=data["allowSend"],
        allow_usage=data["allowUsage"],
        policy=data["policy"],
        allow_attachments="allowAttachments" in data,
    )


def parse_machine_frame(line: bytes) -> dict[str, Any] | None:
    """A `res`, `pong` or `error` frame with exactly its schema, else None (the connection closes)."""
    data = _object(line)
    if data is None:
        return None
    kind = data.get("t")
    keys = set(data)
    if kind == "res":
        if not {"t", "id", "doc"} <= keys <= {"t", "id", "doc", "more"} or not _frame_id(data["id"]):
            return None
        if not isinstance(data["doc"], dict) or data.get("more", True) is not True:
            return None
        return data
    if kind == "pong":
        return data if keys == {"t", "id"} and _frame_id(data["id"]) else None
    if kind == "error":
        if keys != {"t", "id", "error"} or data["error"] not in FRAME_ERRORS:
            return None
        return data if data["id"] is None or _frame_id(data["id"]) else None
    return None


@dataclass(frozen=True)
class _Closed:
    """The connection closed after the request went out (`replaced`: by a newer connection)."""

    replaced: bool


@dataclass
class _Request:
    queue: asyncio.Queue[tuple[dict[str, Any], int] | None] = field(default_factory=asyncio.Queue)


class AgentConnection:
    """One accepted machine connection (after preamble and hello)."""

    def __init__(
        self,
        hub: AgentHub,
        *,
        machine: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        hello: Hello,
        peer: str | None,
        since_ms: int,
    ) -> None:
        self.hub = hub
        self.machine = machine
        self.reader = reader
        self.writer = writer
        self.hello = hello
        self.peer = peer
        self.since_ms = since_ms
        self.replaced = False
        self.close_reason: str | None = None
        self.closed = asyncio.Event()
        self._frame_seen = asyncio.Event()
        self._pending: dict[int, _Request] = {}
        self._semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._background: set[asyncio.Task[bool]] = set()

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send(self, frame: dict[str, Any]) -> bool:
        """Writes one frame; False (and the connection closed) when it fails or does not drain
        within WRITE_STALL_SECONDS."""
        if self.closed.is_set():
            return False
        try:
            async with asyncio.timeout(WRITE_STALL_SECONDS), self._write_lock:
                if self.closed.is_set():
                    return False
                self.writer.write(encode_frame(frame))
                await self.writer.drain()
        except TimeoutError:
            self.hub.logger.warn("agent connection stopped reading", {"machine": self.machine})
            await self.close("write_stalled")
            return False
        except (ConnectionError, OSError, RuntimeError):
            await self.close("write_failed")
            return False
        return True

    def send_later(self, frame: dict[str, Any]) -> None:
        """`send` in the background, for frames sent after the caller's deadline (cancel)."""
        task = asyncio.get_running_loop().create_task(self.send(frame))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self, reason: str, *, bye: str | None = None) -> None:
        """Closes once: optionally a `bye` first, then every waiting request fails."""
        if self.closed.is_set():
            return
        self.close_reason = reason
        self.replaced = reason == "replaced"
        if bye is not None:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                self.writer.write(encode_frame({"t": "bye", "reason": bye}))
                await asyncio.wait_for(self.writer.drain(), 2)
        self.closed.set()
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            self.writer.close()
            if self.writer.transport.get_write_buffer_size():
                # close() would wait to flush buffers the machine never reads (write_stalled).
                self.writer.transport.abort()
        for request in self._pending.values():
            request.queue.put_nowait(None)

    async def run(self, heartbeat_seconds: float) -> None:
        """Reads frames and pings until the connection closes."""
        beat = asyncio.create_task(self._heartbeat(heartbeat_seconds))
        try:
            await self._read()
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _read(self) -> None:
        while not self.closed.is_set():
            try:
                line = await self.reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                await self.close("frame_too_large", bye="protocol")
                return
            except (ConnectionError, OSError):
                await self.close("eof")
                return
            if not line.endswith(b"\n"):
                await self.close("eof")
                return
            frame = parse_machine_frame(line[:-1])
            if frame is None:
                # Machine output is untrusted: no resynchronisation.
                await self.close("protocol", bye="protocol")
                return
            self._frame_seen.set()
            if frame["t"] == "pong":
                continue
            frame_id = frame["id"]
            request = self._pending.get(frame_id) if isinstance(frame_id, int) else None
            if request is not None:
                request.queue.put_nowait((frame, len(line)))
            # A late answer (cancelled, timed out) or an error without id is dropped.

    async def _heartbeat(self, seconds: float) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(seconds)
            self._frame_seen.clear()
            if not await self.send({"t": "ping", "id": self.new_id()}):
                return
            try:
                await asyncio.wait_for(self._frame_seen.wait(), PONG_SECONDS)
            except TimeoutError:
                self.hub.logger.warn("agent heartbeat missed", {"machine": self.machine})
                await self.close("heartbeat")
                return

    async def request(
        self,
        verb: str,
        args: Sequence[str],
        data: str | None,
        deadline: float,
        sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]] | TransportResult | _Closed:
        """The documents of one request, a failed result, or _Closed (with `written` folded in:
        _Closed is returned only after the request frame was handed to the connection). With
        `sink` each document goes there as it arrives instead (the list stays empty); a
        ResponseError from it cancels the request as `bad_response`."""
        started = time.monotonic()

        def failed(code: TransportError, detail: str | None = None) -> TransportResult:
            return TransportResult(
                ok=False, error=code, detail=detail, elapsed_ms=round((time.monotonic() - started) * 1000)
            )

        frame: dict[str, Any] = {"t": "req", "id": 0, "verb": verb, "args": list(args)}
        if data is not None:
            frame["input"] = data
        request_id: int | None = None
        written = False
        cap = response_cap(verb)
        try:
            async with asyncio.timeout_at(deadline), self._semaphore:
                if self.closed.is_set():
                    return failed("not_connected")
                request_id = self.new_id()
                frame["id"] = request_id
                line = encode_frame(frame)
                if len(line) > MAX_HUB_FRAME_BYTES:
                    return failed("other")
                request = _Request()
                self._pending[request_id] = request
                written = True
                if not await self.send(frame):
                    return _Closed(self.replaced)
                docs: list[dict[str, Any]] = []
                size = 0
                while True:
                    item = await request.queue.get()
                    if item is None:
                        return _Closed(self.replaced)
                    answer, length = item
                    if answer["t"] == "error":
                        code = str(answer["error"])
                        return failed("refused_by_policy" if code in NOT_RUN_ERRORS else "other", code)
                    size += length
                    if size > cap:
                        self.send_later({"t": "cancel", "id": request_id})
                        return failed("bad_response")
                    if sink is None:
                        docs.append(answer["doc"])
                    else:
                        try:
                            sink(answer["doc"])
                        except ResponseError:
                            if answer.get("more"):
                                self.send_later({"t": "cancel", "id": request_id})
                            return failed("bad_response")
                    if not answer.get("more"):
                        return docs
        except TimeoutError:
            if written and request_id is not None and not self.closed.is_set():
                # The deadline has passed: the cancel must not hold up the caller.
                self.send_later({"t": "cancel", "id": request_id})
            return failed("timeout")
        finally:
            if request_id is not None:
                self._pending.pop(request_id, None)


@dataclass
class MachineState:
    """What `status` shows for one agent machine, kept across connections."""

    replacements: list[int] = field(default_factory=list)
    last_disconnect_ms: int | None = None
    last_disconnect_reason: str | None = None


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    option = getattr(socket, "SO_PEERCRED", None)
    if sock is None or option is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


class AgentHub:
    """The relay socket server, the live connections and the control socket."""

    def __init__(
        self,
        remote: RemoteConfig,
        state_dir: str,
        logger: Logger,
        *,
        now: Callable[[], int] | None = None,
        control_path: str | None = None,
    ) -> None:
        self.remote = remote
        self.settings = remote.agents
        self.logger = logger.child({"component": "agents"})
        self.now = now or now_ms
        self.machines = {m.name: m for m in remote.machines if m.transport == "agent"}
        self.control_path = control_path or os.path.join(state_dir, CONTROL_SOCKET_NAME)
        self.problem: str | None = None
        """Why the relay socket is not served (shown by `status` and `remote-check`)."""
        self.check: Callable[[str], Awaitable[tuple[list[str], bool]]] | None = None
        """`check <machine>` of the control socket (set by RemoteTools: version and list, formatted)."""
        self.on_link: Callable[[str, bool, str | None, int], None] | None = None
        """Called with (machine, up, close reason, time) when a connection is accepted and when one
        closes that was not replaced and not closed by the hub's own stop (the incident log)."""
        self.on_relay_problem: Callable[[str, str | None], None] | None = None
        """Called with (code, machine) for a relay socket that is not served or a relay connection
        refused before its hello (the incident log)."""
        self._live: dict[str, AgentConnection] = {}
        self._state: dict[str, MachineState] = {name: MachineState() for name in self.machines}
        self._relay_uid: int | None = None
        self._servers: list[asyncio.AbstractServer] = []
        self._open = 0
        self._handshaking: dict[str, int] = {}
        self._handlers: set[asyncio.Task[None]] = set()
        self.started_ms = self.now()

    @property
    def enabled(self) -> bool:
        return bool(self.machines)

    def transport(self, machine: MachineConfig) -> AgentTransport:
        return AgentTransport(machine, self)

    def live(self, name: str) -> AgentConnection | None:
        conn = self._live.get(name)
        return conn if conn is not None and not conn.closed.is_set() else None

    # --- startup ---------------------------------------------------------------------------

    def _check_directory(self, gid: int) -> str | None:
        directory = os.path.dirname(self.settings.socket_path)
        try:
            info = os.lstat(directory)
        except OSError:
            return f"{directory} does not exist (see the tmpfiles line in the setup guide)"
        if not stat.S_ISDIR(info.st_mode):
            return f"{directory} is not a directory"
        if info.st_uid != os.getuid():
            return f"{directory} is not owned by the hub's user"
        if info.st_gid != gid:
            return f"{directory} does not have the group of {self.settings.relay_user}"
        if stat.S_IMODE(info.st_mode) != SOCKET_DIR_MODE:
            return f"{directory} must have mode 2750 (it has {stat.S_IMODE(info.st_mode):o})"
        return None

    @staticmethod
    def _remove_stale(path: str) -> str | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as err:
            return f"{path}: {err.strerror}"
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return f"{path} exists and is not a socket of the hub's user"
        os.unlink(path)
        return None

    async def _bind(
        self, path: str, mode: int, handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
    ) -> str | None:
        if len(os.fsencode(path)) > MAX_UNIX_PATH:
            return f"{path} is too long for a Unix socket"
        problem = self._remove_stale(path)
        if problem:
            return problem
        # The socket is bound and chmod-ed before it listens, so nobody connects while it still has
        # the umask's mode (and the process-wide umask is never changed).
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
            os.chmod(path, mode)
            server = await asyncio.start_unix_server(handler, sock=sock, limit=MAX_MACHINE_FRAME_BYTES + 1)
        except OSError as err:
            sock.close()
            return f"{path}: {err.strerror}"
        self._servers.append(server)
        return None

    async def start(self) -> None:
        """Serves the relay socket and the control socket. Problems are logged and kept in
        `problem`; every agent machine then reads as not connected."""
        if not self.enabled:
            return
        problem: str | None
        try:
            relay = pwd.getpwnam(self.settings.relay_user)
        except KeyError:
            problem = f"the relay user {self.settings.relay_user} does not exist"
        else:
            self._relay_uid = relay.pw_uid
            problem = self._check_directory(relay.pw_gid) or await self._bind(
                self.settings.socket_path, 0o660, self._accept
            )
        if problem:
            self.problem = problem
            self.logger.warn("agent socket not served", {"problem": problem})
            self._relay_problem("socket_not_served", None)
        else:
            self.logger.info("agent socket served", {"path": self.settings.socket_path})
        control = await self._bind(self.control_path, 0o600, self._control)
        if control:
            self.logger.warn("agent control socket not served", {"problem": control})

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
        for conn in list(self._live.values()):
            await conn.close("shutdown", bye="shutdown")
        for task in list(self._handlers):
            task.cancel()
        for task in list(self._handlers):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()
        for path in (self.settings.socket_path, self.control_path):
            with contextlib.suppress(OSError):
                info = os.lstat(path)
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    os.unlink(path)

    # --- relay connections -------------------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self._connection(reader, writer)
        finally:
            if task is not None:
                self._handlers.discard(task)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = _peer_uid(writer)
        if uid is None or uid != self._relay_uid:
            self.logger.warn("agent connection from another user refused", {"uid": uid})
            self._relay_problem("relay_wrong_user", None)
            writer.close()
            return
        if self._open >= 2 * len(self.machines) + 4:
            self.logger.warn("agent connection refused: too many connections")
            self._relay_problem("relay_too_many", None)
            writer.close()
            return
        self._open += 1
        conn: AgentConnection | None = None
        try:
            conn = await self._handshake(reader, writer)
            if conn is not None:
                await conn.run(float(self.settings.heartbeat_seconds))
        finally:
            self._open -= 1
            if conn is not None:
                await conn.close(conn.close_reason or "eof")
                state = self._state[conn.machine]
                closed_ms = self.now()
                state.last_disconnect_ms = closed_ms
                state.last_disconnect_reason = conn.close_reason
                if self._live.get(conn.machine) is conn:
                    del self._live[conn.machine]
                self.logger.info("agent disconnected", {"machine": conn.machine, "reason": conn.close_reason})
                if conn.close_reason not in ("replaced", "shutdown") and self.on_link is not None:
                    self.on_link(conn.machine, False, conn.close_reason, closed_ms)
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.close()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> AgentConnection | None:
        async def line_within(seconds: float) -> bytes | None:
            try:
                line = await asyncio.wait_for(reader.readline(), seconds)
            except (TimeoutError, ValueError, asyncio.LimitOverrunError, ConnectionError, OSError):
                return None
            return line[:-1] if line.endswith(b"\n") else None

        async def bye(reason: str) -> None:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError, TimeoutError):
                writer.write(encode_frame({"t": "bye", "reason": reason}))
                await asyncio.wait_for(writer.drain(), 2)

        raw = await line_within(PREAMBLE_SECONDS)
        preamble = parse_preamble(raw) if raw is not None else None
        if preamble is None:
            self.logger.warn("agent connection without a valid relay preamble")
            self._relay_problem("relay_protocol", None)
            await bye("protocol")
            return None
        name = preamble.machine
        if name not in self.machines:
            self.logger.warn("agent connection for an unknown machine", {"machine": name})
            self._relay_problem("relay_unknown_machine", None)
            await bye("unknown_machine")
            return None
        if self._handshaking.get(name, 0) >= MAX_UNFINISHED_HANDSHAKES:
            self.logger.warn("agent connection refused: too many unfinished handshakes", {"machine": name})
            await bye("protocol")
            return None
        self._handshaking[name] = self._handshaking.get(name, 0) + 1
        try:
            raw = await line_within(HELLO_SECONDS)
        finally:
            self._handshaking[name] -= 1
        hello = parse_hello(raw) if raw is not None else None
        if hello is None:
            self.logger.warn("agent connection without a valid hello", {"machine": name})
            self._relay_problem("relay_protocol", name)
            await bye("protocol")
            return None
        conn = AgentConnection(
            self, machine=name, reader=reader, writer=writer, hello=hello, peer=preamble.peer, since_ms=self.now()
        )
        old = self._live.get(name)
        self._live[name] = conn
        if old is not None:
            await old.close("replaced", bye="replaced")
            self._note_replacement(name)
        if not await conn.send({"t": "welcome", "protocol": PROTOCOL}):
            return None
        self.logger.info(
            "agent connected",
            {
                "machine": name,
                "peer": preamble.peer,
                "version": hello.version,
                "policy": hello.policy,
                "replaced": old is not None,
            },
        )
        for problem in self.disagreements(name, hello):
            self.logger.warn("agent policy differs", {"machine": name, "problem": problem})
        if self.on_link is not None:
            self.on_link(name, True, None, conn.since_ms)
        return conn

    def _relay_problem(self, code: str, machine: str | None) -> None:
        if self.on_relay_problem is not None:
            try:
                self.on_relay_problem(code, machine)
            except Exception as err:
                self.logger.error("incident observation failed", {"error": type(err).__name__})

    def _note_replacement(self, name: str) -> None:
        now = self.now()
        state = self._state[name]
        state.replacements = [t for t in state.replacements if now - t <= HOUR_MS] + [now]
        recent = sum(1 for t in state.replacements if now - t <= FLAP_WINDOW_MS)
        if recent > FLAP_LIMIT:
            self.logger.warn(
                "agent connection replaced often: two connectors share a key, or the key was stolen",
                {"machine": name, "replacements": recent},
            )

    def disagreements(self, name: str, hello: Hello) -> list[str]:
        machine = self.machines[name]
        out = []
        if hello.policy != "ok":
            out.append("the dispatcher's command line is broken (bad_policy); it refuses everything")
        if machine.allow_send != hello.allow_send:
            out.append(f"allowSend is {'on' if machine.allow_send else 'off'} on the hub, --allow-send is not the same")
        if machine.allow_usage != hello.allow_usage:
            out.append(
                f"allowUsage is {'on' if machine.allow_usage else 'off'} on the hub, --allow-usage is not the same"
            )
        if machine.allow_attachments != hello.allow_attachments:
            out.append(
                f"allowAttachments is {'on' if machine.allow_attachments else 'off'} on the hub, "
                "--allow-attachments is not the same"
            )
        return out

    # --- status and control ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self.now()
        machines = []
        for name in self.machines:
            state = self._state[name]
            conn = self.live(name)
            entry: dict[str, Any] = {
                "name": name,
                "connected": conn is not None,
                "replacementsLastHour": sum(1 for t in state.replacements if now - t <= HOUR_MS),
                "lastDisconnectMs": state.last_disconnect_ms,
                "lastDisconnectReason": state.last_disconnect_reason,
            }
            if conn is not None:
                entry.update(
                    {
                        "sinceMs": conn.since_ms,
                        "version": conn.hello.version,
                        "verbs": list(conn.hello.verbs),
                        "allowSend": conn.hello.allow_send,
                        "allowUsage": conn.hello.allow_usage,
                        "allowAttachments": conn.hello.allow_attachments,
                        "policy": conn.hello.policy,
                        "peer": conn.peer,
                        "warnings": self.disagreements(name, conn.hello),
                    }
                )
            machines.append(entry)
        return {"ok": True, "nowMs": now, "startedMs": self.started_ms, "problem": self.problem, "machines": machines}

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if _peer_uid(writer) != os.getuid():
                return
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
            except (TimeoutError, ValueError, ConnectionError, OSError):
                return
            request = _object(line[:MAX_CONTROL_BYTES])
            answer: dict[str, Any]
            if request is not None and request == {"t": "status"}:
                answer = self.status()
            elif (
                request is not None
                and set(request) == {"t", "machine"}
                and request["t"] == "check"
                and isinstance(request["machine"], str)
                and request["machine"] in self.machines
                and self.check is not None
            ):
                lines, ok = await self.check(request["machine"])
                answer = {"ok": ok, "lines": lines}
            else:
                answer = {"ok": False, "error": "bad_request"}
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.write(encode_frame(answer))
                await writer.drain()
        finally:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.close()


class AgentTransport:
    """`Transport.call` over a machine's own outbound connection."""

    def __init__(self, machine: MachineConfig, hub: AgentHub) -> None:
        self.machine = machine
        self._hub = hub

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        started = time.monotonic()

        def failed(code: TransportError, detail: str | None = None) -> TransportResult:
            return TransportResult(
                ok=False, error=code, detail=detail, elapsed_ms=round((time.monotonic() - started) * 1000)
            )

        try:
            request_tokens(verb, args)
            data = stdin.decode("utf-8") if stdin is not None else None
        except (ValueError, UnicodeDecodeError):
            return failed("other")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        attempts = 2 if verb in READ_ONLY_VERBS else 1
        outcome: list[dict[str, Any]] | TransportResult | _Closed = _Closed(False)
        stream: AttachmentStream | None = None
        for _ in range(attempts):
            conn = self._hub.live(self.machine.name)
            if conn is None:
                return failed("not_connected")
            # An attachment is decoded chunk by chunk as the frames arrive (a fresh stream for a
            # retry on a new connection).
            stream = AttachmentStream() if verb == "attachment" else None
            sink = stream.feed if stream is not None else None
            outcome = await conn.request(verb, args, data, deadline, sink)
            if not (isinstance(outcome, _Closed) and outcome.replaced):
                break
        if isinstance(outcome, _Closed):
            # The request went out: for send, keys and resize the outcome is unknown.
            return failed("disconnected")
        if isinstance(outcome, TransportResult):
            return outcome
        parsed: BaseModel | str
        try:
            if stream is not None:
                parsed = stream.result()
            else:
                stdout = b"\n".join(json.dumps(doc, ensure_ascii=True).encode("ascii") for doc in outcome)
                parsed = parse_response(verb, stdout)
        except ResponseError:
            return failed("bad_response")
        if isinstance(parsed, str):
            return failed(dispatcher_error(parsed), parsed)
        return TransportResult(ok=True, data=parsed, elapsed_ms=round((time.monotonic() - started) * 1000))


# --- CLI side ------------------------------------------------------------------------------------


async def control_request(
    path: str, request: dict[str, Any], *, seconds: float = CONTROL_SECONDS
) -> dict[str, Any] | None:
    """One request to the hub's control socket; None when the hub is not running (or answers badly)."""
    try:
        async with asyncio.timeout(seconds):
            reader, writer = await asyncio.open_unix_connection(path, limit=MAX_CONTROL_BYTES * 16)
            try:
                writer.write(encode_frame(request))
                await writer.drain()
                line = await reader.readline()
            finally:
                writer.close()
    except (TimeoutError, OSError, ValueError):
        return None
    return _object(line)


_RELAY_COMMAND_RE = re.compile(
    r"\S+ -I -m voice_coding_mcp\.remote\.relay --machine ([a-z][a-z0-9-]{0,31})(?: --socket /[A-Za-z0-9._/-]{1,100})?"
)


def _split_options(text: str) -> tuple[list[str], str] | None:
    """The options field of an authorized_keys line (commas outside double quotes) and the rest."""
    options: list[str] = []
    current = ""
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quoted and index + 1 < len(text):
            current += text[index : index + 2]
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            options.append(current)
            current = ""
            index += 1
            continue
        elif char in " \t" and not quoted:
            options.append(current)
            return options, text[index:].strip()
        current += char
        index += 1
    if quoted:
        return None
    options.append(current)
    return options, ""


KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


def lint_authorized_keys(text: str, agent_machines: Sequence[str]) -> list[str]:
    """Problems of the relay account's authorized_keys: every key line needs `restrict` and exactly
    the relay `command=` for a configured agent machine; no key twice. Reports, never edits."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    named: set[str] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"line {number}"
        first = line.split(None, 1)[0]
        if first in KEY_TYPES:
            problems.append(f"{where}: a key without options (it needs restrict and the relay command=)")
            key = line.split()[1] if len(line.split()) > 1 else ""
        else:
            split = _split_options(line)
            if split is None:
                problems.append(f"{where}: cannot read the options")
                continue
            options, rest = split
            parts = rest.split()
            if len(parts) < 2 or parts[0] not in KEY_TYPES:
                problems.append(f"{where}: no key after the options")
                continue
            key = parts[1]
            if "restrict" not in options:
                problems.append(f"{where}: no restrict option")
            commands = [o for o in options if o.startswith("command=")]
            if len(commands) != 1:
                problems.append(f"{where}: needs exactly one command= option")
            else:
                value = commands[0][len("command=") :]
                command = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else ""
                found = _RELAY_COMMAND_RE.fullmatch(command)
                if found is None:
                    problems.append(f"{where}: command= is not the switchboard relay")
                elif found.group(1) not in agent_machines:
                    problems.append(f"{where}: --machine {found.group(1)} is not a configured agent machine")
                else:
                    named.add(found.group(1))
            unexpected = [
                o.split("=", 1)[0] for o in options if o != "restrict" and not o.startswith(("command=", "from="))
            ]
            if unexpected:
                problems.append(f"{where}: unexpected options {', '.join(sorted(set(unexpected)))}")
        if key in seen:
            problems.append(f"{where}: the same key as line {seen[key]}")
        else:
            seen[key] = number
    problems += [f"no key line for agent machine {name}" for name in agent_machines if name not in named]
    return problems
