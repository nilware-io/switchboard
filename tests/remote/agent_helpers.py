"""Helpers for the outbound-agent tests: serve mode on pipes, a short socket directory with the
modes the hub demands, a fake machine speaking the frame protocol over the hub's socket, and the
real chain (dispatcher in serve mode, relay) wired with pipes instead of ssh.

Nothing here opens a network connection or runs ssh, tmux or claude: the dispatcher talks to the
fake tmux, and the relay's own-uid check gets a uid that is not the test user's (the test's hub
socket is the test user's own)."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import select
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.remote.helpers import DISPATCHER, RemoteWorld
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import relay

OWN_USER = pwd.getpwuid(os.getuid()).pw_name
NOT_OWN_UID = os.getuid() + 1
"""Passed to the relay as its own uid, so the hub socket (the test user's) passes its owner check."""
PEER = "203.0.113.5 50000 192.0.2.1 22"


def frame(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, separators=(",", ":")) + "\n").encode("ascii")


class FrameReader:
    """Reads newline-delimited frames from a file descriptor with a timeout."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = bytearray()
        self.eof = False

    def line(self, timeout: float = 10.0) -> bytes | None:
        """The next line without its newline, or None at end of input. Fails on timeout."""
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                line = bytes(self.buffer[:end])
                del self.buffer[: end + 1]
                return line
            if self.eof:
                return None
            ready, _, _ = select.select([self.fd], [], [], timeout)
            assert ready, "no frame within the timeout"
            chunk = os.read(self.fd, 1 << 20)
            if not chunk:
                self.eof = True
            self.buffer.extend(chunk)

    def frame(self, timeout: float = 10.0) -> dict[str, Any] | None:
        line = self.line(timeout)
        if line is None:
            return None
        line.decode("ascii")  # every frame is ASCII
        data = json.loads(line)
        assert isinstance(data, dict)
        return data


class ServeSession:
    """`dispatcher.serve` in a thread, on two pipes; the test plays the hub."""

    def __init__(self, argv: list[str], env: dict[str, str], *, silence: float = 30.0) -> None:
        to_dispatcher = os.pipe()
        from_dispatcher = os.pipe()
        self._hub_write = to_dispatcher[1]
        self.reader = FrameReader(from_dispatcher[0])
        self._fds = (to_dispatcher[0], from_dispatcher[1], from_dispatcher[0])
        self.code: int | None = None

        def run() -> None:
            try:
                self.code = d.serve(argv, env, to_dispatcher[0], from_dispatcher[1], silence)
            finally:
                os.close(to_dispatcher[0])
                os.close(from_dispatcher[1])

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self._closed = False

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._hub_write, view) :]

    def send(self, data: dict[str, Any]) -> None:
        self.write(frame(data))

    def read(self, timeout: float = 10.0) -> dict[str, Any] | None:
        return self.reader.frame(timeout)

    def request(self, request_id: int, verb: str, args: list[str], **extra: Any) -> list[dict[str, Any]]:
        """Sends one request and returns its response documents (every `res` frame of its id)."""
        self.send({"t": "req", "id": request_id, "verb": verb, "args": args, **extra})
        docs = []
        while True:
            got = self.read()
            assert got is not None
            assert got["t"] == "res", got
            assert got["id"] == request_id
            docs.append(got["doc"])
            if not got.get("more"):
                return docs

    def close_input(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._hub_write)

    def finish(self, timeout: float = 20.0) -> int | None:
        self.close_input()
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        os.close(self.reader.fd)
        return self.code


@contextmanager
def socket_dir() -> Iterator[Path]:
    """A short directory (Unix socket paths are limited to 108 bytes) owned by the test user, with
    the test user's primary group and mode 2750, as the hub demands of the relay directory."""
    path = Path(tempfile.mkdtemp(prefix="sb-"))
    try:
        os.chown(path, -1, pwd.getpwuid(os.getuid()).pw_gid)
        path.chmod(0o2750)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def agent_machine(name: str, **keys: Any) -> dict[str, Any]:
    return {"name": name, "transport": "agent", "sessions": ["fpga", "notes", "work-*"], **keys}


