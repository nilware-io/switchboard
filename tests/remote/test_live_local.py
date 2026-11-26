"""Optional live smoke of the remote tools over the local transport (Switchboard M5, same-user mode on
Linux): RemoteTools -> LocalTransport -> the bundled dispatcher -> a real tmux. Skipped unless
SB_LIVE_TMUX=1.

It starts its own isolated server (`tmux -L sb-test-<random> -f /dev/null`), never the default one,
and always kills that server afterwards. The dispatcher reaches it through its test-only
`--tmux-socket-name` option, accepted only with SWITCHBOARD_REMOTE_TEST=1 in its environment. The
"Claude Code" pane runs tests/fixtures/fake_tui.py, registered in a temporary Claude home exactly
as Claude Code registers a session started inside tmux on Linux.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import FIXTURES, claude_config
from voice_coding_mcp.config import parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.tools import Caller, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import LocalTransport
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

FAKE_TUI = str(FIXTURES / "fake_tui.py")
SESSION_ID = "66666666-0000-4000-8000-00000000000a"
OWNER = Caller(fingerprint="len=36 sha256=0a0b0c0d", name="Claude")

pytestmark = pytest.mark.skipif(
    os.environ.get("SB_LIVE_TMUX") != "1" or shutil.which("tmux") is None,
    reason="live tmux test: set SB_LIVE_TMUX=1 (needs tmux)",
)


class IsolatedLocalTransport(LocalTransport):
    """The real local transport, pointed at the isolated test server."""

    def __init__(
        self, socket_name: str, machine: MachineConfig, base_env: Mapping[str, str], *, slash_commands: Sequence[str]
    ) -> None:
        super().__init__(machine, base_env, slash_commands=slash_commands)
        self.socket_name = socket_name

    def policy_args(self) -> list[str]:
        return [*super().policy_args(), "--tmux-socket-name", self.socket_name]


@dataclass
class Live:
    tools: RemoteTools
    base: list[str]
    record: Path
    registry: Path
    tui_pane: str

    def register(self, **data: object) -> None:
        entry = {
            "pid": os.getpid(),
            "sessionId": SESSION_ID,
            "cwd": "/home/USER/projects/fpga",
            "name": "parser",
            "kind": "interactive",
            "entrypoint": "cli",
            "status": "idle",
            "messagingSocketPath": f"/run/user/{os.getuid()}/cc-socks/{os.getpid()}.sock",
            "tmux": f"fpga:@9.{self.tui_pane}",
            **data,
        }
        self.registry.write_text(json.dumps(entry), encoding="utf-8")


def tmux(base: Sequence[str], *args: str) -> str:
    return subprocess.run([*base, *args], check=True, timeout=10, capture_output=True, text=True).stdout


@dataclass
class Server:
    name: str
    base: list[str]
    record: Path
    tui_pane: str


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    """The isolated tmux server: session `fpga` (a shell-less window that printed a line, and the fake
    TUI as the "Claude Code" pane) and session `private`, which the allow-list leaves out."""
    name = f"sb-test-{secrets.token_hex(6)}"
    base = ["tmux", "-L", name, "-f", "/dev/null"]
    record = tmp_path / "received.bin"
    try:
        # Plain programs, no interactive shell: nothing of the user's own shell setup runs.
        shell = "printf 'hello from %s\\n' live-local; exec sleep 120"
        tmux(base, "new-session", "-d", "-s", "fpga", "-x", "100", "-y", "30", "sh", "-c", shell)
        tmux(base, "new-session", "-d", "-s", "private", "sleep", "120")
        tmux(base, "new-window", "-d", "-t", "fpga", "-n", "tui", sys.executable, "-I", FAKE_TUI, str(record))
        tui_pane = tmux(base, "display-message", "-p", "-t", "fpga:tui", "#{pane_id}").strip()
        deadline = time.monotonic() + 10
        while not Path(f"{record}.ready").exists():
            assert time.monotonic() < deadline, "fake TUI did not start"
            time.sleep(0.05)
        time.sleep(0.3)  # tmux has seen the bracketed paste switch
        yield Server(name=name, base=base, record=record, tui_pane=tui_pane)
    finally:
        subprocess.run([*base, "kill-server"], check=False, timeout=10, capture_output=True)
        socket_dir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(socket_dir, f"tmux-{os.getuid()}", name))


def wait_for_paste(record: Path) -> bytes:
    """What the fake TUI received, once a bracketed paste followed by Enter arrived (at most 10 s)."""
    deadline = time.monotonic() + 10
    while not (record.exists() and record.read_bytes().endswith(b"\x1b[201~\r")):
        assert time.monotonic() < deadline, record.read_bytes() if record.exists() else "nothing received"
        time.sleep(0.05)
    return record.read_bytes()


def audit_lines(path: str) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    claude_home = tmp_path / "claude"
    (claude_home / "sessions").mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.mkdir()
    return claude_home, projects


@pytest.fixture
async def live(server: Server, tmp_path: Path) -> AsyncIterator[Live]:
    claude_home, projects = make_dirs(tmp_path)
    claude = claude_config(str(tmp_path / "record.jsonl"))
    config = parse_config(
        {
            "publicUrl": "https://example-host.example-tailnet.ts.net",
            "stateDir": str(tmp_path / "state"),
            "projectRoots": [str(projects)],
            "claude": {"bin": claude.bin, "env": dict(claude.env)},
            "tasks": {"syncWaitSeconds": 20},
            "remote": {
                "machines": [
                    {
                        "name": "dev",
                        "transport": "local",
                        "sessions": ["fpga"],
                        "claudeHome": str(claude_home),
                        "allowSend": True,
                    }
                ],
                "summary": {"enabled": False},
                "send": {"minConfirmSeconds": 0},
            },
        },
        home_dir="/nonexistent-home-dir",
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SWITCHBOARD_REMOTE_TEST": "1"}
    if "TMUX_TMPDIR" in os.environ:
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    machine = config.remote.machines[0]
    transport = IsolatedLocalTransport(server.name, machine, env, slash_commands=config.remote.send.slash_commands)
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=silent_logger,
            kill_grace_ms=200,
        )
    )
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=manager,
            logger=silent_logger,
            base_env=env,
            transports={"dev": transport},
            claude_home=str(tmp_path / "hub-claude"),
            new_confirmation_id=lambda: "4172",
        )
    )
    try:
        yield Live(
            tools=tools,
            base=server.base,
            record=server.record,
            registry=claude_home / "sessions" / f"{os.getpid()}.json",
            tui_pane=server.tui_pane,
        )
    finally:
        await manager.shutdown("test end")


async def test_the_remote_tools_work_end_to_end_over_the_local_transport(live: Live) -> None:
    live.register()
    listed = await live.tools.list_remote_sessions(None)
    assert not listed.is_error, listed.text
    assert "fpga" in listed.text
    assert "private" not in listed.text, "sessions outside the allow-list stay invisible"

    status = await live.tools.remote_status("dev", "fpga", None)
    assert not status.is_error, status.text
    assert "parser" in status.text or "fpga" in status.text

    shell = await live.tools.remote_status("dev", "fpga:0", None)
    assert not shell.is_error, shell.text
    assert "hello from live-local" in shell.text

    unknown = await live.tools.remote_status("dev", "private", None)
    assert unknown.is_error
    assert "private" in unknown.text  # the answer for a session that does not exist

    live.register(status="waiting", waitingFor="permission prompt")
    pending = await live.tools.pending_input(None)
    assert "fpga" in pending.text, pending.text
    assert "permission" in pending.text
    live.register(status="idle")
    assert "fpga" not in (await live.tools.pending_input(None)).text

    text = "line one\nline two with 'quotes' $(not run)"
    prepared = await live.tools.remote_send("dev", "fpga", text, OWNER)
    assert not prepared.is_error, prepared.text
    assert "4172" in prepared.text
    sent = await live.tools.remote_send_confirm("4172", False, OWNER)
    assert not sent.is_error, sent.text
    assert wait_for_paste(live.record) == b"\x1b[200~line one\rline two with 'quotes' $(not run)\x1b[201~\r"
    assert "sb-" not in tmux(live.base, "list-buffers")
    [line] = audit_lines(live.tools.audit.path)
    audit = json.loads(line)
    assert audit["machine"] == "dev"
    assert audit["outcome"] == "sent"
    assert "line one" not in line, "the audit keeps a keyed hash, not the text"
