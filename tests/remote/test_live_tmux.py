"""Optional: the dispatcher against a real tmux, skipped unless SB_LIVE_TMUX=1.

It starts its own isolated server (`tmux -L sb-test-<random> -f /dev/null`), never the default
one, and always kills that server afterwards.
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
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import FIXTURES
from voice_coding_mcp.remote import dispatcher as d

FAKE_TUI = str(FIXTURES / "fake_tui.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("SB_LIVE_TMUX") != "1" or shutil.which("tmux") is None,
    reason="live tmux test: set SB_LIVE_TMUX=1 (needs tmux)",
)


@pytest.fixture
def socket_name() -> Iterator[str]:
    name = f"sb-test-{secrets.token_hex(6)}"
    base = ["tmux", "-L", name, "-f", "/dev/null"]
    try:
        subprocess.run([*base, "new-session", "-d", "-s", "fpga", "-x", "80", "-y", "20"], check=True, timeout=10)
        subprocess.run([*base, "new-session", "-d", "-s", "private"], check=True, timeout=10)
        subprocess.run(
            [*base, "send-keys", "-t", "fpga", "printf 'hello from %s\\n' live-test", "Enter"], check=True, timeout=10
        )
        time.sleep(0.5)
        yield name
    finally:
        subprocess.run([*base, "kill-server"], check=False, timeout=10, capture_output=True)
        socket_dir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(socket_dir, f"tmux-{os.getuid()}", name))


def test_list_and_capture_on_an_isolated_tmux_server(socket_name: str) -> None:
    env = {"HOME": os.environ.get("HOME", "/nonexistent"), "SWITCHBOARD_REMOTE_TEST": "1"}
    if "TMUX_TMPDIR" in os.environ:
        # The fixture's tmux used it, so the dispatcher must look in the same socket directory.
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    argv = ["--policy-from-hub", "--sessions", "fpga", "--tmux-socket-name", socket_name, "--", "list"]
    listed = d.handle(argv, env)
    assert listed["ok"] is True, listed
    panes = listed["panes"]
    assert isinstance(panes, list)
    assert [p["session"] for p in panes] == ["fpga"]
    pane_id = panes[0]["paneId"]
    argv = [*argv[:-1], "capture", pane_id, "20"]
    captured = d.handle(argv, env)
    assert captured["ok"] is True, captured
    assert "hello from live-test" in str(captured["text"])


def test_send_types_one_bracketed_paste_then_enter_on_an_isolated_tmux_server(socket_name: str, tmp_path: Path) -> None:
    """Real tmux behaviour the fakes only model: `load-buffer -` from stdin, `paste-buffer -p -d` as
    one bracketed paste into a program that asked for it, the buffer deleted, Enter after it."""
    base = ["tmux", "-L", socket_name, "-f", "/dev/null"]
    record = tmp_path / "received.bin"
    subprocess.run(
        [*base, "new-window", "-d", "-t", "fpga", "-n", "tui", sys.executable, "-I", FAKE_TUI, str(record)],
        check=True,
        timeout=10,
    )
    deadline = time.monotonic() + 10
    while not Path(f"{record}.ready").exists():
        assert time.monotonic() < deadline, "fake TUI did not start"
        time.sleep(0.05)
    time.sleep(0.3)  # tmux has seen the bracketed paste switch
    env = {"HOME": str(tmp_path), "SWITCHBOARD_REMOTE_TEST": "1"}
    if "TMUX_TMPDIR" in os.environ:
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    claude_home = tmp_path / "claude"
    (claude_home / "sessions").mkdir(parents=True)
    policy = [
        "--policy-from-hub",
        "--sessions",
        "fpga",
        "--allow-send",
        "--claude-home",
        str(claude_home),
        "--tmux-socket-name",
        socket_name,
        "--",
    ]
    listed = d.handle([*policy, "list"], env)
    panes = listed["panes"]
    assert isinstance(panes, list)
    tui = next(p for p in panes if p["windowName"] == "tui")
    session_id = "44444444-0000-4000-8000-00000000000a"
    (claude_home / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": session_id, "status": "idle", "tmux": f"fpga:@9.{tui['paneId']}"}),
        encoding="utf-8",
    )
    text = "line one\nline two with 'quotes' $(not run) C-c Enter"
    out = d.handle([*policy, "send", tui["paneId"], str(tui["panePid"]), session_id, "message"], env, text.encode())
    assert out == {"protocol": 1, "ok": True, "sent": True, "bytes": len(text.encode()), "enter": True}, out
    deadline = time.monotonic() + 10
    while not (record.exists() and record.read_bytes().endswith(b"\x1b[201~\r")):
        assert time.monotonic() < deadline, record.read_bytes() if record.exists() else "nothing received"
        time.sleep(0.05)
    received = record.read_bytes()
    # paste-buffer turns each LF into CR (its default separator) inside the one bracketed block.
    assert received == b"\x1b[200~line one\rline two with 'quotes' $(not run) C-c Enter\x1b[201~\r", received
    buffers = subprocess.run([*base, "list-buffers"], capture_output=True, text=True, check=False, timeout=10)
    assert "sb-" not in buffers.stdout, "the send buffer is deleted"