def agents_section(directory: Path, **keys: Any) -> dict[str, Any]:
    return {"socketPath": str(directory / "hub.sock"), "relayUser": OWN_USER, **keys}


class FakeMachine:
    """A machine speaking the frame protocol directly on the hub's socket (as the relay would carry
    it), so a test controls every frame."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(
        cls,
        path: str,
        machine: str = "laptop",
        *,
        hello: dict[str, Any] | None = None,
        peer: str | None = "203.0.113.5",
        send_hello: bool = True,
    ) -> FakeMachine:
        reader, writer = await asyncio.open_unix_connection(path, limit=16 << 20)
        fake = cls(reader, writer)
        await fake.send({"t": "relay", "protocol": 1, "machine": machine, "peer": peer})
        if send_hello:
            await fake.send(hello or hello_frame())
        return fake

    async def send(self, data: dict[str, Any]) -> None:
        await self.write(frame(data))

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def read(self, seconds: float = 10.0) -> dict[str, Any] | None:
        try:
            line = await asyncio.wait_for(self.reader.readline(), seconds)
        except ConnectionResetError:
            return None
        if not line:
            return None
        data = json.loads(line)
        assert isinstance(data, dict)
        return data

    async def expect(self, kind: str, seconds: float = 10.0) -> dict[str, Any]:
        """The next frame that is not a ping (pings are answered)."""
        while True:
            got = await self.read(seconds)
            assert got is not None, f"connection closed while waiting for {kind}"
            if got["t"] == "ping" and kind != "ping":
                await self.send({"t": "pong", "id": got["id"]})
                continue
            assert got["t"] == kind, got
            return got

    async def closed(self, seconds: float = 10.0) -> list[dict[str, Any]]:
        """Frames until the hub closes the connection."""
        frames: list[dict[str, Any]] = []
        while True:
            got = await self.read(seconds)
            if got is None:
                return frames
            frames.append(got)

    def close(self) -> None:
        self.writer.close()


def hello_frame(**keys: Any) -> dict[str, Any]:
    return {
        "t": "hello",
        "protocol": 1,
        "version": d.VERSION,
        "verbs": list(d.VERBS),
        "allowSend": False,
        "allowUsage": True,
        "policy": "ok",
        **keys,
    }


def ok_doc(**body: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": True, **body}


class MachineLink:
    """The real machine side without ssh: the dispatcher in serve mode (a child process on the fake
    tmux) and the relay (a thread here), wired with pipes as sshd and ssh would carry them."""

    def __init__(
        self, world: RemoteWorld, socket_path: str, machine: str, policy: list[str], dispatcher: str = DISPATCHER
    ) -> None:
        self.world = world
        self.dispatcher = subprocess.Popen(
            [sys.executable, "-I", dispatcher, "--serve", *policy],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={**world.tmux_env(), "PATH": "/usr/bin:/bin"},
        )
        assert self.dispatcher.stdin is not None
        assert self.dispatcher.stdout is not None
        self.relay_code: int | None = None
        self.relay_err = _Lines()
        in_fd, out_fd = self.dispatcher.stdout.fileno(), self.dispatcher.stdin.fileno()

        def run() -> None:
            self.relay_code = relay.run(
                ["--machine", machine, "--socket", socket_path],
                {"SSH_CONNECTION": PEER, "SSH_ORIGINAL_COMMAND": "--machine other"},
                in_fd=in_fd,
                out_fd=out_fd,
                own_uid=NOT_OWN_UID,
                err=self.relay_err,  # type: ignore[arg-type]
            )
            # sshd closes the channel when the forced command ends.
            assert self.dispatcher.stdin is not None
            self.dispatcher.stdin.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self) -> int:
        """Closes the machine side; the dispatcher's exit code."""
        if self.dispatcher.poll() is None:
            self.dispatcher.terminate()
        code = self.dispatcher.wait(20)
        self.thread.join(20)
        assert self.dispatcher.stdout is not None
        self.dispatcher.stdout.close()
        return code


class _Lines:
    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        pass


async def wait_until(predicate: Any, seconds: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not predicate():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.02)
