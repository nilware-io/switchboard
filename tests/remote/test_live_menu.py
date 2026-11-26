"""Optional live test of menus on a real tmux, skipped unless SB_LIVE_TMUX=1: the dispatcher's capture
of a synthetic menu (tests/fixtures/fake_menu.py) on the normal and the alternate screen, in a tall
and a short pane, through the hub's cleaning and menu heuristic; a confirmed `remote_send_keys` that
moves the selection and chooses an option; a confirmed `remote_resize` of a short pane; a menu
taller than a 24-row alternate pane, said to be cut off and read whole after a resize.

It starts its own isolated server (`tmux -L sb-test-<random> -f /dev/null`), never the default one,
and always kills that server afterwards. No real screen of the owner is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import FIXTURES, claude_config
from tests.remote.test_live_local import IsolatedLocalTransport
from voice_coding_mcp.config import parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.menu import screen_menu
from voice_coding_mcp.remote.redact import clean_screen
from voice_coding_mcp.remote.tools import Caller, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

FAKE_MENU = str(FIXTURES / "fake_menu.py")
SESSION_ID = "77777777-0000-4000-8000-00000000000a"
OWNER = Caller(fingerprint="len=36 sha256=0a0b0c0d", name="Claude")
QUESTION = "How big should the high score table be?"
OPTIONS = (
    (1, "Small (10 entries)"),
    (2, "Medium (50 entries)"),
    (3, "Large (100 entries)"),
    (4, "Type something else"),
)
PLAIN_OPTIONS = ((1, "Small"), (2, "Medium"), (3, "Large"))
PANES = {
    "normal": ("normal", 40),
    "alt": ("alt", 40),
    "short": ("normal", 8),
    "shortalt": ("alt", 8),
    "plain": ("plain", 24),
    "tall": ("tall", 24),
}
TALL_QUESTION = "Which theme should the editor use?"

pytestmark = pytest.mark.skipif(
    os.environ.get("SB_LIVE_TMUX") != "1" or shutil.which("tmux") is None,
    reason="live tmux test: set SB_LIVE_TMUX=1 (needs tmux)",
)


def tmux(base: list[str], *args: str) -> str:
    return subprocess.run([*base, *args], check=True, timeout=10, capture_output=True, text=True).stdout


@dataclass
class Server:
    name: str
    base: list[str]
    records: dict[str, Path]
    panes: dict[str, str]
    pids: dict[str, int]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    name = f"sb-test-{secrets.token_hex(6)}"
    base = ["tmux", "-L", name, "-f", "/dev/null"]
    records = {session: tmp_path / f"{session}.chosen" for session in PANES}
    try:
        for session, (mode, rows) in PANES.items():
            # The menu program itself, no shell: nothing of the user's own shell setup runs.
            menu = [sys.executable, "-I", FAKE_MENU, mode, str(records[session])]
            tmux(base, "new-session", "-d", "-s", session, "-x", "80", "-y", str(rows), *menu)
        deadline = time.monotonic() + 10
        while not all(Path(f"{record}.ready").exists() for record in records.values()):
            assert time.monotonic() < deadline, "fake menus did not start"
            time.sleep(0.05)
        time.sleep(0.3)
        panes, pids = {}, {}
        for session in PANES:
            pane_id, pid = tmux(base, "display-message", "-p", "-t", session, "#{pane_id} #{pane_pid}").split()
            panes[session], pids[session] = pane_id, int(pid)
        yield Server(name=name, base=base, records=records, panes=panes, pids=pids)
    finally:
        subprocess.run([*base, "kill-server"], check=False, timeout=10, capture_output=True)
        socket_dir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(socket_dir, f"tmux-{os.getuid()}", name))


def wait_for_choice(record: Path) -> str:
    """The option the fake menu chose (at most 10 s)."""
    deadline = time.monotonic() + 10
    while not record.exists():
        assert time.monotonic() < deadline, "the menu chose nothing"
        time.sleep(0.05)
    return record.read_text(encoding="utf-8")


def read_lines(path: str) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def dispatcher_env() -> dict[str, str]:
    env = {"HOME": os.environ.get("HOME", "/nonexistent"), "SWITCHBOARD_REMOTE_TEST": "1"}
    if "TMUX_TMPDIR" in os.environ:
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    return env


def policy(server: Server, *extra: str) -> list[str]:
    return ["--policy-from-hub", "--sessions", ",".join(PANES), *extra, "--tmux-socket-name", server.name, "--"]


def test_the_whole_menu_is_captured_on_every_screen(server: Server) -> None:
    for session, (mode, rows) in PANES.items():
        if mode == "tall":
            continue
        captured = d.handle([*policy(server), "capture", server.panes[session], "120"], dispatcher_env())
        assert captured["ok"] is True, captured
        assert captured["alternateOn"] is (mode in ("alt", "plain")), session
        assert captured["paneHeight"] == rows
        menu = screen_menu(clean_screen(str(captured["text"])))
        assert menu is not None, session
        assert menu.numbered is (mode != "plain"), session
        assert menu.options == (OPTIONS if mode != "plain" else PLAIN_OPTIONS), session
        assert menu.selected == 1
        assert menu.question == QUESTION, session


def test_keys_move_the_selection_through_the_dispatcher(server: Server) -> None:
    pane_id, pid = server.panes["normal"], server.pids["normal"]
    screen = str(d.handle([*policy(server), "capture", pane_id, str(d.SEND_CHECK_LINES)], dispatcher_env())["text"])
    bound = d.menu_hash(screen)
    assert bound is not None
    request = ["keys", pane_id, str(pid), "none", "menu", bound, "Down", "Down", "Up", "Enter"]
    out = d.handle([*policy(server, "--allow-send", "--send-to-any-pane"), *request], dispatcher_env())
    assert out == {"protocol": 1, "ok": True, "sent": True, "keys": 4}
    record = server.records["normal"]
    assert wait_for_choice(record) == "2"
    time.sleep(0.2)
    after = d.handle([*policy(server), "capture", pane_id, "40"], dispatcher_env())
    menu = screen_menu(clean_screen(str(after["text"])))
    assert menu is not None
    assert menu.selected == 2
    # The same request again is bound to the old screen: the selection moved, so nothing is pressed.
    again = d.handle([*policy(server, "--allow-send", "--send-to-any-pane"), *request], dispatcher_env())
    assert again == {"protocol": 1, "ok": False, "error": "menu_changed"}


@dataclass
class Live:
    tools: RemoteTools
    server: Server
    registry: Path


@pytest.fixture
async def live(server: Server, tmp_path: Path) -> AsyncIterator[Live]:
    claude_home = tmp_path / "claude"
    (claude_home / "sessions").mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.mkdir()
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
                        "sessions": list(PANES),
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
    transport = IsolatedLocalTransport(server.name, config.remote.machines[0], env, slash_commands=[])
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
    ids = iter(["4172", "5283", "6394"])
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=manager,
            logger=silent_logger,
            base_env=env,
            transports={"dev": transport},
            claude_home=str(tmp_path / "hub-claude"),
            new_confirmation_id=lambda: next(ids),
        )
    )
    registry = claude_home / "sessions" / f"{os.getpid()}.json"
    try:
        yield Live(tools=tools, server=server, registry=registry)
    finally:
        await manager.shutdown("test end")


def register(live: Live, session: str) -> None:
    """The fake menu in `session` registered as a waiting Claude Code session, as Claude Code does."""
    entry = {
        "pid": os.getpid(),
        "sessionId": SESSION_ID,
        "cwd": "/home/USER/projects/game",
        "status": "waiting",
        "waitingFor": "user answer",
        "tmux": f"{session}:@0.{live.server.panes[session]}",
    }
    live.registry.write_text(json.dumps(entry), encoding="utf-8")


async def test_a_confirmed_key_sequence_chooses_an_option(live: Live) -> None:
    register(live, "alt")
    status = await live.tools.remote_status("dev", "alt", 120)
    assert not status.is_error, status.text
    assert QUESTION in status.text
    assert '2 "Medium (50 entries)"' in status.text
    assert "choose" not in status.text
    prepared = await live.tools.remote_send_keys("dev", "alt", ["down", "down", "enter"], OWNER)
    assert not prepared.is_error, prepared.text
    assert 'That should choose 3, "Large (100 entries)".' in prepared.text
    record = live.server.records["alt"]
    assert not record.exists(), "nothing is pressed before the confirm"
    done = await live.tools.remote_send_confirm("4172", False, OWNER)
    assert (done.text, done.is_error) == ("Pressed down, down and enter in alt on dev.", False)
    assert wait_for_choice(record) == "3"
    [line] = read_lines(live.tools.audit.path)
    audit = json.loads(line)
    assert (audit["event"], audit["keys"], audit["outcome"]) == ("remote_send_keys", ["Down", "Down", "Enter"], "sent")


async def test_a_confirmed_resize_makes_a_short_alternate_pane_show_the_whole_menu(live: Live) -> None:
    register(live, "shortalt")
    before = await live.tools.remote_status("dev", "shortalt", None)
    assert "only 8 rows high" in before.text
    assert "I can make it bigger" in before.text
    prepared = await live.tools.remote_resize("dev", "shortalt", 100, 30, OWNER)
    assert "It is now 80 by 8." in prepared.text
    done = await live.tools.remote_send_confirm("4172", False, OWNER)
    assert done.text.startswith("Resized the window of shortalt on dev to 100 by 30.")
    assert not done.is_error
    size = tmux(live.server.base, "display-message", "-p", "-t", "shortalt", "#{window_width} #{window_height}")
    assert size.split() == ["100", "30"]
    assert tmux(live.server.base, "show-options", "-w", "-v", "-t", "shortalt", "window-size").strip() == "manual"
    await asyncio.sleep(0.3)
    captured = d.handle([*policy(live.server), "capture", live.server.panes["shortalt"], "60"], dispatcher_env())
    assert "I can set the high score table up" in str(captured["text"]), "the line above the box is back"
    after = await live.tools.remote_status("dev", "shortalt", None)
    assert "rows high" not in after.text
    assert QUESTION in after.text
    # The undo the read-back names: the window option is unset and the window follows the global one.
    tmux(live.server.base, "set-option", "-w", "-u", "-t", "shortalt", "window-size")
    assert tmux(live.server.base, "show-options", "-w", "-v", "-t", "shortalt", "window-size").strip() == ""


async def test_a_menu_taller_than_a_24_row_alternate_pane_is_said_to_be_cut_and_resized(live: Live) -> None:
    """12 options with a description line each (31 lines) on the alternate screen of a 24-row pane:
    option 1 is off the top, so no menu is read; the answer says it is cut and offers a resize, keys
    are refused with the resize named, and after a resize to 40 rows the whole menu is read."""
    register(live, "tall")
    pane_id = live.server.panes["tall"]
    captured = d.handle([*policy(live.server), "capture", pane_id, "120"], dispatcher_env())
    assert (captured["alternateOn"], captured["paneHeight"]) == (True, 24)
    screen = clean_screen(str(captured["text"]))
    assert screen_menu(screen) is None
    assert d.shows_picker(screen) is True
    before = await live.tools.remote_status("dev", "tall", None)
    assert "the menu on it is cut off" in before.text
    assert "I can make it bigger" in before.text
    refused = await live.tools.remote_send_keys("dev", "tall", ["enter"], OWNER)
    assert refused.is_error
    assert "I can make the pane taller" in refused.text
    prepared = await live.tools.remote_resize("dev", "tall", 80, 40, OWNER)
    assert not prepared.is_error, prepared.text
    done = await live.tools.remote_send_confirm("4172", False, OWNER)
    assert not done.is_error, done.text
    await asyncio.sleep(0.3)
    after = await live.tools.remote_status("dev", "tall", None)
    assert TALL_QUESTION in after.text
    assert '1 "Theme 1"' in after.text
    assert "cut off" not in after.text
    assert not live.server.records["tall"].exists(), "nothing was pressed"
