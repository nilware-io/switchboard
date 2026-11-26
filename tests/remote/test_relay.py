"""The relay (`remote/relay.py`, forced command of the hub's relay account): identity only from
`--machine`, argument validation, socket checks, the frame cap toward the hub, fixed stderr lines,
standard library only."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.remote.agent_helpers import NOT_OWN_UID, socket_dir
from voice_coding_mcp.remote import relay

RELAY = Path(relay.__file__)


class Hub:
    """A listening socket standing in for the hub: records what the relay sends."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        os.chmod(self.path, 0o660)
        self.server.listen(1)

    def accept(self) -> socket.socket:
        self.server.settimeout(10)
        conn, _ = self.server.accept()
        conn.settimeout(10)
        return conn

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def hub() -> Iterator[Hub]:
    with socket_dir() as directory:
        server = Hub(directory / "hub.sock")
        try:
            yield server
        finally:
            server.close()


def read_line(conn: socket.socket) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(1)
        assert chunk
        data += chunk
    return data


class Run:
    """relay.run in a thread with pipes for the SSH channel."""

    def __init__(self, argv: list[str], env: dict[str, str], own_uid: int = NOT_OWN_UID) -> None:
        self.machine_in = os.pipe()  # machine -> relay
        self.machine_out = os.pipe()  # relay -> machine
        self.err = io.StringIO()
        self.code: int | None = None

        def target() -> None:
            self.code = relay.run(
                argv, env, in_fd=self.machine_in[0], out_fd=self.machine_out[1], own_uid=own_uid, err=self.err
            )
            os.close(self.machine_out[1])

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.machine_in[1], view) :]

    def end_input(self) -> None:
        os.close(self.machine_in[1])

    def read_all(self) -> bytes:
        out = b""
        while chunk := os.read(self.machine_out[0], 65536):
            out += chunk
        return out

    def join(self) -> int | None:
        self.thread.join(15)
        assert not self.thread.is_alive()
        return self.code


def test_the_machine_comes_only_from_the_forced_command(hub: Hub) -> None:
    env = {"SSH_CONNECTION": "203.0.113.5 50000 192.0.2.1 22", "SSH_ORIGINAL_COMMAND": "--machine desk"}
    run = Run(["--machine", "laptop", "--socket", hub.path], env)
    conn = hub.accept()
    assert json.loads(read_line(conn)) == {"t": "relay", "protocol": 1, "machine": "laptop", "peer": "203.0.113.5"}
    # A machine that claims another name in the stream only sends a frame the hub reads as data.
    run.write(b'{"t":"relay","protocol":1,"machine":"desk","peer":null}\n')
    assert read_line(conn) == b'{"t":"relay","protocol":1,"machine":"desk","peer":null}\n'
    conn.sendall(b'{"t":"bye","reason":"shutdown"}\n')
    conn.close()
    run.end_input()
    assert run.read_all() == b'{"t":"bye","reason":"shutdown"}\n'
    assert run.join() == 0
    assert run.err.getvalue() == ""


@pytest.mark.parametrize("connection", ["", "garbage here", "203.0.113.5;rm 1 2 3", "x" * 60])
def test_an_odd_peer_address_is_sent_as_null(hub: Hub, connection: str) -> None:
    run = Run(["--machine", "laptop", "--socket", hub.path], {"SSH_CONNECTION": connection})
    conn = hub.accept()
    assert json.loads(read_line(conn))["peer"] is None
    conn.close()
    run.end_input()
    run.join()


def test_bytes_are_copied_both_ways_unchanged(hub: Hub) -> None:
    run = Run(["--machine", "laptop", "--socket", hub.path], {})
    conn = hub.accept()
    read_line(conn)
    upstream = b"".join(b'{"t":"res","id":%d,"doc":{}}\n' % i for i in range(2000))
    writer = threading.Thread(target=run.write, args=(upstream,), daemon=True)
    writer.start()
    got = b""
    while len(got) < len(upstream):
        got += conn.recv(65536)
    assert got == upstream
    downstream = b'{"t":"ping","id":1}\n' * 1000
    conn.sendall(downstream)
    conn.shutdown(socket.SHUT_WR)
    assert run.read_all() == downstream
    run.end_input()
    assert run.join() == 0
    conn.close()


