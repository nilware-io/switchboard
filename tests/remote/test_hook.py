"""switchboard-hook (`remote/hook.py`, deployed as `deploy/remote/switchboard-hook`): the Claude Code
hook with real-shaped payloads (the hooks reference's examples), the spool's bounds and modes, never
blocking Claude Code, the settings installer on temporary homes, and the notify_owner MCP server.
Nothing here touches the real `~/.claude` or runs the real `claude`."""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_cli import run_cli
from voice_coding_mcp.remote import hook as h

REPO = Path(__file__).resolve().parents[2]
HOOK = Path(h.__file__)
HOOK_SOURCE = HOOK.read_text(encoding="utf-8")
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
T0 = 1_795_000_000_000
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def payload(event: str, **fields: Any) -> dict[str, Any]:
    """The common fields of the hooks reference, plus the event's own."""
    return {
        "session_id": SESSION,
        "transcript_path": "/home/user/.claude/projects/x/00893aaf-19fa-41d2-8238-13269b9b3ca0.jsonl",
        "cwd": "/home/user/projects/fpgadc",
        "permission_mode": "default",
        "hook_event_name": event,
        **fields,
    }


NOTIFICATION = payload(
    "Notification", message="Claude needs your permission to use Bash", notification_type="permission_prompt"
)
STOP = payload(
    "Stop",
    stop_hook_active=False,
    last_assistant_message=f"I've completed the refactoring. Token {SECRET}",
    background_tasks=[],
    session_crons=[],
)
STOP_FAILURE = payload(
    "StopFailure",
    error="rate_limit",
    error_details="429 Too Many Requests",
    last_assistant_message="API Error: Rate limit reached",
)
PR_CREATE = payload(
    "PostToolUse",
    tool_name="Bash",
    tool_input={"command": "gh pr create --base main --title 'x' --body-file body.md", "description": "Open the PR"},
    tool_response={
        "stdout": "Creating pull request\nhttps://github.com/nilware-io/switchboard/pull/7\n",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
    },
    tool_use_id="toolu_01ABC123",
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "machine-home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    (home / ".claude" / "sessions" / "4242.json").write_text(
        json.dumps({"pid": 4242, "sessionId": SESSION, "name": "fix the \x1b[31mtests\u202e"}), encoding="utf-8"
    )
    return {"HOME": str(home), "TMUX_PANE": "%3", "PATH": "/usr/bin:/bin"}


def spool(env: dict[str, str]) -> Path:
    return Path(h.spool_dir(env))


def events(env: dict[str, str]) -> list[dict[str, Any]]:
    directory = spool(env)
    if not directory.exists():
        return []
    return [json.loads((directory / name).read_text()) for name in h.spooled(str(directory))]


def run_payload(env: dict[str, str], data: object, now_ms: int = T0) -> int:
    read, write = os.pipe()
    os.write(write, json.dumps(data).encode())
    os.close(write)
    try:
        return h.run_hook(env, read, now_ms)
    finally:
        os.close(read)


# --- events from hook payloads -----------------------------------------------------------------


def test_a_permission_prompt_is_approval_needed(env: dict[str, str]) -> None:
    assert run_payload(env, NOTIFICATION) == 0
    [event] = events(env)
    assert event == {
        "v": 1,
        "kind": "approval_needed",
        "time": T0,
        "sessionId": SESSION,
        "sessionName": "fix the [31mtests",
        "project": "fpgadc",
        "pane": "%3",
        "message": "Claude needs your permission to use Bash",
        "url": None,
        "level": None,
    }


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("idle_prompt", "question"), ("elicitation_dialog", "question"), ("agent_needs_input", "question")],
)
def test_questions(env: dict[str, str], kind: str, expected: str) -> None:
    run_payload(env, {**NOTIFICATION, "notification_type": kind, "message": "Claude is waiting for your input"})
    assert [e["kind"] for e in events(env)] == [expected]


@pytest.mark.parametrize("kind", ["auth_success", "elicitation_complete", "plugin_notification", None])
def test_other_notifications_are_ignored(env: dict[str, str], kind: str | None) -> None:
    run_payload(env, {**NOTIFICATION, "notification_type": kind})
    assert events(env) == []


def test_stop_is_finished_without_any_assistant_text(env: dict[str, str]) -> None:
    run_payload(env, STOP)
    [event] = events(env)
    assert event["kind"] == "finished"
    assert event["message"] is None
    raw = "".join(p.read_text() for p in spool(env).iterdir())
    assert "refactoring" not in raw
    assert SECRET not in raw
    assert "transcript" not in raw


