"""The dispatcher (`remote/dispatcher.py`, deployed as `deploy/remote/switchboard-remote`): request
parsing, policy sources, allow-list, registry join, capture, and its install-time properties."""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import DISPATCHER, FAKE_TMUX, SESSION_A, SESSION_B, RemoteWorld, pane
from voice_coding_mcp.remote import dispatcher as d

REPO = Path(__file__).resolve().parents[2]
FORCED = ["--sessions", "fpga,notes,work-*"]


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    return RemoteWorld.create(tmp_path)


def run(world: RemoteWorld, argv: list[str], command: str | None = None, **env: str) -> dict[str, Any]:
    """`handle` in process, as sshd would start the forced command."""
    full = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22", **env}
    if command is not None:
        full["SSH_ORIGINAL_COMMAND"] = command
    return d.handle(argv, full)


def standard_panes(world: RemoteWorld) -> None:
    world.set_panes(
        [
            pane("fpga", "%1", os.getppid(), command="claude"),
            pane("fpga", "%2", 999_999_1, window_index=1, window_name="tests", command="bash"),
            pane("private", "%3", 999_999_2),
            pane("work-api", "%4", 999_999_3, command="claude"),
        ],
        {"%1": "line 1\nline 2\nlast line", "%3": "secret stuff"},
    )


# --- requests and policy ---------------------------------------------------------------------


def test_version_reports_verbs_and_policy(world: RemoteWorld) -> None:
    out = run(world, [*FORCED, "--allow-usage"], "switchboard-remote version")
    assert out == {
        "protocol": 1,
        "ok": True,
        "version": d.VERSION,
        "verbs": ["version", "list", "capture", "send", "usage"],
        "allowSend": False,
        "allowUsage": True,
    }


@pytest.mark.parametrize(
    "command",
    [
        "",
        "version",  # no program word
        "ls -la",
        "switchboard-remote",
        "switchboard-remote unknown",
        "switchboard-remote send %1 123 -",  # "-" is not a session id ("none" is)
        "switchboard-remote send %1 123",
        "switchboard-remote send %1 abc none",
        "switchboard-remote send 1 123 none",
        "switchboard-remote send %1 123 not-a-uuid",
        "switchboard-remote send %1 123 none extra",
        "switchboard-remote list extra",
        "switchboard-remote capture %1",
        "switchboard-remote capture 1 10",
        "switchboard-remote capture %1 0",
        "switchboard-remote capture %1 501",
        "switchboard-remote capture %1 10 more",
        "switchboard-remote capture '%1;id' 10",
        "switchboard-remote 'list;id'",
        "switchboard-remote list $(id)",
        "switchboard-remote capture %1 '10 11'",
        "switchboard-remote 'unterminated",
    ],
)
def test_bad_requests_are_refused(world: RemoteWorld, command: str) -> None:
    assert run(world, FORCED, command) == {"protocol": 1, "ok": False, "error": "bad_request"}
    assert world.tmux_calls() == []


def test_without_an_ssh_command_nothing_runs(world: RemoteWorld) -> None:
    assert run(world, FORCED)["error"] == "bad_request"


def test_policy_flags_inside_the_request_do_not_count(world: RemoteWorld) -> None:
    standard_panes(world)
    # The forced command allows fpga only; options smuggled into SSH_ORIGINAL_COMMAND are tokens of the
    # request, and a request token starting with - is not a verb.
    assert run(world, ["--sessions", "fpga"], "switchboard-remote --sessions private list")["error"] == "bad_request"
    assert run(world, ["--sessions", "fpga"], "switchboard-remote list --sessions private")["error"] == "bad_request"
    listed = run(world, ["--sessions", "fpga"], "switchboard-remote list")
    assert {p["session"] for p in listed["panes"]} == {"fpga"}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--sessions"],
        ["--sessions", ""],
        ["--sessions", "a b"],
        ["--sessions", "a:b"],
        ["--sessions", "[ab]"],
        ["--sessions", "fpga", "--sessions", "x"],
        ["--sessions", "fpga", "--unknown"],
        ["--sessions", "fpga", "--claude-home", "relative/path"],
        ["--sessions", "fpga", "--claude-home", "/a/../b"],
        ["--sessions", "fpga", "--slash-commands", "Compact"],
        ["--sessions", ",".join(f"s{i}" for i in range(65))],
    ],
)
def test_bad_forced_policies_refuse_everything(world: RemoteWorld, argv: list[str]) -> None:
    assert run(world, argv, "switchboard-remote version")["error"] == "bad_policy"


def test_test_options_need_the_test_flag(world: RemoteWorld) -> None:
    argv = [*FORCED, "--tmux-socket-name", "sb-test-abc"]
    assert run(world, argv, "switchboard-remote version", SWITCHBOARD_REMOTE_TEST="0")["error"] == "bad_policy"
    assert run(world, argv, "switchboard-remote version")["ok"] is True
    assert run(world, [*FORCED, "--tmux-socket-name", "default"], "switchboard-remote version")["error"] == "bad_policy"


