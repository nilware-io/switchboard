#!/usr/bin/env python3
"""Fake `claude` for tests. Standard library only.

Appends one JSON line {argv, env, stdin, pid, cwd, childPid} to the file named by
FAKE_CLAUDE_RECORD, reads stdin fully, then follows directives found in the prompt (one per line):
  SLEEP <ms>        wait before the result
  PROGRESS <n>      emit n assistant events with tool_use blocks
  RESULT <text>     result text (default "OK")
  FAIL              result with is_error true, exit 1
  NO_RESULT         exit without a result event
  EXIT <n>          exit code (after a normal result)
  RESUME_MISSING    when --resume is given: like the real CLI, stderr "No conversation found with
                    session ID: <id>", an error result event without `result`, exit 1
  SPAWN_CHILD       spawn a long-running child in the same process group (pid recorded)
  SPAWN_HOLDER      like SPAWN_CHILD, but the child inherits stdout and stderr (holds the pipes open)
  IGNORE_TERM       ignore SIGTERM (needs SIGKILL)
  SPLIT             write stdout in tiny chunks, with a non-JSON line
  STDERR <text>     write text to stderr
  SEND_MESSAGE <how> relay runs only: "none" (no SendMessage call), "wrong_to" (another address),
                    "changed" (altered text), "twice" (two calls), "denied" (one call, its
                    permission denied) or "denied_then_sent" (a denied call, then a sent one)
With --resume the run keeps that session id, with --fork-session it gets a new one.

A relay run (`--tools SendMessage`) emits one SendMessage tool_use like a faithful relay: "to" from
the `"to" set to "<address>"` part of --append-system-prompt and "message" from the text between
the two boundary lines of stdin. SEND_MESSAGE changes that.

With --bg (like `claude --bg --resume <id> --remote-control <name>`) it records the run, then
follows FAKE_CLAUDE_BG (comma separated): "untrusted" prints the trust error and exits 1, "fail"
exits 3, "missing" fails like a resume of an unknown session ID; "copy" starts the session under a
new id with the CLI's copy note. Otherwise it starts a detached sleeper that writes the live
registry entry `$CLAUDE_CONFIG_DIR/sessions/<sleeper pid>.json` (sessionId, kind "bg", name = id8,
cwd, startedAt in ms), adds `bridgeSessionId` after 0.2 s unless "nolink" or "stale" is given or
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC is set (like the real CLI), and removes the entry when it
gets SIGTERM (at most 60 s). "stale" also dates startedAt two hours back: a session whose process
lives but that has lost Remote Control. The launcher prints "backgrounded · <id8>" and exits 0.

Without --resume (`claude --bg --remote-control <name> --name <name> ...`, a new session task) the
session gets a new id and the launcher prints "backgrounded · <id8> · <name>". The registry entry
gets `status` "idle", or "busy" / "waiting" (with `waitingFor` "permission prompt") / "input"
(waiting with `waitingFor` "user answer") when that flag is in FAKE_CLAUDE_BG. When --settings has a
Stop hook command, the launcher of a new session runs it (`sh -c`) with {"session_id",
"last_assistant_message"} on stdin, the message from RESULT (default "OK"), unless "nohook" is given;
a resume (no turn of its own) runs it only with "turn" (the owner finished a turn in the app).

`stop <id>` records the call, SIGTERMs the bg sleepers whose session id starts with <id>, prints
"stopped <id>" and exits 0, or prints an error and exits 1 when there is none.

The background agent list (`claude agents --json --all`, not recorded) is the JSON list in
`$CLAUDE_CONFIG_DIR/fake-agents.json` (missing: empty). A --bg resume of a session listed there
starts a copy, like the real CLI does for a session that ran in the background before.
`rm <id8>` records the call and drops the entry with that `id` (prints "removed <id8>", exit 0),
or prints "No session matching" and exits 1; "rmfail" in FAKE_CLAUDE_BG makes it fail always.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

argv = sys.argv[1:]
stdin = sys.stdin.buffer.read().decode("utf-8", "replace")
lines = [line.strip() for line in stdin.split("\n")]


def directive(name: str) -> str | None:
    for line in lines:
        if line == name or line.startswith(name + " "):
            return line[len(name) :].strip()
    return None


def record_call() -> None:
    record_file = os.environ.get("FAKE_CLAUDE_RECORD")
    if record_file:
        rec = {"argv": argv, "env": dict(os.environ), "stdin": stdin, "pid": os.getpid(), "cwd": os.getcwd()}
        with open(record_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")


def stop_command() -> None:
    record_call()
    prefix = argv[1] if len(argv) > 1 else ""
    sessions_dir = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "sessions")
    stopped = False
    for name in os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else []:
        path = os.path.join(sessions_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if prefix and data.get("kind") == "bg" and str(data.get("sessionId", "")).startswith(prefix):
            try:
                os.kill(int(data["pid"]), signal.SIGTERM)
            except OSError:
                continue
            for _ in range(100):
                if not os.path.exists(path):
                    break
                time.sleep(0.02)
            stopped = True
    if not stopped:
        print(f"No background session {prefix}")
        sys.exit(1)
    print(f"stopped {prefix}")
    sys.exit(0)


def agents_path() -> str:
    return os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "fake-agents.json")


def read_agents() -> list[dict[str, Any]]:
    try:
        with open(agents_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def agents_command() -> None:
    print(json.dumps(read_agents()))
    sys.exit(0)


def rm_command() -> None:
    record_call()
    target = argv[1] if len(argv) > 1 else ""
    agents = read_agents()
    kept = [a for a in agents if a.get("id") != target]
    if "rmfail" in os.environ.get("FAKE_CLAUDE_BG", "").split(",") or len(kept) == len(agents):
        print(f"No session matching '{target}'")
        sys.exit(1)
    with open(agents_path(), "w", encoding="utf-8") as handle:
        json.dump(kept, handle)
    print(f"removed {target}")
    sys.exit(0)


def run_stop_hook(sid: str) -> None:
    raw = argv[argv.index("--settings") + 1] if "--settings" in argv else "{}"
    stops = json.loads(raw).get("hooks", {}).get("Stop", [])
    for group in stops:
        for hook in group.get("hooks", []):
            payload = {
                "session_id": sid,
                "hook_event_name": "Stop",
                "last_assistant_message": directive("RESULT") or "OK",
            }
            subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(payload).encode(), check=True)


def background() -> None:
    new = "--resume" not in argv
    sid = str(uuid.uuid4()) if new else argv[argv.index("--resume") + 1]
    name = argv[argv.index("--remote-control") + 1]
    flags = os.environ.get("FAKE_CLAUDE_BG", "").split(",")
    record_call()
    if "untrusted" in flags:
        print(f"Workspace not trusted. Run `claude` in {os.getcwd()} once and accept the trust prompt, then retry.")
        sys.exit(1)
    if "fail" in flags:
        print("Error: something broke")
        sys.exit(3)
    if "missing" in flags:
        print(f"No conversation found with session ID: {sid}")
        sys.exit(1)
    note = ""
    if "copy" in flags or (not new and any(a.get("sessionId") == sid for a in read_agents())):
        old8, sid = sid[:8], str(uuid.uuid4())
        note = f"note: background session {old8} keeps its own saved options, so the flags you passed started a copy as {sid[:8]}.\n"
    sessions_dir = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "sessions")
    sleeper = (
        "import json, os, signal, sys, time\n"
        "d, sid, name, cwd, link, status, started = sys.argv[1:8]\n"
        "path = os.path.join(d, f'{os.getpid()}.json')\n"
        "def write(extra):\n"
        "    with open(path + '.tmp', 'w') as h: json.dump({'pid': os.getpid(), 'sessionId': sid, 'cwd': cwd, 'kind': 'bg', 'name': sid[:8], 'startedAt': int(started), **json.loads(status), **extra}, h)\n"
        "    os.replace(path + '.tmp', path)\n"
        "def stop(*_):\n"
        "    try: os.unlink(path)\n"
        "    except OSError: pass\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "write({})\n"
        "time.sleep(0.2)\n"
        "if link == '1': write({'bridgeSessionId': 'session_01FAKE' + sid[:8]})\n"
        "time.sleep(60)\n"
        "stop()\n"
    )
    link = (
        "0"
        if "nolink" in flags or "stale" in flags or os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
        else "1"
    )
    started = int(time.time() * 1000) - (2 * 3600 * 1000 if "stale" in flags else 0)
    status: dict[str, str] = {"status": "idle"}
    if "busy" in flags:
        status = {"status": "busy"}
    elif "waiting" in flags:
        status = {"status": "waiting", "waitingFor": "permission prompt"}
    elif "input" in flags:
        status = {"status": "waiting", "waitingFor": "user answer"}
    if (new or "turn" in flags) and "nohook" not in flags:
        run_stop_hook(sid)
    subprocess.Popen(
        [sys.executable, "-c", sleeper, sessions_dir, sid, name, os.getcwd(), link, json.dumps(status), str(started)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    label = f" \u00b7 {argv[argv.index('--name') + 1]}" if "--name" in argv else ""
    sys.stdout.buffer.write(f"backgrounded \u00b7 {sid[:8]}{label}\n{note}".encode())
    sys.exit(0)


if argv[:1] == ["stop"]:
    stop_command()
if argv[:1] == ["agents"]:
    agents_command()
if argv[:1] == ["rm"]:
    rm_command()
if "--bg" in argv:
    background()


def arg_after(flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


resume_id = arg_after("--resume")

record: dict[str, Any] = {
    "argv": argv,
    "env": dict(os.environ),
    "stdin": stdin,
    "pid": os.getpid(),
    "cwd": os.getcwd(),
    "childPid": None,
}

if directive("SPAWN_CHILD") is not None or directive("SPAWN_HOLDER") is not None:
    holder = directive("SPAWN_HOLDER") is not None
    child = subprocess.Popen(  # same process group, not waited for (like child.unref())
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
        stdin=subprocess.DEVNULL,
        stdout=None if holder else subprocess.DEVNULL,
        stderr=None if holder else subprocess.DEVNULL,
    )
    record["childPid"] = child.pid

# A Python handler (not SIG_IGN) so children spawned above or later keep the default disposition
# (an ignored signal would be inherited).
if directive("IGNORE_TERM") is not None:
    signal.signal(signal.SIGTERM, lambda _sig, _frame: None)

record_path = os.environ.get("FAKE_CLAUDE_RECORD")
if record_path:
    with open(record_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")

split = directive("SPLIT") is not None
out: list[str] = []


def emit(event: dict[str, Any]) -> None:
    out.append(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")


def flush() -> None:
    text = "".join(out)
    out.clear()
    if not split:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    for i in range(0, len(text), 7):
        sys.stdout.write(text[i : i + 7])
        sys.stdout.flush()
        time.sleep(0.001)


stderr_text = directive("STDERR")
if stderr_text:
    sys.stderr.write(stderr_text + "\n")
    sys.stderr.flush()

if directive("RESUME_MISSING") is not None and resume_id:
    sys.stderr.write(f"No conversation found with session ID: {resume_id}\n")
    sys.stderr.flush()
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "duration_ms": 0,
            "duration_api_ms": 0,
            "is_error": True,
            "num_turns": 0,
            "session_id": resume_id,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "permission_denials": [],
        }
    )
    flush()
    sys.exit(1)

# --fork-session continues under a new id, like the real CLI.
session_id = resume_id if resume_id and "--fork-session" not in argv else str(uuid.uuid4())
emit(
    {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "cwd": os.getcwd(),
        "tools": ["Bash", "Edit", "Read"],
    }
)
if split:
    out.append("this is not json\n")

progress = int(directive("PROGRESS") or 0)
tools: list[dict[str, Any]] = [
    {"name": "Read", "input": {"file_path": "/srv/project/src/config.py"}},
    {"name": "Bash", "input": {"command": "npm test -- --reporter dot", "description": "Run tests"}},
    {"name": "Edit", "input": {"file_path": "/srv/project/README.md", "old_string": "a", "new_string": "b"}},
    {"name": "Grep", "input": {"pattern": "TODO"}},
]
for i in range(progress):
    tool = tools[i % len(tools)]
    emit(
        {
            "type": "assistant",
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working."},
                    {"type": "tool_use", "id": f"toolu_{i}", "name": tool["name"], "input": tool["input"]},
                ],
            },
        }
    )


def relay_calls() -> list[tuple[dict[str, Any], bool]]:
    """The SendMessage inputs of a relay run and whether each was denied, following SEND_MESSAGE."""
    system = argv[argv.index("--append-system-prompt") + 1] if "--append-system-prompt" in argv else ""
    match = re.search(r'"to" set to ("(?:[^"\\]|\\.)*")', system)
    to = json.loads(match.group(1)) if match else ""
    framed = re.search(r"^----- (MESSAGE [0-9a-f]+) -----\n(.*)\n----- \1 -----$", stdin, re.DOTALL | re.MULTILINE)
    message = framed.group(2) if framed else ""
    how = directive("SEND_MESSAGE") or ""
    call = {"to": to, "summary": "Message from the owner, dictated by voice", "message": message}
    if how == "none":
        return []
    if how == "wrong_to":
        return [({**call, "to": "uds:/tmp/cc-socks/other.sock"}, False)]
    if how == "changed":
        return [({**call, "message": message + " (shortened)"}, False)]
    if how == "twice":
        return [(call, False), (call, False)]
    if how == "denied":
        return [(call, True)]
    if how == "denied_then_sent":
        return [(call, True), (call, False)]
    return [(call, False)]


permission_denials: list[dict[str, Any]] = []
if arg_after("--tools") == "SendMessage":
    for i, (call, denied) in enumerate(relay_calls()):
        use_id = f"toolu_send_{i}"
        emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": use_id, "name": "SendMessage", "input": call}],
                },
            }
        )
        emit(
            {
                "type": "user",
                "session_id": session_id,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": use_id,
                            "is_error": denied,
                            "content": "Permission denied" if denied else "Message sent",
                        }
                    ],
                },
            }
        )
        if denied:
            permission_denials.append({"tool_name": "SendMessage", "tool_use_id": use_id, "tool_input": call})
flush()

sleep_ms = float(directive("SLEEP") or 0)
if sleep_ms > 0:
    time.sleep(sleep_ms / 1000)

if directive("NO_RESULT") is not None:
    flush()
    sys.exit(0)

fail = directive("FAIL") is not None
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": fail,
        "result": "API Error: something went wrong" if fail else (directive("RESULT") or "OK"),
        "session_id": session_id,
        "total_cost_usd": 0.0123,
        "duration_ms": 1234,
        "num_turns": 2,
        "permission_denials": permission_denials,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0},
    }
)
flush()
sys.exit(1 if fail else int(directive("EXIT") or 0))