def test_stop_while_a_stop_hook_continues_and_subagents_are_ignored(env: dict[str, str]) -> None:
    run_payload(env, {**STOP, "stop_hook_active": True})
    run_payload(env, {**STOP, "agent_id": "agent-1", "agent_type": "Explore"})
    assert events(env) == []


def test_stop_failure_keeps_only_the_error_code(env: dict[str, str]) -> None:
    run_payload(env, STOP_FAILURE, T0)
    run_payload(env, {**STOP_FAILURE, "error": "something new " + SECRET}, T0 + 1)
    assert [(e["kind"], e["message"]) for e in events(env)] == [("failed", "rate_limit"), ("failed", "unknown")]
    raw = "".join(p.read_text() for p in spool(env).iterdir())
    assert "429" not in raw
    assert SECRET not in raw


def test_gh_pr_create_reports_the_validated_url(env: dict[str, str]) -> None:
    run_payload(env, PR_CREATE)
    [event] = events(env)
    assert event["kind"] == "pr_opened"
    assert event["url"] == "https://github.com/nilware-io/switchboard/pull/7"
    assert event["message"] == "gh pr create"


def test_gh_pr_ready_builds_the_url_from_the_reference(env: dict[str, str]) -> None:
    ready = {
        **PR_CREATE,
        "tool_input": {"command": "cd repo && gh pr ready 7"},
        "tool_response": {"stdout": "✓ Pull request nilware-io/switchboard#7 is marked as ready for review\n"},
    }
    run_payload(env, ready)
    assert [e["url"] for e in events(env)] == ["https://github.com/nilware-io/switchboard/pull/7"]


@pytest.mark.parametrize(
    ("command", "stdout"),
    [
        ("gh pr view 7", "https://github.com/o/r/pull/7"),
        ("echo gh-pr create", "https://github.com/o/r/pull/7"),
        ("gh pr create", "https://github.com.evil.example/o/r/pull/7"),
        ("gh pr create", "https://github.com/o/r/pull/7x"),
        ("gh pr create", "http://github.com/o/r/pull/7"),
        ("gh pr create", "no url here"),
    ],
)
def test_other_commands_and_bad_urls_report_nothing(env: dict[str, str], command: str, stdout: str) -> None:
    run_payload(env, {**PR_CREATE, "tool_input": {"command": command}, "tool_response": {"stdout": stdout}})
    assert events(env) == []


def test_other_tools_and_unknown_events_are_ignored(env: dict[str, str]) -> None:
    run_payload(env, {**PR_CREATE, "tool_name": "Write"})
    run_payload(env, payload("SessionEnd", reason="other"))
    run_payload(env, payload("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr create"}))
    run_payload(env, ["not", "an", "object"])
    assert events(env) == []


def test_messages_are_cleaned_redacted_and_bounded(env: dict[str, str]) -> None:
    run_payload(env, {**NOTIFICATION, "message": "use \x1b]8;;x\x07 " + SECRET + " now\n" + "y" * 500})
    [event] = events(env)
    assert SECRET not in event["message"]
    assert "[secret]" in event["message"]
    assert "\x1b" not in event["message"]
    assert "\n" not in event["message"]
    assert len(event["message"]) <= h.MAX_MESSAGE


def test_bad_ids_and_panes_are_dropped(env: dict[str, str]) -> None:
    run_payload(env, {**NOTIFICATION, "session_id": "../../etc", "cwd": 7}, T0)
    env["TMUX_PANE"] = "%3; rm -rf /"
    run_payload(env, NOTIFICATION, T0 + 1)
    first, second = events(env)
    assert first["sessionId"] is None
    assert first["project"] is None
    assert first["sessionName"] is None
    assert second["pane"] is None


# --- the spool ---------------------------------------------------------------------------------


def test_spool_modes(env: dict[str, str]) -> None:
    run_payload(env, NOTIFICATION)
    directory = spool(env)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    [name] = os.listdir(directory)
    assert h.EVENT_FILE_RE.fullmatch(name)
    assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_the_spool_is_bounded(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "MAX_SPOOL_FILES", 3)
    for index in range(5):
        run_payload(env, NOTIFICATION, T0 + index)
    assert len(events(env)) == 3


def test_an_oversized_event_is_not_written(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "MAX_EVENT_BYTES", 100)
    run_payload(env, NOTIFICATION)
    assert events(env) == []


def test_a_spool_others_can_enter_is_not_used(env: dict[str, str]) -> None:
    directory = spool(env)
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    run_payload(env, NOTIFICATION)
    assert os.listdir(directory) == []


