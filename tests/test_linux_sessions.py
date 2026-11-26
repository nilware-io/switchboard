"""Upstream's own-session features (Mac mode) on Linux, in same-user mode: the tool layer against a
Claude Code home shaped like the registry Claude Code 2.x writes on Linux and WSL (Switchboard M5).

What differs from macOS: `messagingSocketPath` lies under `/run/user/<uid>/cc-socks/` (or
`/tmp/cc-socks-<uid>/` without XDG_RUNTIME_DIR) instead of `/tmp/cc-socks/`, a session started
inside tmux carries `tmux` (`session:@window.%pane`), `status` may be `waiting` with `waitingFor`,
and there are more keys (`pidDomain`, `procStart`, `peerProtocol`, ...). The relay must address the
socket path from the registry as is; listing, asking and pending_input must not care about the rest.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from tests import test_mcp
from tests.helpers import read_records
from tests.session_fixtures import assistant_text, prompt, write_live_session, write_transcript
from tests.test_mcp import Ctx
from voice_coding_mcp.config import config_warnings, parse_config
from voice_coding_mcp.sessions import read_live_sessions

ID_TMUX = "55555555-0000-4000-8000-00000000000a"
ID_PLAIN = "55555555-0000-4000-8000-00000000000b"
UID = os.getuid()

ctx = test_mcp.ctx
"""The tool-layer fixture of test_mcp.py (fake Claude home in `ctx.home`, projects in `ctx.projects`)."""


def linux_entry(session_id: str, cwd: str, *, socket: str, **extra: Any) -> dict[str, Any]:
    """A registry entry with every key seen on the target WSL box (Claude Code 2.x, Linux)."""
    now = int(time.time() * 1000)
    return {
        "sessionId": session_id,
        "cwd": cwd,
        "name": "parser",
        "nameSince": now - 60_000,
        "nameSource": "user",
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "idle",
        "statusUpdatedAt": now - 30_000,
        "startedAt": now - 3_600_000,
        "updatedAt": now - 30_000,
        "version": "2.1.330",
        "messagingSocketPath": socket,
        "peerFeatures": ["messaging"],
        "peerProtocol": 1,
        "pidDomain": "linux",
        "procStart": "12345678",
        **extra,
    }


def seed_linux(ctx: Ctx, *, socket: str, **extra: Any) -> str:
    """One running terminal session inside tmux, in alpha-app; returns its cwd."""
    cwd = os.path.join(ctx.projects, "alpha-app")
    write_transcript(ctx.home, ID_TMUX, [prompt(cwd, "Fix the parser"), assistant_text(cwd, "Working on it.")])
    write_live_session(ctx.home, os.getpid(), linux_entry(ID_TMUX, cwd, socket=socket, tmux="fpga:@0.%3", **extra))
    return cwd


def test_reads_every_linux_registry_key_it_uses(ctx: Ctx) -> None:
    socket = f"/run/user/{UID}/cc-socks/{os.getpid()}.sock"
    cwd = seed_linux(ctx, socket=socket, status="waiting", waitingFor="permission prompt")
    live = read_live_sessions(ctx.home)[ID_TMUX]
    assert (live.socket_path, live.tmux, live.cwd, live.entrypoint, live.kind) == (
        socket,
        "fpga:@0.%3",
        cwd,
        "cli",
        "interactive",
    )
    assert (live.status, live.waiting_for, live.name) == ("waiting", "permission prompt", "parser")


async def test_list_sessions_shows_a_linux_tmux_session_as_running(ctx: Ctx) -> None:
    seed_linux(ctx, socket=f"/run/user/{UID}/cc-socks/{os.getpid()}.sock")
    assert await ctx.text("list_sessions") == "Sessions:\nparser (alpha-app, running, just now)"


async def test_list_sessions_reports_a_busy_linux_session(ctx: Ctx) -> None:
    seed_linux(ctx, socket=f"/run/user/{UID}/cc-socks/{os.getpid()}.sock", status="busy")
    assert await ctx.text("list_sessions") == "Sessions:\nparser (alpha-app, running, busy, just now)"


@pytest.mark.parametrize(
    "socket",
    [
        pytest.param(f"/run/user/{UID}/cc-socks/{os.getpid()}.sock", id="xdg-runtime-dir"),
        pytest.param(f"/tmp/cc-socks-{UID}/{os.getpid()}.sock", id="no-xdg-runtime-dir"),
    ],
)
async def test_send_message_relays_to_the_linux_socket_path_from_the_registry(ctx: Ctx, socket: str) -> None:
    seed_linux(ctx, socket=socket)
    text, is_error = await ctx.call("send_message_to_session", {"session": "parser", "message": "Run the tests."})
    assert not is_error, text
    [rec] = read_records(ctx.record)
    argv = rec["argv"]
    assert argv[argv.index("--tools") + 1] == "SendMessage"
    system = argv[argv.index("--append-system-prompt") + 1]
    assert f'"uds:{socket}"' in system
    assert "/tmp/cc-socks/" not in system, "no macOS path is assumed"
    assert "--strict-mcp-config" in argv
    assert not any("Run the tests." in a for a in argv), "the message goes on stdin only"
    assert "Run the tests." in rec["stdin"]


async def test_send_message_refuses_a_linux_session_without_a_socket(ctx: Ctx) -> None:
    cwd = os.path.join(ctx.projects, "alpha-app")
    write_transcript(ctx.home, ID_TMUX, [prompt(cwd, "Fix the parser")])
    entry = linux_entry(ID_TMUX, cwd, socket="", tmux="fpga:@0.%3")
    del entry["messagingSocketPath"]
    write_live_session(ctx.home, os.getpid(), entry)
    text, is_error = await ctx.call("send_message_to_session", {"session": "parser", "message": "Run the tests."})
    assert is_error
    assert "parser" in text
    assert read_records(ctx.record) == []


async def test_ask_session_readonly_resumes_a_linux_session(ctx: Ctx) -> None:
    seed_linux(ctx, socket=f"/run/user/{UID}/cc-socks/{os.getpid()}.sock")
    text = await ctx.text(
        "ask_session_readonly", {"session": "parser", "question": "What is left?\nRESULT Only tests."}
    )
    assert text == "Task 1 for session parser is done. Only tests."
    [rec] = read_records(ctx.record)
    argv = rec["argv"]
    assert argv[argv.index("--resume") + 1] == ID_TMUX
    assert "--fork-session" in argv


async def test_pending_input_leaves_tmux_sessions_to_the_machines_and_reports_a_plain_one(ctx: Ctx) -> None:
    """A tmux session of the hub's own user is reported through its machine (`dev`), so the hub-local
    part of pending_input skips registry entries with `tmux`; a terminal session outside tmux that
    waits is reported as "on this machine"."""
    seed_linux(
        ctx,
        socket=f"/run/user/{UID}/cc-socks/{os.getpid()}.sock",
        status="waiting",
        waitingFor="permission prompt",
    )
    text = await ctx.text("pending_input")
    assert "parser" not in text
    cwd = os.path.join(ctx.projects, "beta-site")
    write_transcript(ctx.home, ID_PLAIN, [prompt(cwd, "New site")])
    write_live_session(
        ctx.home,
        os.getppid(),
        linux_entry(
            ID_PLAIN,
            cwd,
            socket=f"/run/user/{UID}/cc-socks/{os.getppid()}.sock",
            name="site",
            status="waiting",
            waitingFor="permission prompt",
        ),
    )
    text = await ctx.text("pending_input")
    assert 'On this machine, session "site" in beta-site' in text
    assert "permission" in text
    assert "parser" not in text


def test_the_desktop_handoff_is_refused_outside_macos(tmp_path: Any) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    config = parse_config(
        {
            "publicUrl": "https://example-host.example-tailnet.ts.net",
            "stateDir": str(tmp_path / "state"),
            "projectRoots": [str(projects)],
            "remoteControl": {"handoffToDesktop": True},
        },
        home_dir="/nonexistent-home-dir",
    )
    assert any("handoffToDesktop" in w for w in config_warnings(config, platform="linux"))
    assert not any("handoffToDesktop" in w for w in config_warnings(config, platform="darwin"))