def test_policy_from_hub_is_refused_under_sshd(world: RemoteWorld) -> None:
    argv = ["--policy-from-hub", *FORCED, "--", "version"]
    assert run(world, argv)["error"] == "bad_policy"
    env = world.tmux_env()
    assert d.handle(argv, env)["ok"] is True
    assert d.handle(argv, {**env, "SSH_ORIGINAL_COMMAND": "x"})["error"] == "bad_policy"


def test_explicit_path_uses_the_policy_file_and_accepts_no_options(world: RemoteWorld) -> None:
    standard_panes(world)
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    assert d.handle(["list"], env)["error"] == "no_policy"
    policy = Path(world.home) / ".config" / "switchboard-remote" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"sessions": ["work-*"], "allowUsage": False}), encoding="utf-8")
    listed = d.handle(["list"], env)["panes"]
    assert isinstance(listed, list)
    assert {p["session"] for p in listed} == {"work-api"}
    # The hub sends the whole command line here, so options in it are refused.
    assert d.handle(["--sessions", "*", "list"], env)["error"] == "bad_request"
    policy.write_text(json.dumps({"sessions": ["work-*"], "extra": 1}), encoding="utf-8")
    assert d.handle(["list"], env)["error"] == "no_policy"
    policy.write_text("[]", encoding="utf-8")
    assert d.handle(["list"], env)["error"] == "no_policy"


# --- list --------------------------------------------------------------------------------------


def test_list_keeps_allowed_sessions_only_and_joins_the_registry(world: RemoteWorld) -> None:
    standard_panes(world)
    pid = os.getpid()
    world.register(
        "1001",
        pid,
        sessionId=SESSION_A,
        cwd="/home/USER/projects/fpgadc",
        name="parser fix",
        status="waiting",
        waitingFor="permission prompt",
        statusUpdatedAt=1764160000123,
        startedAt=1764150000000,
        messagingSocketPath="/run/user/1000/cc-socks/1.sock",
        bridgeSessionId="session_01abc",
        tmux="3:@4.%4",
    )
    # No tmux field: found through the process tree (the pane's pid is an ancestor of the session's pid).
    world.register("1002", pid, sessionId=SESSION_B, cwd="/srv/x/notes-app/", status="busy")
    # Dead process and bad session id: skipped.
    world.register("1003", 999_999_99, sessionId=SESSION_A, tmux="0:@0.%1")
    world.register("1004", pid, sessionId="not-a-uuid", tmux="0:@0.%1")
    (Path(world.home) / ".claude" / "sessions" / "1005.json").write_text("{broken", encoding="utf-8")
    out = run(world, FORCED, "switchboard-remote list")
    assert out["ok"] is True
    assert out["truncated"] is False
    panes = {p["paneId"]: p for p in out["panes"]}
    assert sorted(panes) == ["%1", "%2", "%4"]  # "private" is not allowed
    assert panes["%4"]["claude"] == {
        "sessionId": SESSION_A,
        "name": "parser fix",
        "status": "waiting",
        "waitingFor": "permission prompt",
        "statusUpdatedAt": 1764160000123,
        "startedAt": 1764150000000,
        "cwdName": "fpgadc",
    }
    assert panes["%1"]["claude"]["sessionId"] == SESSION_B
    assert panes["%1"]["claude"]["cwdName"] == "notes-app"
    assert panes["%2"]["claude"] is None
    assert panes["%2"] == {
        "session": "fpga",
        "sessionId": "$4",
        "windowIndex": 1,
        "windowId": "@1",
        "windowName": "tests",
        "paneIndex": 0,
        "paneId": "%2",
        "panePid": 9999991,
        "command": "bash",
        "active": True,
        "attached": False,
        "activity": 1764160000,
        "claude": None,
    }
    text = json.dumps(out)
    for leaked in ("/run/user", "session_01abc", "/home/USER", "private", "secret stuff"):
        assert leaked not in text
    assert world.tmux_calls()[0]["argv"] == ["list-panes", "-a", "-F", d.LIST_FORMAT]


def test_list_without_a_tmux_server_is_empty(world: RemoteWorld) -> None:
    world.set_panes([], noServer=True)
    assert run(world, FORCED, "switchboard-remote list") == {"protocol": 1, "ok": True, "panes": [], "truncated": False}


def test_list_tmux_failure(world: RemoteWorld) -> None:
    world.set_panes([], fail=True)
    assert run(world, FORCED, "switchboard-remote list")["error"] == "tmux_failed"


