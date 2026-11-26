"""Redaction of terminal text, name cleaning, and spoken target resolution."""

from __future__ import annotations

from typing import Any

import pytest

from voice_coding_mcp.projects import MatchOk
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.inventory import MachineView, resolve_machine
from voice_coding_mcp.remote.protocol import ClaudeInfo, PaneInfo
from voice_coding_mcp.remote.redact import clean_name, clean_screen, redact, strip_terminal
from voice_coding_mcp.remote.speech import machine_line
from voice_coding_mcp.remote.targets import Ambiguous, NoTarget, NotFound, Resolved, resolve_target

SECRETS = [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----",
    "ghp_" + "a1" * 15,
    "github_pat_" + "B2" * 20,
    "sk-ant-api03-" + "x9" * 20,
    "sk-" + "Ab3" * 10,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "xoxb-1234567890-abcdefghij",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbA==",
    "0123456789abcdef0123456789abcdef01234567",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_replaced(secret: str) -> None:
    text = f"before {secret} after"
    out = redact(text)
    assert "[secret]" in out
    assert secret not in out
    assert out.startswith("before")
    assert out.endswith("after")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=hunter2 ok", "password=[secret] ok"),
        ("DB_PASSWORD: 'a b c' ok", "DB_PASSWORD: [secret] ok"),
        ('"api_key": "abc"', '"api_key": [secret]'),
        ("export GITHUB_TOKEN=abc", "export GITHUB_TOKEN=[secret]"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "aws_secret_access_key = [secret]"),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: [secret]"),
        ("curl -H 'Bearer abcdefgh1234'", "curl -H 'Bearer [secret]'"),
        ("git clone https://bob:pa55@example.com/r.git", "git clone https://[secret]@example.com/r.git"),
        ("ssh-ed25519 " + "A" * 68 + " me@host", "ssh-ed25519 [secret] me@host"),
    ],
)
def test_keyed_values(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "commit 0123456789abcdef0123456789abcdef01234567",
        "All 41 tests passed in 3.2 s",
        "src/voice_coding_mcp/remote/dispatcher_v2_module",
        "a-very-long-hyphenated-name-without-digits-at-all",
        "Running cargo build --release",
    ],
)
def test_ordinary_text_stays(text: str) -> None:
    assert redact(text) == text


def test_terminal_sequences_and_controls_are_stripped() -> None:
    raw = "\x1b[1;31mred\x1b[0m \x1b]0;title\x07\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ \x9b2Jc1\r\nnext\x07\x00\tTab\u202eRTL\u200b"
    assert strip_terminal(raw) == "red link c1\nnext\tTabRTL"


def test_clean_screen_does_both() -> None:
    assert clean_screen("\x1b[32mtoken=abc\x1b[0m") == "token=[secret]"


def test_clean_name() -> None:
    assert clean_name("  my\n\tsession\x1b[31m  ") == "my session"
    assert clean_name("x" * 100) == "x" * 40
    assert clean_name(None) == ""
    assert clean_name("a\u2066b\u200dc") == "abc"


# --- targets ---------------------------------------------------------------------------------


def make_pane(
    session: str,
    pane_id: str,
    *,
    window: int = 0,
    pane_index: int = 0,
    claude: dict[str, Any] | None = None,
    **keys: Any,
) -> PaneInfo:
    return PaneInfo.model_validate(
        {
            "session": session,
            "paneId": pane_id,
            "panePid": int(pane_id[1:]) + 100,
            "windowIndex": window,
            "windowName": keys.pop("window_name", f"w{window}"),
            "paneIndex": pane_index,
            "claude": claude,
            **keys,
        }
    )


MACHINE = MachineConfig(name="dev", transport="local", sessions=["*"])


def view(*panes: PaneInfo) -> MachineView:
    return MachineView(machine=MACHINE, panes=tuple(panes))


CC = {"sessionId": "s", "status": "busy", "name": "parser fix"}


