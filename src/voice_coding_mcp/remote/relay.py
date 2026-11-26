"""The relay: forced command of the hub's relay account (docs/SWITCHBOARD.md, "Outbound agents").

One line per machine key in the relay account's root-owned `authorized_keys`:

    restrict,command="<venv>/bin/python -I -m voice_coding_mcp.remote.relay --machine laptop" ssh-ed25519 ...

sshd runs this with the SSH channel as stdin and stdout. The relay connects to the hub's Unix
socket, writes one preamble line naming the machine, and then copies bytes both ways until either
side closes. The machine name comes only from `--machine`, that is from which key authenticated;
nothing from the stream or from `SSH_ORIGINAL_COMMAND` is read. It parses no frame: machine to hub it
only splits at newlines to enforce the frame cap. Standard library only (no third-party package is
loaded on each connection); its stderr goes back to the machine, so it prints only fixed lines.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import stat
import sys
import threading
from collections.abc import Sequence
from typing import TextIO

MACHINE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}", re.ASCII)
SOCKET_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,100}", re.ASCII)
PEER_RE = re.compile(r"[0-9A-Fa-f.:]{2,45}", re.ASCII)
DEFAULT_SOCKET = "/run/switchboard-relay/hub.sock"
PROTOCOL = 1
MAX_MACHINE_FRAME_BYTES = 5 * 1024 * 1024
"""Longest frame from the machine (the dispatcher's `MAX_MACHINE_FRAME_BYTES`)."""
CHUNK = 65536

EXIT_OK = 0
EXIT_HUB = 1
"""The hub is not running, or its socket did not pass the checks."""
EXIT_USAGE = 2
EXIT_TOO_LARGE = 3

MESSAGES = {
    "usage": "switchboard relay: bad arguments",
    "hub": "switchboard relay: hub not running",
    "refused": "switchboard relay: refused",
    "too_large": "switchboard relay: frame too large",
}


class RelayError(Exception):
    def __init__(self, key: str, code: int) -> None:
        super().__init__(key)
        self.key = key
        self.code = code


def parse_args(argv: Sequence[str]) -> tuple[str, str]:
    """(machine, socket path) from `--machine <name> [--socket <path>]`, nothing else."""
    machine: str | None = None
    path: str | None = None
    items = list(argv)
    index = 0
    while index < len(items):
        option = items[index]
        if option not in ("--machine", "--socket") or index + 1 >= len(items):
            raise RelayError("usage", EXIT_USAGE)
        value = items[index + 1]
        if option == "--machine":
            if machine is not None or not MACHINE_RE.fullmatch(value):
                raise RelayError("usage", EXIT_USAGE)
            machine = value
        else:
            if path is not None or not SOCKET_PATH_RE.fullmatch(value) or ".." in value.split("/"):
                raise RelayError("usage", EXIT_USAGE)
            path = value
        index += 2
    if machine is None:
        raise RelayError("usage", EXIT_USAGE)
    return machine, path or DEFAULT_SOCKET


def check_socket(path: str, own_uid: int) -> None:
    """The socket and its directory must not be the relay account's own or writable by others: the
    relay hands machine traffic only to a socket the hub created. RelayError otherwise."""
    try:
        directory = os.lstat(os.path.dirname(path))
        target = os.lstat(path)
    except OSError:
        raise RelayError("hub", EXIT_HUB) from None
    if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(target.st_mode):
        raise RelayError("refused", EXIT_HUB)
    for info in (directory, target):
        if info.st_uid == own_uid or info.st_mode & stat.S_IWOTH:
            raise RelayError("refused", EXIT_HUB)


def peer_address(env: dict[str, str]) -> str | None:
    """The client address of `SSH_CONNECTION` (shown on the hub, never trusted), when it looks like one."""
    first = (env.get("SSH_CONNECTION") or "").split(" ")[0]
    return first if PEER_RE.fullmatch(first) else None


def preamble(machine: str, peer: str | None) -> bytes:
    frame = {"t": "relay", "protocol": PROTOCOL, "machine": machine, "peer": peer}
    return (json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def pump(in_fd: int, out_fd: int, conn: socket.socket, limit: int = MAX_MACHINE_FRAME_BYTES) -> int:
    """Copies `in_fd` (the machine) to `conn` (the hub) and `conn` to `out_fd` until the hub closes
    its side. A frame from the machine longer than `limit` closes everything. The exit code."""
    oversize = threading.Event()

    def machine_to_hub() -> None:
        run = 0
        # run: bytes of the frame being copied (since the last newline).
        try:
            while not oversize.is_set():
                chunk = os.read(in_fd, CHUNK)
                if not chunk:
                    break
                start = 0
                while True:
                    end = chunk.find(b"\n", start)
                    run += (len(chunk) if end < 0 else end) - start
                    if run > limit:
                        oversize.set()
                        break
                    if end < 0:
                        break
                    run, start = 0, end + 1
                if not oversize.is_set():
                    conn.sendall(chunk)
        except OSError:
            pass
        # Oversized: stop both directions, the hub must never buffer more than one capped frame.
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR if oversize.is_set() else socket.SHUT_WR)

    upstream = threading.Thread(target=machine_to_hub, daemon=True)
    upstream.start()
    try:
        while True:
            data = conn.recv(CHUNK)
            if not data:
                break
            _write_all(out_fd, data)
    except OSError:
        pass
    if oversize.is_set():
        return EXIT_TOO_LARGE
    return EXIT_OK


def run(
    argv: Sequence[str],
    env: dict[str, str],
    *,
    in_fd: int = 0,
    out_fd: int = 1,
    own_uid: int | None = None,
    err: TextIO | None = None,
) -> int:
    """One relay session. `own_uid` is the relay account's uid (tests pass another one, since the
    test's hub socket is the test user's own)."""
    stream = err if err is not None else sys.stderr
    try:
        machine, path = parse_args(argv)
        check_socket(path, os.getuid() if own_uid is None else own_uid)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
            conn.sendall(preamble(machine, peer_address(env)))
        except OSError:
            conn.close()
            raise RelayError("hub", EXIT_HUB) from None
    except RelayError as failed:
        print(MESSAGES[failed.key], file=stream)
        return failed.code
    try:
        code = pump(in_fd, out_fd, conn)
    finally:
        conn.close()
    if code == EXIT_TOO_LARGE:
        print(MESSAGES["too_large"], file=stream)
    return code


def main() -> int:
    try:
        return run(sys.argv[1:], dict(os.environ))
    except Exception:  # stderr reaches the machine: never a traceback with hub paths
        print("switchboard relay: internal error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