def test_the_test_tmux_hook_needs_the_test_flag(
    world: RemoteWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_panes(world)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    # No real tmux can be found, so ignoring the hook ends in tmux_failed instead of a real server.
    monkeypatch.setattr(d, "TMUX_PATH", str(empty))
    # The hook names a wrapper that runs the fake tmux without needing PATH.
    hook = tmp_path / "hook-tmux"
    # It leaves a marker, since without the flag the fake tmux would not get its FAKE_TMUX_* settings.
    marker = tmp_path / "hook-ran"
    hook.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(FAKE_TMUX)} "$@"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    env = {key: value for key, value in world.tmux_env().items() if key != "SWITCHBOARD_REMOTE_TEST"}
    env["SWITCHBOARD_REMOTE_TEST_TMUX"] = str(hook)
    full = {**env, "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22", "SSH_ORIGINAL_COMMAND": "switchboard-remote list"}
    assert d.handle(FORCED, full)["error"] == "tmux_failed"
    for value in ("0", "yes", ""):
        assert d.handle(FORCED, {**full, "SWITCHBOARD_REMOTE_TEST": value})["error"] == "tmux_failed"
    assert not marker.exists()  # the executable the hook names never ran
    flagged = d.handle(FORCED, {**full, "SWITCHBOARD_REMOTE_TEST": "1"})
    assert flagged["ok"] is True
    assert marker.exists()


def test_list_skips_malformed_lines_and_cleans_names(world: RemoteWorld) -> None:
    hostile = pane("fpga", "%1", 1, window_name="evil\x1b[2J\u202ename" + "x" * 100)
    world.set_panes([hostile], rawLines=["too\tfew\tfields", "\t".join(["fpga"] * 12)])
    world.register("1001", os.getpid(), sessionId=SESSION_A, name="a\u200bb\x07c", tmux="0:@0.%1", waitingFor="w" * 500)
    out = run(world, FORCED, "switchboard-remote list")
    assert [p["paneId"] for p in out["panes"]] == ["%1"]
    first = out["panes"][0]
    assert first["windowName"].startswith("evil[2Jname")
    assert len(first["windowName"]) == d.NAME_MAX
    assert first["claude"]["name"] == "abc"
    assert len(first["claude"]["waitingFor"]) == d.WAITING_MAX


def test_tmux_runs_with_a_fixed_environment(world: RemoteWorld) -> None:
    standard_panes(world)
    run(world, FORCED, "switchboard-remote list", PATH="/evil/bin", TMUX_TMPDIR="/tmp/x", LD_PRELOAD="evil.so")
    env = world.tmux_calls()[0]["env"]
    assert env["PATH"] == d.TMUX_PATH
    assert env["TMUX_TMPDIR"] == "/tmp/x"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "LD_PRELOAD" not in env
    assert "SSH_ORIGINAL_COMMAND" not in env


def test_isolated_socket_name_is_passed_to_tmux(world: RemoteWorld) -> None:
    standard_panes(world)
    run(world, [*FORCED, "--tmux-socket-name", "sb-test-x1"], "switchboard-remote list")
    assert world.tmux_calls()[0]["argv"][:2] == ["-L", "sb-test-x1"]


# --- capture -----------------------------------------------------------------------------------


def test_capture_an_allowed_pane(world: RemoteWorld) -> None:
    standard_panes(world)
    out = run(world, FORCED, "switchboard-remote capture %1 2")
    assert out == {
        "protocol": 1,
        "ok": True,
        "text": "line 2\nlast line\n",
        "paneId": "%1",
        "session": "fpga",
        "window": 0,
    }
    assert world.tmux_calls()[-1]["argv"] == ["capture-pane", "-p", "-J", "-t", "%1", "-S", "-2"]


def test_capture_outside_the_allow_list_answers_like_a_missing_pane(world: RemoteWorld) -> None:
    standard_panes(world)
    assert run(world, FORCED, "switchboard-remote capture %3 10")["error"] == "gone"
    assert run(world, FORCED, "switchboard-remote capture %9 10")["error"] == "gone"
    assert all(call["argv"][0] == "list-panes" for call in world.tmux_calls())


def test_capture_is_capped(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", 1)], {"%1": "\n".join("é" * 400 for _ in range(400))})
    out = run(world, FORCED, "switchboard-remote capture %1 500")
    assert len(out["text"].encode("utf-8")) <= d.MAX_CAPTURE_BYTES


# --- the installed script ----------------------------------------------------------------------


def test_deploy_copy_is_byte_identical_and_executable() -> None:
    deployed = REPO / "deploy" / "remote" / "switchboard-remote"
    assert deployed.read_bytes() == Path(DISPATCHER).read_bytes()
    assert os.access(deployed, os.X_OK)


def test_dispatcher_uses_only_the_standard_library_and_python_3_8_syntax() -> None:
    source = Path(DISPATCHER).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    # Things newer than 3.8 that ast.parse(feature_version) does not catch.
    for newer in ("removeprefix", "removesuffix", "strict=True", "match ", "functools.cache("):
        assert newer not in source, newer


def test_dispatcher_runs_isolated_as_a_script(world: RemoteWorld) -> None:
    standard_panes(world)
    env = {**world.tmux_env(), "SSH_ORIGINAL_COMMAND": "switchboard-remote list", "PATH": "/usr/bin:/bin"}
    done = subprocess.run(
        [sys.executable, "-I", DISPATCHER, *FORCED], env=env, capture_output=True, check=False, timeout=30
    )
    assert done.returncode == 0
    out = json.loads(done.stdout)
    assert out["ok"] is True
    assert len(out["panes"]) == 3
    assert done.stdout.decode("ascii").count("\n") == 1