def test_session_names_fuzzy_and_literal() -> None:
    v = view(
        make_pane("fpga", "%1", claude=CC),
        make_pane("fpga", "%2", window=1, window_name="tests"),
        make_pane("notes", "%3"),
        make_pane("work-api", "%4", claude={"name": "billing"}),
    )
    picked = resolve_target(v, "fpga")
    assert isinstance(picked, Resolved)
    # The session's only Claude Code pane is named by its session alone; another pane by its window.
    assert (picked.pane.pane_id, picked.windows, picked.name_window) == ("%1", 2, False)
    window = resolve_target(v, "fpga:1")
    assert isinstance(window, Resolved)
    assert (window.pane.pane_id, window.name_window) == ("%2", True)
    named = resolve_target(v, "fpga:tests.0")
    assert isinstance(named, Resolved)
    assert named.pane.pane_id == "%2"
    assert isinstance(resolve_target(v, "fpga:7"), NotFound)
    fuzzy = resolve_target(v, "work api")
    assert isinstance(fuzzy, Resolved)
    assert fuzzy.pane.pane_id == "%4"
    by_claude_name = resolve_target(v, "parser fix")
    assert isinstance(by_claude_name, Resolved)
    assert by_claude_name.pane.pane_id == "%1"
    assert isinstance(resolve_target(v, "billing"), Resolved)


def test_several_claude_panes_in_a_session_are_ambiguous() -> None:
    v = view(make_pane("fpga", "%1", claude=CC), make_pane("fpga", "%2", window=1, claude=CC))
    result = resolve_target(v, "fpga")
    assert isinstance(result, Ambiguous)
    assert [p.pane_id for p in result.panes] == ["%1", "%2"]
    picked = resolve_target(v, "fpga:1")
    assert isinstance(picked, Resolved)
    # One of several Claude Code panes: the label names its window.
    assert (picked.pane.pane_id, picked.name_window) == ("%2", True)


def test_session_without_claude_picks_the_active_pane() -> None:
    v = view(make_pane("notes", "%1", active=False), make_pane("notes", "%2", pane_index=1, active=True))
    result = resolve_target(v, "notes")
    assert isinstance(result, Resolved)
    assert result.pane.pane_id == "%2"


@pytest.mark.parametrize("target", ["../etc", "/tmp/x", "fpga; rm -rf", "$(id)", "fp*", "a`b`", "x\ny"])
def test_paths_and_shell_metacharacters_never_match(target: str) -> None:
    v = view(make_pane("fpga", "%1", claude=CC))
    result = resolve_target(v, target)
    assert isinstance(result, NotFound)
    assert result.sessions == ["fpga"]


def test_no_target() -> None:
    one = view(make_pane("fpga", "%1", claude=CC), make_pane("notes", "%2"))
    resolved = resolve_target(one, None)
    assert isinstance(resolved, Resolved)
    assert resolved.pane.pane_id == "%1"
    two = view(make_pane("fpga", "%1", claude=CC), make_pane("notes", "%2", claude=CC))
    assert resolve_target(two, " ") == NoTarget(sessions=["fpga", "notes"])


def test_ambiguous_session_names() -> None:
    v = view(make_pane("work-api", "%1"), make_pane("work-web", "%2"))
    result = resolve_target(v, "work")
    assert isinstance(result, Ambiguous)
    assert result.sessions == ["work-api", "work-web"]


def test_a_cut_pane_list_is_spoken() -> None:
    cut = " It has more panes than I can list, so some are missing."
    full = view(make_pane("notes", "%1"))
    assert machine_line(full) == "dev: 1 session. notes, no Claude Code."
    truncated = MachineView(machine=MACHINE, panes=full.panes, truncated=True)
    assert machine_line(truncated) == "dev: 1 session. notes, no Claude Code." + cut
    assert machine_line(MachineView(machine=MACHINE, truncated=True)) == "dev: no allowed tmux sessions." + cut


def test_machine_resolution() -> None:
    dev = MachineConfig(name="dev", transport="local", sessions=["a"])
    build = MachineConfig(name="buildbox", host="example-host", user="bot", identity_file="/k", sessions=["a"])
    found = resolve_machine([dev, build], "build box")
    assert isinstance(found, MatchOk)
    assert found.item.name == "buildbox"
    assert not isinstance(resolve_machine([dev, build], None), MatchOk)
    only = resolve_machine([dev], None)
    assert isinstance(only, MatchOk)
    assert isinstance(ClaudeInfo(), ClaudeInfo)
