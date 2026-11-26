"""Optional: the dispatcher against a real tmux, skipped unless SB_LIVE_TMUX=1.

It starts its own isolated server (`tmux -L sb-test-<random> -f /dev/null`), never the default
one, and always kills that server afterwards.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from voice_coding_mcp.remote import dispatcher as d

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
