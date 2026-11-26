"""Dispatcher serve mode (outbound agents): framing, limits, malformed frames, concurrency, cancel,
liveness and exit codes, and policy enforcement identical to the forced command."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.remote.agent_helpers import FrameReader, ServeSession, frame
from tests.remote.helpers import DISPATCHER, SESSION_A, RemoteWorld, pane
from tests.usage.helpers import S1, S2, assistant, user, write_transcript
from voice_coding_mcp.remote import dispatcher as d

POLICY = ["--sessions", "fpga,notes,work-*"]
CLAUDE_PID = 9_300_001
EMPTY_BOX = "⏺ Done.\n╭──────────╮\n│ >        │\n╰──────────╯\n  ? for shortcuts\n"


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", 9_300_002, window_index=1, window_name="shell", command="bash"),
            pane("private", "%3", 9_300_003),
        ],
        {"%1": EMPTY_BOX, "%2": "$ ", "%3": "secret stuff"},
    )
    world.register("2001", os.getpid(), sessionId=SESSION_A, name="fpga", status="idle", tmux="0:@0.%1")
    return world


def session(world: RemoteWorld, policy: list[str] | None = None, **kwargs: Any) -> ServeSession:
    return ServeSession(["--serve", *(POLICY if policy is None else policy)], world.tmux_env(), **kwargs)


def started(world: RemoteWorld, policy: list[str] | None = None, **kwargs: Any) -> ServeSession:
    serve = session(world, policy, **kwargs)
    hello = serve.read()
    assert hello is not None
    assert hello["t"] == "hello"
    return serve


# --- hello and the policy ------------------------------------------------------------------------


def test_hello_is_the_version_body_and_the_policy_state_without_identity(world: RemoteWorld) -> None:
    serve = session(world, [*POLICY, "--allow-usage"])
    hello = serve.read()
    assert hello == {
        "t": "hello",
        "protocol": 1,
        "version": d.VERSION,
        "verbs": list(d.VERBS),
        "allowSend": False,
        "allowUsage": True,
        "policy": "ok",
    }
    assert serve.finish() == 0


@pytest.mark.parametrize(
    "policy",
    [
        [],
        ["--sessions"],
        ["--sessions", "a b"],
        ["--sessions", "fpga", "--bogus"],
        ["--sessions", "fpga", "--policy-from-hub"],
        ["--sessions", "fpga", "list"],
        ["--sessions", "fpga", "--sessions", "notes"],
        ["--sessions", "fpga", "--tmux-socket-name", "sb-test-x"],
        ["--sessions", "fpga", "--claude-home", "relative"],
    ],
)
def test_a_broken_policy_connects_and_refuses_everything(tmp_path: Path, policy: list[str]) -> None:
    env = {"HOME": str(tmp_path)}
    serve = ServeSession(["--serve", *policy], env)
    hello = serve.read()
    assert hello is not None
    assert hello["policy"] == "bad_policy"
    assert (hello["allowSend"], hello["allowUsage"]) == (False, False)
    for request_id, verb in enumerate(["version", "list", "send", "bogus"], 1):
        assert serve.request(request_id, verb, []) == [{"protocol": 1, "ok": False, "error": "bad_policy"}]
    assert serve.finish() == 0


@pytest.mark.parametrize("variable", ["SSH_ORIGINAL_COMMAND", "SSH_CONNECTION"])
def test_serve_mode_refuses_to_run_under_sshd(world: RemoteWorld, variable: str) -> None:
    env = {**world.tmux_env(), variable: "switchboard-remote list"}
    serve = ServeSession(["--serve", *POLICY], env)
    assert serve.read() == {"protocol": 1, "ok": False, "error": "bad_policy"}
    assert serve.read() is None
    assert serve.finish() == 0
    assert world.tmux_calls() == []


def test_serve_on_the_forced_command_path_is_bad_policy(world: RemoteWorld) -> None:
    env = {**world.tmux_env(), "SSH_ORIGINAL_COMMAND": "switchboard-remote version", "SSH_CONNECTION": "x"}
    assert d.handle(["--serve", *POLICY], env)["error"] == "bad_policy"
    assert d.handle([*POLICY, "--serve"], env)["error"] == "bad_policy"


def test_serve_requested_skips_option_values() -> None:
    assert d.serve_requested(["--serve", "--sessions", "x"])
    assert d.serve_requested(["--sessions", "x", "--serve"])
    assert not d.serve_requested(["--sessions", "--serve"])
    assert not d.serve_requested(["--sessions", "x", "--", "--serve"])
    assert not d.serve_requested(["--sessions", "x", "version", "--serve"])


def test_no_frame_can_change_the_policy(world: RemoteWorld) -> None:
    serve = started(world)
    attempts: list[dict[str, Any]] = [
        {"t": "req", "id": 1, "verb": "version", "args": [], "policy": {"allowSend": True}},
        {"t": "req", "id": 2, "verb": "version", "args": ["--allow-send"]},
        {"t": "req", "id": 3, "verb": "--allow-send", "args": []},
        {"t": "req", "id": 4, "verb": "send", "args": ["--allow-send", "%1", str(CLAUDE_PID), SESSION_A, "message"]},
        {"t": "welcome", "protocol": 1, "allowSend": True},
        {"t": "policy", "allowSend": True},
    ]
    for attempt in attempts:
        serve.send(attempt)
        got = serve.read()
        assert got is not None
        assert got.get("doc", {}).get("ok") is not True or got["doc"].get("allowSend") is False, got
    docs = serve.request(9, "send", ["%1", str(CLAUDE_PID), SESSION_A, "message"], input="hi")
    assert docs == [{"protocol": 1, "ok": False, "error": "not_allowed"}]
    assert serve.request(10, "version", [])[0]["allowSend"] is False
    serve.finish()
    assert not any(call["argv"][:1] == ["load-buffer"] for call in world.tmux_calls())


# --- the same answers as the forced command --------------------------------------------------------

CORPUS: list[tuple[list[str], str | None]] = [
    (["version"], None),
    (["list"], None),
    (["capture", "%1", "20"], None),
    (["capture", "%2", "500"], None),
    (["capture", "%3", "20"], None),
    (["capture", "%9", "20"], None),
    (["capture", "%1", "0"], None),
    (["capture", "%1", "501"], None),
    (["capture", "%1"], None),
    (["capture", "%1;rm", "20"], None),
    (["capture", "$(id)", "20"], None),
    (["capture", "%1", "20", "extra"], None),
    (["list", "x"], None),
    (["bogus"], None),
    (["send", "%1", str(CLAUDE_PID), SESSION_A, "message"], "run the tests"),
    (["send", "%1", str(CLAUDE_PID), SESSION_A, "message"], "!rm -rf /"),
    (["send", "%1", str(CLAUDE_PID), SESSION_A, "message"], "a\tb"),
    (["send", "%1", str(CLAUDE_PID), SESSION_A, "answer", "none"], "1"),
    (["send", "%1", "1", SESSION_A, "message"], "hi"),
    (["send", "%2", "9300002", "none", "message"], "ls"),
    (["send", "%3", "9300003", "none", "message"], "ls"),
    (["send", "%1", str(CLAUDE_PID), "nope", "message"], "hi"),
    (["keys", "%1", str(CLAUDE_PID), SESSION_A, "menu", "none", "Enter"], None),
    (["keys", "%1", str(CLAUDE_PID), SESSION_A, "menu", "none", "C-c"], None),
    (["keys", "%1", str(CLAUDE_PID), SESSION_A, "menu", "none", "BTab"], None),
    (["keys", "%1", str(CLAUDE_PID), SESSION_A, "answer", "none", "1"], None),
    (["resize", "%1", str(CLAUDE_PID), SESSION_A, "120", "40"], None),
    (["resize", "%1", str(CLAUDE_PID), SESSION_A, "20", "40"], None),
    (["usage", "0", "10"], None),
    (["usage", "0", "10"], '[{"id": "' + S1 + '", "size": 1, "mtime": 1}]'),
    (["usage", "0", "10"], "not json"),
    (["usage", "0", "0"], None),
]
POLICIES = [
    POLICY,
    [*POLICY, "--allow-usage", "--allow-send"],
    [
        "--sessions",
        "*",
        "--allow-usage",
        "--allow-send",
        "--allow-permission-answers",
        "--send-to-any-pane",
        "--slash-commands",
        "compact",
    ],
]


def forced(world: RemoteWorld, policy: list[str], tokens: list[str], data: str | None) -> list[dict[str, Any]]:
    env = {
        **world.tmux_env(),
        "SSH_CONNECTION": "x",
        "SSH_ORIGINAL_COMMAND": shlex.join(["switchboard-remote", *tokens]),
    }
    return list(d.respond(policy, env, data.encode("utf-8") if data is not None else b""))


@pytest.mark.parametrize("policy", POLICIES, ids=["no-opt-ins", "send-and-usage", "everything"])
def test_every_request_answers_exactly_as_the_forced_command(world: RemoteWorld, policy: list[str]) -> None:
    write_transcript(world.home + "/.claude", S1, [user(1_764_000_000_000), assistant(1_764_000_001_000)])
    write_transcript(world.home + "/.claude", S2, [user(1_764_000_002_000)])
    expected = [forced(world, policy, tokens, data) for tokens, data in CORPUS]
    forced_tmux = [c["argv"] for c in world.tmux_calls()]
    Path(world.tmux_log).unlink()
    serve = started(world, policy)
    got = []
    for request_id, (tokens, data) in enumerate(CORPUS, 1):
        extra = {"input": data} if data is not None and tokens[0] in ("send", "usage") else {}
        got.append(serve.request(request_id, tokens[0], tokens[1:], **extra))
    serve.finish()
    assert got == expected
    # Buffer names are random per send; everything else the two modes ran is the same.
    served_tmux = [c["argv"] for c in world.tmux_calls()]
    assert [a for a in served_tmux if "-b" not in a] == [a for a in forced_tmux if "-b" not in a]
    assert len(served_tmux) == len(forced_tmux)


def test_a_send_text_arrives_only_on_load_buffer_stdin(world: RemoteWorld) -> None:
    serve = started(world, [*POLICY, "--allow-send"])
    text = "line one\nline $(two) `three`"
    docs = serve.request(1, "send", ["%1", str(CLAUDE_PID), SESSION_A, "message"], input=text)
    serve.finish()
    assert docs[0]["ok"] is True, docs
    calls = world.tmux_calls()
    assert [c["stdin"] for c in calls if c["argv"][:1] == ["load-buffer"]] == [text]
    assert not any(text in " ".join(c["argv"]) for c in calls)


def test_input_on_a_verb_without_stdin_is_bad_request(world: RemoteWorld) -> None:
    serve = started(world)
    assert serve.request(1, "version", [], input="x") == [{"protocol": 1, "ok": False, "error": "bad_request"}]
    assert serve.request(2, "capture", ["%1", "10"], input="") == [{"protocol": 1, "ok": False, "error": "bad_request"}]
    serve.finish()


def test_lone_surrogates_in_input_are_refused_like_invalid_utf8(world: RemoteWorld) -> None:
    serve = started(world, [*POLICY, "--allow-send"])
    serve.write(
        b'{"t":"req","id":1,"verb":"send","args":["%1","'
        + str(CLAUDE_PID).encode()
        + b'","'
        + SESSION_A.encode()
        + b'","message"],"input":"a\\ud800b"}\n'
    )
    got = serve.read()
    serve.finish()
    assert got == {"t": "res", "id": 1, "doc": {"protocol": 1, "ok": False, "error": "bad_text"}}


def test_a_usage_stream_is_one_res_frame_per_line_with_more_on_all_but_the_last(world: RemoteWorld) -> None:
    write_transcript(world.home + "/.claude", S1, [user(1_764_000_000_000), assistant(1_764_000_001_000)])
    serve = started(world, [*POLICY, "--allow-usage"])
    serve.send({"t": "req", "id": 5, "verb": "usage", "args": ["0", "10"]})
    frames = []
    while True:
        got = serve.read()
        assert got is not None
        frames.append(got)
        if not got.get("more"):
            break
    serve.finish()
    assert all(f["t"] == "res" and f["id"] == 5 for f in frames)
    assert [f.get("more") for f in frames] == [True] * (len(frames) - 1) + [None]
    assert frames[0]["doc"] == {"protocol": 1, "ok": True, "stream": "usage"}
    assert frames[-1]["doc"] == {"done": True, "gone": []}
    assert "more" not in frames[-1]


# --- framing and malformed frames ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "error_id"),
    [
        (b"not json", None),
        (b"[1, 2]", None),
        (b'"text"', None),
        (b"{}", None),
        (b'{"t": "nope", "id": 3}', 3),
        (b'{"t": "req", "id": 3, "verb": "version"}', 3),
        (b'{"t": "req", "id": 3, "verb": "version", "args": [], "extra": 1}', 3),
        (b'{"t": "req", "id": 3, "verb": 7, "args": []}', 3),
        (b'{"t": "req", "id": 3, "verb": "version", "args": "list"}', 3),
        (b'{"t": "req", "id": 3, "verb": "version", "args": [1]}', 3),
        (b'{"t": "req", "id": 3, "verb": "version", "args": [], "input": 5}', 3),
        (
            b'{"t": "req", "id": 3, "verb": "capture", "args": ["a","a","a","a","a","a","a","a","a","a","a","a","a","a","a","a","a"]}',
            3,
        ),
        (b'{"t": "req", "id": 0, "verb": "version", "args": []}', None),
        (b'{"t": "req", "id": -1, "verb": "version", "args": []}', None),
        (b'{"t": "req", "id": 9007199254740992, "verb": "version", "args": []}', None),
        (b'{"t": "req", "id": 1.5, "verb": "version", "args": []}', None),
        (b'{"t": "req", "id": true, "verb": "version", "args": []}', None),
        (b'{"t": "req", "id": "1", "verb": "version", "args": []}', None),
        (b'{"t": "ping"}', None),
        (b'{"t": "welcome", "protocol": 2}', None),
        (b'{"t": "bye", "reason": "because"}', None),
        (b"\xff\xfe", None),
        (b'{"t": "ping", "id": 1, "t": "nope"}', 1),
    ],
)
def test_a_malformed_frame_is_answered_and_the_connection_goes_on(
    world: RemoteWorld, raw: bytes, error_id: Any
) -> None:
    serve = started(world)
    serve.write(raw + b"\n")
    assert serve.read() == {"t": "error", "id": error_id, "error": "bad_frame"}
    assert serve.request(100, "version", [])[0]["ok"] is True
    assert serve.finish() == 0


def test_the_largest_id_and_args_are_accepted(world: RemoteWorld) -> None:
    serve = started(world)
    docs = serve.request(2**53 - 1, "capture", ["%1"] * 16)
    assert docs == [{"protocol": 1, "ok": False, "error": "bad_request"}]
    serve.finish()


def test_a_frame_over_the_limit_closes_with_frame_too_large(world: RemoteWorld) -> None:
    serve = started(world)
    payload = b'{"t":"req","id":1,"verb":"usage","args":["0","1"],"input":"' + b"x" * d.MAX_HUB_FRAME_BYTES + b'"}\n'
    writer = threading.Thread(target=lambda: _quiet_write(serve, payload), daemon=True)
    writer.start()
    assert serve.read() == {"t": "error", "id": None, "error": "frame_too_large"}
    assert serve.finish() == d.EXIT_REFUSED
    writer.join(10)


def test_a_frame_of_exactly_the_limit_is_read(world: RemoteWorld) -> None:
    serve = started(world, [*POLICY, "--allow-usage"])
    head = b'{"t":"req","id":1,"verb":"usage","args":["0","1"],"input":"'
    tail = b'"}'
    filler = d.MAX_HUB_FRAME_BYTES - len(head) - len(tail)
    line = head + b" " * filler + tail
    assert len(line) == d.MAX_HUB_FRAME_BYTES
    writer = threading.Thread(target=lambda: _quiet_write(serve, line + b"\n"), daemon=True)
    writer.start()
    got = serve.read()
    writer.join(10)
    assert got is not None
    # The frame was read whole; the known-list (2 MB of spaces) is then over the verb's own 1 MB cap.
    assert got["doc"] == {"protocol": 1, "ok": False, "error": "bad_request"}
    serve.finish()


def _quiet_write(serve: ServeSession, data: bytes) -> None:
    with contextlib.suppress(OSError):
        serve.write(data)


def test_frames_are_ascii_lines(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, window_name="häßlich ✓", command="claude")], {"%1": "Grüße ✓"})
    serve = session(world)
    serve.send({"t": "req", "id": 1, "verb": "list", "args": []})
    serve.send({"t": "req", "id": 2, "verb": "capture", "args": ["%1", "10"]})
    lines = [serve.reader.line(), serve.reader.line(), serve.reader.line()]
    serve.finish()
    frames = []
    for line in lines:
        assert line is not None
        line.decode("ascii")
        assert line.startswith(b"{")
        frames.append(json.loads(line))
    capture = next(f for f in frames if f.get("id") == 2)
    assert capture["doc"]["text"].strip() == "Grüße ✓"


# --- concurrency, busy, duplicate ids, cancel ------------------------------------------------------


def test_requests_run_concurrently_and_answer_whole_frames(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = d.verb_capture

    def slow_capture(*args: Any) -> dict[str, object]:
        gate.wait(10)
        return real(*args)

    monkeypatch.setattr(d, "verb_capture", slow_capture)
    serve = started(world)
    for request_id in (1, 2, 3, 4):
        serve.send({"t": "req", "id": request_id, "verb": "capture", "args": ["%1", "10"]})
    serve.send({"t": "req", "id": 5, "verb": "version", "args": []})
    assert serve.read() == {"t": "error", "id": 5, "error": "busy"}
    serve.send({"t": "ping", "id": 6})
    assert serve.read() == {"t": "pong", "id": 6}
    gate.set()
    answered = {serve.read()["id"] for _ in range(4)}  # type: ignore[index]
    assert answered == {1, 2, 3, 4}
    serve.send({"t": "req", "id": 7, "verb": "version", "args": []})
    assert serve.read()["id"] == 7  # type: ignore[index]
    serve.finish()


def test_a_duplicate_id_in_flight_is_refused(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = d.verb_capture
    monkeypatch.setattr(d, "verb_capture", lambda *a: (gate.wait(10), real(*a))[1])
    serve = started(world)
    serve.send({"t": "req", "id": 1, "verb": "capture", "args": ["%1", "10"]})
    serve.send({"t": "req", "id": 1, "verb": "version", "args": []})
    assert serve.read() == {"t": "error", "id": 1, "error": "duplicate_id"}
    gate.set()
    got = serve.read()
    assert got is not None
    assert got["doc"]["paneId"] == "%1"
    # Once answered, the id may be used again.
    assert serve.request(1, "version", [])[0]["ok"] is True
    serve.finish()


def test_sends_keys_and_resizes_run_one_at_a_time(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    most = 0
    lock = threading.Lock()

    def counted(name: str) -> Any:
        real = getattr(d, name)

        def run(*args: Any) -> Any:
            nonlocal active, most
            with lock:
                active += 1
                most = max(most, active)
            time.sleep(0.2)
            try:
                return real(*args)
            finally:
                with lock:
                    active -= 1

        return run

    for name in ("verb_send", "verb_keys", "verb_resize"):
        monkeypatch.setattr(d, name, counted(name))
    serve = started(world, [*POLICY, "--allow-send"])
    serve.send(
        {"t": "req", "id": 1, "verb": "send", "args": ["%1", str(CLAUDE_PID), SESSION_A, "message"], "input": "a"}
    )
    serve.send(
        {"t": "req", "id": 2, "verb": "keys", "args": ["%1", str(CLAUDE_PID), SESSION_A, "menu", "none", "Enter"]}
    )
    serve.send({"t": "req", "id": 3, "verb": "resize", "args": ["%1", str(CLAUDE_PID), SESSION_A, "100", "30"]})
    ids = {serve.read()["id"] for _ in range(3)}  # type: ignore[index]
    serve.finish()
    assert ids == {1, 2, 3}
    assert most == 1


def test_cancel_stops_a_usage_stream(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()

    def documents(*_args: Any) -> Any:
        yield {"file": S1, "size": 1, "mtime": 1, "project": "p", "records": 0}
        gate.wait(10)
        yield {"file": S2, "size": 1, "mtime": 1, "project": "p", "records": 0}
        yield {"done": True, "gone": []}

    monkeypatch.setattr(d, "usage_documents", documents)
    serve = started(world, [*POLICY, "--allow-usage"])
    serve.send({"t": "req", "id": 4, "verb": "usage", "args": ["0", "10"]})
    assert serve.read() == {"t": "res", "id": 4, "doc": {"protocol": 1, "ok": True, "stream": "usage"}, "more": True}
    serve.send({"t": "cancel", "id": 4})
    serve.send({"t": "cancel", "id": 99})
    time.sleep(0.2)
    gate.set()
    rest = []
    while True:
        got = serve.read()
        assert got is not None
        rest.append(got)
        if not got.get("more"):
            break
    serve.finish()
    assert rest[-1] == {"t": "res", "id": 4, "doc": {"protocol": 1, "ok": False, "error": "cancelled"}}
    assert not any(f["doc"].get("done") for f in rest)


def test_cancel_does_not_stop_other_verbs(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = d.verb_capture
    monkeypatch.setattr(d, "verb_capture", lambda *a: (gate.wait(10), real(*a))[1])
    serve = started(world)
    serve.send({"t": "req", "id": 1, "verb": "capture", "args": ["%1", "10"]})
    serve.send({"t": "cancel", "id": 1})
    gate.set()
    got = serve.read()
    serve.finish()
    assert got is not None
    assert got["doc"]["ok"] is True
    assert "more" not in got


def test_a_crashing_verb_answers_crashed_and_the_connection_goes_on(
    world: RemoteWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(d, "verb_list", boom)
    serve = started(world)
    serve.send({"t": "req", "id": 1, "verb": "list", "args": []})
    assert serve.read() == {"t": "error", "id": 1, "error": "crashed"}
    assert serve.request(2, "version", [])[0]["ok"] is True
    serve.finish()


# --- liveness and exit codes -----------------------------------------------------------------------


def test_ping_and_welcome(world: RemoteWorld) -> None:
    serve = started(world)
    serve.send({"t": "welcome", "protocol": 1})
    serve.send({"t": "ping", "id": 42})
    assert serve.read() == {"t": "pong", "id": 42}
    assert serve.finish() == 0


@pytest.mark.parametrize(
    ("reason", "code"), [("replaced", 75), ("unknown_machine", 76), ("protocol", 76), ("shutdown", 0)]
)
def test_bye_exit_codes(world: RemoteWorld, reason: str, code: int) -> None:
    serve = started(world)
    serve.send({"t": "bye", "reason": reason})
    serve.thread.join(10)
    assert serve.code == code
    serve.finish()


def test_end_of_input_exits_0(world: RemoteWorld) -> None:
    serve = started(world)
    assert serve.finish() == 0


def test_silence_ends_serve_mode(world: RemoteWorld) -> None:
    serve = started(world, silence=0.3)
    serve.thread.join(10)
    assert serve.code == 0
    serve.finish()


def test_a_send_in_progress_completes_when_the_connection_drops(
    world: RemoteWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = d.verb_send
    monkeypatch.setattr(d, "verb_send", lambda *a: (gate.wait(10), real(*a))[1])
    serve = started(world, [*POLICY, "--allow-send"])
    serve.send(
        {"t": "req", "id": 1, "verb": "send", "args": ["%1", str(CLAUDE_PID), SESSION_A, "message"], "input": "go"}
    )
    time.sleep(0.2)
    serve.close_input()
    gate.set()
    assert serve.finish() == 0
    assert [c["stdin"] for c in world.tmux_calls() if c["argv"][:1] == ["load-buffer"]] == ["go"]


def test_serve_mode_as_a_script(world: RemoteWorld) -> None:
    env = {**world.tmux_env(), "PATH": "/usr/bin:/bin"}
    process = subprocess.Popen(
        [sys.executable, "-I", DISPATCHER, "--serve", *POLICY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    reader = FrameReader(process.stdout.fileno())
    assert (reader.frame() or {})["t"] == "hello"
    process.stdin.write(frame({"t": "req", "id": 1, "verb": "list", "args": []}))
    process.stdin.flush()
    got = reader.frame()
    assert got is not None
    assert [p["paneId"] for p in got["doc"]["panes"]] == ["%1", "%2"]
    process.stdin.write(frame({"t": "bye", "reason": "replaced"}))
    process.stdin.close()
    assert process.wait(20) == 75
    process.stdout.close()