def test_a_frame_over_the_cap_from_the_machine_closes_both_sides(hub: Hub) -> None:
    run = Run(["--machine", "laptop", "--socket", hub.path], {})
    conn = hub.accept()
    read_line(conn)
    run.write(b'{"t":"pong","id":1}\n')
    big = b"x" * (relay.MAX_MACHINE_FRAME_BYTES + 1)
    writer = threading.Thread(target=lambda: _quiet(run.write, big), daemon=True)
    writer.start()
    received = b""
    while chunk := conn.recv(1 << 20):
        received += chunk
    assert received.startswith(b'{"t":"pong","id":1}\n')
    assert len(received) <= relay.MAX_MACHINE_FRAME_BYTES + 100
    assert run.join() == relay.EXIT_TOO_LARGE
    assert run.err.getvalue() == "switchboard relay: frame too large\n"
    conn.close()


def test_a_frame_of_exactly_the_cap_passes(hub: Hub) -> None:
    run = Run(["--machine", "laptop", "--socket", hub.path], {})
    conn = hub.accept()
    read_line(conn)
    line = b"y" * relay.MAX_MACHINE_FRAME_BYTES + b"\n"
    writer = threading.Thread(target=run.write, args=(line,), daemon=True)
    writer.start()
    got = b""
    while len(got) < len(line):
        got += conn.recv(1 << 20)
    assert got == line
    conn.close()
    run.end_input()
    assert run.join() == 0


def _quiet(write: Callable[[bytes], None], data: bytes) -> None:
    with contextlib.suppress(OSError):
        write(data)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--machine"],
        ["--machine", "Laptop"],
        ["--machine", "lap top"],
        ["--machine", "-x"],
        ["--machine", "a" * 33],
        ["--machine", "laptop", "--machine", "desk"],
        ["--machine", "laptop", "--socket", "relative.sock"],
        ["--machine", "laptop", "--socket", "/run/../etc/x.sock"],
        ["--machine", "laptop", "--socket", "/run/a b"],
        ["--machine", "laptop", "--verbose"],
        ["laptop"],
        ["--socket", "/run/x.sock"],
    ],
)
def test_bad_arguments_are_refused_with_a_fixed_line(argv: list[str]) -> None:
    err = io.StringIO()
    assert relay.run(argv, {}, err=err) == relay.EXIT_USAGE
    assert err.getvalue() == "switchboard relay: bad arguments\n"


def test_no_hub_is_a_fixed_line(tmp_path: Path) -> None:
    err = io.StringIO()
    code = relay.run(["--machine", "laptop", "--socket", str(tmp_path / "missing.sock")], {}, err=err)
    assert code == relay.EXIT_HUB
    assert err.getvalue() == "switchboard relay: hub not running\n"


def test_a_socket_of_the_relay_account_itself_is_refused(hub: Hub) -> None:
    err = io.StringIO()
    code = relay.run(["--machine", "laptop", "--socket", hub.path], {}, own_uid=os.getuid(), err=err)
    assert code == relay.EXIT_HUB
    assert err.getvalue() == "switchboard relay: refused\n"


def test_a_socket_writable_by_others_is_refused(hub: Hub) -> None:
    os.chmod(hub.path, 0o666)
    err = io.StringIO()
    assert relay.run(["--machine", "laptop", "--socket", hub.path], {}, err=err) == relay.EXIT_HUB
    assert err.getvalue() == "switchboard relay: refused\n"


def test_a_directory_writable_by_others_is_refused(hub: Hub) -> None:
    directory = os.path.dirname(hub.path)
    os.chmod(directory, 0o2757)
    try:
        err = io.StringIO()
        assert relay.run(["--machine", "laptop", "--socket", hub.path], {}, err=err) == relay.EXIT_HUB
    finally:
        os.chmod(directory, 0o2750)
    assert err.getvalue() == "switchboard relay: refused\n"


def test_a_regular_file_instead_of_a_socket_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hub.sock"
    path.write_text("", encoding="utf-8")
    err = io.StringIO()
    assert relay.run(["--machine", "laptop", "--socket", str(path)], {}, err=err) == relay.EXIT_HUB
    assert err.getvalue() == "switchboard relay: refused\n"


def test_the_relay_uses_only_the_standard_library() -> None:
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_running_the_module_loads_no_third_party_package() -> None:
    code = (
        "import sys, runpy\n"
        "sys.argv = ['relay', '--machine', 'BAD']\n"
        "try:\n"
        "    runpy.run_module('voice_coding_mcp.remote.relay', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "third = sorted(m.split('.')[0] for m in sys.modules if m.split('.')[0] not in sys.stdlib_module_names)\n"
        "print(','.join(sorted(set(third))))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=30)
    # `sitecustomize` and the like come from the interpreter's environment, not from the relay.
    loaded = set(done.stdout.strip().split(",")) - {"__main__", "sitecustomize", "usercustomize", "_virtualenv"}
    assert loaded == {"voice_coding_mcp"}
    assert done.stderr == "switchboard relay: bad arguments\n"