def test_a_symlinked_spool_is_not_used(env: dict[str, str], tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    directory = spool(env)
    directory.parent.mkdir(parents=True, mode=0o700)
    directory.symlink_to(target)
    run_payload(env, NOTIFICATION)
    assert os.listdir(target) == []


def test_an_unwritable_disk_or_broken_input_never_fails(env: dict[str, str]) -> None:
    Path(env["HOME"], ".local").write_text("a file where the directory should be")
    assert run_payload(env, NOTIFICATION) == 0
    read, write = os.pipe()
    os.write(write, b"\xff{not json")
    os.close(write)
    assert h.run_hook(env, read) == 0
    os.close(read)


# --- never blocking Claude Code ----------------------------------------------------------------


def run_script(args: list[str], env: dict[str, str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, "-I", str(HOOK), *args], env=env, capture_output=True, check=False, **kwargs)


def test_the_script_prints_nothing_and_exits_0(env: dict[str, str]) -> None:
    done = run_script(["hook"], env, input=json.dumps(NOTIFICATION).encode(), timeout=20)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert len(events(env)) == 1


def test_stdin_that_never_ends_gives_up_quickly(env: dict[str, str]) -> None:
    started = time.monotonic()
    with subprocess.Popen(
        [sys.executable, "-I", str(HOOK), "hook"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdin is not None
        process.stdin.write(b'{"hook_event_name": "Notification"')
        process.stdin.flush()
        code = process.wait(15)
        assert process.stdout is not None
        assert process.stdout.read() == b""
        process.stdin.close()
    assert code == 0
    assert time.monotonic() - started < h.HARD_SECONDS + 3
    assert events(env) == []


def test_oversized_input_is_dropped(env: dict[str, str]) -> None:
    big = {**PR_CREATE, "tool_response": {"stdout": "x" * (h.MAX_STDIN_BYTES + 10)}}
    done = run_script(["hook"], env, input=json.dumps(big).encode(), timeout=30)
    assert done.returncode == 0
    assert events(env) == []


def test_unknown_commands_exit_2(env: dict[str, str]) -> None:
    assert run_script(["nope"], env, timeout=20).returncode == 2
    assert run_script(["version"], env, timeout=20).stdout.decode().strip() == h.VERSION


# --- installer ---------------------------------------------------------------------------------


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """A `claude` stand-in that records its argv (never the real CLI)."""
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    log = tmp_path / "claude-calls.jsonl"
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\ncase "$2" in remove) exit 1;; esac\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class Out:
    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)


OTHER_HOOKS = {
    "theme": "dark",
    "permissions": {"allow": ["Bash(npm test)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/guard.sh"}]}],
    },
}


def install(env: dict[str, str], *args: str, uninstall: bool = False) -> tuple[int, str, str]:
    out, err = Out(), Out()
    code = h.run_install(
        ["--python", "/usr/bin/python3", "--script", "/home/u/.local/bin/switchboard-hook", *args],
        env,
        out,
        err,
        uninstall=uninstall,
    )
    return code, out.text, err.text


def settings_path(env: dict[str, str]) -> Path:
    return Path(env["HOME"], ".claude", "settings.json")


def test_install_merges_keeps_other_hooks_and_backs_up(env: dict[str, str], fake_claude: Path, tmp_path: Path) -> None:
    path = settings_path(env)
    path.write_text(json.dumps(OTHER_HOOKS), encoding="utf-8")
    path.chmod(0o640)
    code, out, err = install(env, "--claude", str(fake_claude))
    assert (code, err) == (0, "")
    data = json.loads(path.read_text())
    assert data["theme"] == "dark"
    assert data["permissions"] == {"allow": ["Bash(npm test)"]}
    assert data["hooks"]["PreToolUse"] == OTHER_HOOKS["hooks"]["PreToolUse"]  # type: ignore[index]
    command = "/usr/bin/python3 -I /home/u/.local/bin/switchboard-hook hook"
    ours = {"type": "command", "command": command, "timeout": 10, "async": True}
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "notify-send done"}]}, {"hooks": [ours]}]
    assert data["hooks"]["StopFailure"] == [{"hooks": [ours]}]
    assert data["hooks"]["PostToolUse"] == [{"matcher": "Bash", "hooks": [ours]}]
    assert data["hooks"]["Notification"] == [
        {"matcher": "permission_prompt|idle_prompt|elicitation_dialog|agent_needs_input", "hooks": [ours]}
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    [backup] = [p for p in path.parent.iterdir() if ".switchboard-backup-" in p.name]
    assert json.loads(backup.read_text()) == OTHER_HOOKS
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "+" in out
    assert "backup:" in out
    calls = (tmp_path / "claude-calls.jsonl").read_text().splitlines()
    assert calls == [
        "mcp remove --scope user switchboard-notify",
        "mcp add --scope user switchboard-notify -- /usr/bin/python3 -I /home/u/.local/bin/switchboard-hook mcp",
    ]


def test_install_is_idempotent(env: dict[str, str]) -> None:
    path = settings_path(env)
    path.write_text(json.dumps(OTHER_HOOKS), encoding="utf-8")
    install(env, "--no-mcp")
    first = path.read_text()
    code, out, _ = install(env, "--no-mcp")
    assert code == 0
    assert path.read_text() == first
    assert "no change" in out
    assert len([p for p in path.parent.iterdir() if ".switchboard-backup-" in p.name]) == 1


def test_a_new_install_path_replaces_the_old_entries(env: dict[str, str]) -> None:
    install(env, "--no-mcp")
    out, err = Out(), Out()
    h.run_install(
        ["--python", "/usr/bin/python3", "--script", "/opt/sb/switchboard-hook", "--no-mcp"],
        env,
        out,
        err,
        uninstall=False,
    )
    data = json.loads(settings_path(env).read_text())
    for event in h.HOOK_EVENTS:
        [group] = data["hooks"][event]
        assert group["hooks"][0]["command"] == "/usr/bin/python3 -I /opt/sb/switchboard-hook hook"


def test_dry_run_writes_nothing_and_runs_nothing(env: dict[str, str], fake_claude: Path, tmp_path: Path) -> None:
    path = settings_path(env)
    path.write_text(json.dumps(OTHER_HOOKS), encoding="utf-8")
    code, out, _ = install(env, "--dry-run", "--claude", str(fake_claude))
    assert code == 0
    assert json.loads(path.read_text()) == OTHER_HOOKS
    assert "dry run" in out
    assert "switchboard-hook hook" in out
    assert not (tmp_path / "claude-calls.jsonl").exists()
    assert not [p for p in path.parent.iterdir() if "switchboard" in p.name]


def test_uninstall_removes_only_ours(env: dict[str, str], fake_claude: Path, tmp_path: Path) -> None:
    path = settings_path(env)
    path.write_text(json.dumps(OTHER_HOOKS), encoding="utf-8")
    install(env, "--no-mcp", "--allow-tool")
    assert h.ALLOW_RULE in json.loads(path.read_text())["permissions"]["allow"]
    code, _, _ = install(env, "--claude", str(fake_claude), uninstall=True)
    assert code == 0
    assert json.loads(path.read_text()) == OTHER_HOOKS
    assert (tmp_path / "claude-calls.jsonl").read_text().splitlines() == ["mcp remove --scope user switchboard-notify"]


def test_install_without_a_settings_file_creates_it_0600(env: dict[str, str]) -> None:
    path = settings_path(env)
    assert install(env, "--no-mcp")[0] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(json.loads(path.read_text())) == {"hooks"}


@pytest.mark.parametrize(
    "content", ["{not json", "[1, 2]", json.dumps({"hooks": []}), json.dumps({"hooks": {"Stop": {"a": 1}}})]
)
def test_a_settings_file_it_cannot_read_is_left_alone(env: dict[str, str], content: str) -> None:
    path = settings_path(env)
    path.write_text(content, encoding="utf-8")
    code, _, err = install(env, "--no-mcp")
    assert code == 1
    assert "switchboard-hook:" in err
    assert path.read_text() == content


def test_paths_outside_the_charset_and_unknown_options_are_refused(env: dict[str, str]) -> None:
    out, err = Out(), Out()
    assert h.run_install(["--python", "/usr/bin/python3; rm -rf ~", "--no-mcp"], env, out, err, uninstall=False) == 1
    assert h.run_install(["--force"], env, out, err, uninstall=False) == 1
    assert not settings_path(env).exists()


def test_without_claude_on_path_the_command_is_printed(env: dict[str, str]) -> None:
    env["PATH"] = "/nonexistent"
    code, out, _ = install(env)
    assert code == 0
    assert "claude mcp add --scope user switchboard-notify -- /usr/bin/python3 -I" in out


# --- notify_owner ------------------------------------------------------------------------------


class Mcp:
    """The `mcp` command as a child process, speaking newline-delimited JSON-RPC."""

    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(HOOK), "mcp"],
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.next_id = 0

    def send(self, data: object) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        assert self.process.stdout is not None
        answer = json.loads(self.process.stdout.readline())
        assert answer["id"] == self.next_id
        return dict(answer)

    def note(self, **arguments: Any) -> dict[str, Any]:
        return self.call("tools/call", {"name": "notify_owner", "arguments": arguments})["result"]  # type: ignore[no-any-return]

    def close(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        code = self.process.wait(15)
        for stream in (self.process.stdout, self.process.stderr):
            assert stream is not None
            stream.close()
        return code


@pytest.fixture
def mcp(env: dict[str, str], tmp_path: Path) -> Any:
    project = tmp_path / "projects" / "fpgadc"
    project.mkdir(parents=True)
    server = Mcp(env, project)
    yield server
    server.close()


def test_handshake_and_tool_list(mcp: Mcp) -> None:
    init = mcp.call(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2"}},
    )["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "switchboard-notify"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert mcp.call("ping")["result"] == {}
    [tool] = mcp.call("tools/list")["result"]["tools"]
    assert tool["name"] == "notify_owner"
    assert tool["inputSchema"]["required"] == ["message"]
    assert mcp.call("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == h.MCP_PROTOCOLS[0]
    assert mcp.call("resources/list")["error"]["code"] == -32601


def test_a_note_is_spooled_as_owner_note(mcp: Mcp, env: dict[str, str]) -> None:
    result = mcp.note(message=f"PR ready, token {SECRET}", level="urgent", url="https://github.com/o/r/pull/3")
    assert result["isError"] is False
    [event] = events(env)
    assert event["kind"] == "owner_note"
    assert event["level"] == "urgent"
    assert event["url"] == "https://github.com/o/r/pull/3"
    assert event["project"] == "fpgadc"
    assert SECRET not in event["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"message": "   "},
        {"message": "x", "level": "panic"},
        {"message": "x", "url": "https://evil.example/login"},
        {"message": "x", "url": "javascript:alert(1)"},
        {"message": "x", "extra": 1},
    ],
)
def test_bad_arguments_are_tool_errors(mcp: Mcp, env: dict[str, str], arguments: dict[str, Any]) -> None:
    assert mcp.note(**arguments)["isError"] is True
    assert events(env) == []


def test_notes_are_rate_limited_per_session(mcp: Mcp, env: dict[str, str]) -> None:
    results = [mcp.note(message=f"note {i}")["isError"] for i in range(h.NOTES_PER_SESSION_HOUR + 2)]
    assert results == [False] * h.NOTES_PER_SESSION_HOUR + [True, True]
    assert len(events(env)) == h.NOTES_PER_SESSION_HOUR


def test_bad_json_and_requests_answer_errors(mcp: Mcp) -> None:
    assert mcp.process.stdin is not None
    assert mcp.process.stdout is not None
    mcp.process.stdin.write(b"{nope\n")
    mcp.process.stdin.flush()
    assert json.loads(mcp.process.stdout.readline())["error"]["code"] == -32700
    assert mcp.call("tools/call", {"name": "rm_rf"})["error"]["code"] == -32602


def test_machine_wide_limit(env: dict[str, str]) -> None:
    now = time.time()
    taken = [h._take_note_slot(env, f"session-{i}", now) for i in range(h.NOTES_PER_MACHINE_HOUR + 1)]
    assert taken == [True] * h.NOTES_PER_MACHINE_HOUR + [False]
    later = now + h.HOUR_SECONDS + 1
    assert h._take_note_slot(env, "session-x", later)


def test_the_session_of_the_mcp_process_comes_from_the_registry(env: dict[str, str]) -> None:
    assert h.session_of_process(env) == (None, None)
    other = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    parent = os.getppid()
    Path(env["HOME"], ".claude", "sessions", f"{parent}.json").write_text(
        json.dumps({"pid": parent, "sessionId": other, "name": "notes"}), encoding="utf-8"
    )
    assert h.session_of_process(env) == (other, "notes")
    assert h.session_name(env, SESSION) == "fix the [31mtests"


# --- the deployed file -------------------------------------------------------------------------


def test_the_deploy_copy_is_byte_identical_executable_python_3_8_and_stdlib_only() -> None:
    deployed = REPO / "deploy" / "remote" / "switchboard-hook"
    assert deployed.read_bytes() == HOOK.read_bytes()
    assert os.access(deployed, os.X_OK)
    source = HOOK.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env python3\n")
    tree = ast.parse(source, feature_version=(3, 8))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names)


async def test_the_cli_prints_the_script() -> None:
    code, io = await run_cli("remote-hook", "--config", "/nonexistent/config.json")
    assert code == 0
    assert io.stdout == HOOK_SOURCE
