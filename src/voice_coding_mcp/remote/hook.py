#!/usr/bin/env python3
"""switchboard-hook: Claude Code hooks and the notify_owner MCP server of a Switchboard machine.

Standard library only, Python 3.8 or newer, one file, installed next to the dispatcher on every
machine. `deploy/remote/switchboard-hook` is a byte-identical copy of this file;
`voice-coding-mcp remote-hook` prints it. Design: docs/SWITCHBOARD.md, "Session hooks".

Commands:

- `switchboard-hook hook`: run by Claude Code for the Notification, Stop, StopFailure and (Bash)
  PostToolUse hooks. Reads the hook's JSON from stdin, keeps a few fields of it, and writes one
  small event file into the spool `~/.local/state/switchboard-hook/events` (0700, files 0600, at
  most MAX_SPOOL_FILES files of at most MAX_EVENT_BYTES). It never blocks Claude Code: it prints
  nothing, always exits 0, and gives up after HARD_SECONDS. It never sends anything anywhere: the
  dispatcher's `events` verb (`--allow-events`) hands the spool to the hub over the machine's own
  link. Never written: the transcript, the assistant's text, tool output, prompts or screen text.
- `switchboard-hook mcp`: a stdio MCP server with one tool, `notify_owner(message, level?, url?)`,
  which spools an `owner_note` event the same way (rate limited per session and per machine).
- `switchboard-hook install [--dry-run] [--settings FILE] [--no-mcp] [--allow-tool]` and
  `switchboard-hook uninstall [--dry-run] [--settings FILE] [--no-mcp]`: merge the hooks into
  Claude Code's user settings (a backup first, idempotent, other hooks kept, the change shown as a
  diff) and add the MCP server with `claude mcp add --scope user`.

Event kinds: approval_needed, question, finished, failed, pr_opened, owner_note.
"""

from __future__ import annotations

import contextlib
import datetime
import difflib
import fcntl
import json
import os
import pwd
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata

VERSION = "0.11.0"
PROGRAM = "switchboard-hook"
SPOOL_PARTS = (".local", "state", "switchboard-hook", "events")
"""The spool, under HOME (the dispatcher computes the same path)."""
RATE_FILE = "notes-rate.json"
"""notify_owner's rate limit state, next to the spool directory."""
MAX_SPOOL_FILES = 200
MAX_EVENT_BYTES = 4096
MAX_STDIN_BYTES = 8 * 1024 * 1024
STDIN_SECONDS = 2.0
HARD_SECONDS = 5
MAX_MESSAGE = 200
MAX_NOTE = 500
MAX_NAME = 64
MAX_URL = 300
MAX_REGISTRY_FILES = 64
MAX_REGISTRY_BYTES = 64 * 1024
NOTES_PER_SESSION_HOUR = 5
NOTES_PER_MACHINE_HOUR = 20
HOUR_SECONDS = 3600
KINDS = ("approval_needed", "question", "finished", "failed", "pr_opened", "owner_note")
LEVELS = ("info", "warning", "urgent")
STOP_ERRORS = (
    "rate_limit",
    "authentication_failed",
    "oauth_org_not_allowed",
    "account_on_hold",
    "billing_error",
    "invalid_request",
    "model_not_found",
    "server_error",
    "max_output_tokens",
    "cloud_credential_error",
    "unknown",
)
"""StopFailure `error` values (Claude Code hooks reference); anything else is `unknown`."""
NOTIFICATION_KINDS = {
    "permission_prompt": "approval_needed",
    "idle_prompt": "question",
    "elicitation_dialog": "question",
    "agent_needs_input": "question",
}
"""Notification types that mean the owner is needed; the others (auth_success, ...) are ignored."""
EVENT_FILE_RE = re.compile(r"[0-9]{13}-[0-9]{10}-[0-9a-f]{8}\.json")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
PANE_ID_RE = re.compile(r"%[0-9]{1,6}")
GITHUB_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
GITHUB_REPO = r"[A-Za-z0-9._-]{1,100}"
PR_URL_RE = re.compile(r"https://github\.com/(" + GITHUB_NAME + r")/(" + GITHUB_REPO + r")/pull/([0-9]{1,9})")
PR_URL_SEARCH_RE = re.compile(PR_URL_RE.pattern + r"(?![A-Za-z0-9/._-])")
PR_REF_RE = re.compile(r"\b(" + GITHUB_NAME + r")/(" + GITHUB_REPO + r")#([0-9]{1,9})\b")
GITHUB_URL_RE = re.compile(
    r"https://github\.com/" + GITHUB_NAME + r"/" + GITHUB_REPO + r"(?:/[A-Za-z0-9._~%/#=?&+-]{0,200})?"
)
"""The only links notify_owner accepts: GitHub pages (a pull request, an issue, a run, a commit)."""
GH_PR_RE = re.compile(r"(?:^|[\s;&|(])gh\s+pr\s+(create|ready)\b")
REDACTED = "[secret]"
_INVISIBLE = frozenset(
    [0x061C, 0x200B, 0x200E, 0x200F, 0x2028, 0x2029, 0xFEFF, *range(0x202A, 0x202F), *range(0x2060, 0x206A)]
)
_KEY_WORDS = r"(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential)"
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_\w{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[\w-]{10,}"),
    re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I),
    re.compile(r"(?i)\b[\w.-]*" + _KEY_WORDS + r"[\w.-]*[\"']?\s*[=:]\s*\S+"),
    re.compile(r"(?<![A-Za-z0-9+/=_-])(?=[A-Za-z0-9+/=_-]*[0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])"),
)
"""A best-effort subset of the hub's redaction (`remote/redact.py`), which runs again on the hub."""

MCP_SERVER_NAME = "switchboard-notify"
MCP_TOOL_NAME = "notify_owner"
MCP_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MCP_LINE = 1024 * 1024
HOOK_EVENTS = ("Notification", "Stop", "StopFailure", "PostToolUse")
HOOK_MATCHERS = {"Notification": "|".join(NOTIFICATION_KINDS), "PostToolUse": "Bash"}
HOOK_TIMEOUT_SECONDS = 10
COMMAND_PATH_RE = re.compile(r"/[A-Za-z0-9._/+-]{1,255}")
ALLOW_RULE = "mcp__" + MCP_SERVER_NAME + "__" + MCP_TOOL_NAME


# --- text --------------------------------------------------------------------------------------


def clean(value: object, limit: int) -> str:
    """One line without control, format (bidi, zero-width), surrogate or private-use characters,
    secrets replaced, at most `limit` characters (cut with an ellipsis)."""
    if not isinstance(value, str):
        return ""
    kept = []
    for char in value[: limit * 4 + 64]:
        if char in "\r\n\t":
            kept.append(" ")
        elif ord(char) in _INVISIBLE or unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co"):
            continue
        else:
            kept.append(char)
    text = " ".join("".join(kept).split())
    for pattern in _SECRETS:
        text = pattern.sub(REDACTED, text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def pr_url(value: object) -> str | None:
    """A pull request URL `https://github.com/<owner>/<repo>/pull/<n>`, else None."""
    if isinstance(value, str) and len(value) <= MAX_URL and PR_URL_RE.fullmatch(value):
        return value
    return None


def github_url(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= MAX_URL and GITHUB_URL_RE.fullmatch(value):
        return value
    return None


def find_pr_url(text: str) -> str | None:
    found = PR_URL_SEARCH_RE.search(text)
    if found is not None:
        return pr_url(found.group(0))
    ref = PR_REF_RE.search(text)
    if ref is not None:
        return pr_url(f"https://github.com/{ref.group(1)}/{ref.group(2)}/pull/{ref.group(3)}")
    return None


# --- paths -------------------------------------------------------------------------------------


def home_dir(env: dict[str, str]) -> str:
    home = env.get("HOME")
    if home and os.path.isabs(home):
        return home
    return pwd.getpwuid(os.getuid()).pw_dir


def spool_dir(env: dict[str, str]) -> str:
    return os.path.join(home_dir(env), *SPOOL_PARTS)


def claude_home(env: dict[str, str]) -> str:
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured and os.path.isabs(configured):
        return configured
    return os.path.join(home_dir(env), ".claude")


def _private_dir(path: str) -> bool:
    """True when `path` is a real directory of this user that nobody else can enter."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0


def ensure_spool(env: dict[str, str]) -> str | None:
    """The spool directory (created 0700 with its parent), or None when it is not safe to use."""
    path = spool_dir(env)
    parent = os.path.dirname(path)
    for directory in (parent, path):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            return None
        if not _private_dir(directory):
            return None
    return path


def spooled(path: str) -> list[str]:
    try:
        return sorted(name for name in os.listdir(path) if EVENT_FILE_RE.fullmatch(name))
    except OSError:
        return []


def write_event(path: str, event: dict[str, object], now_ms: int) -> str | None:
    """Writes one event file (0600, by rename so a reader never sees half of it); its name, or
    None when the spool is full or the disk refused it."""
    if len(spooled(path)) >= MAX_SPOOL_FILES:
        return None
    data = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_EVENT_BYTES:
        return None
    name = f"{now_ms:013d}-{os.getpid() % 10**10:010d}-{secrets.token_hex(4)}.json"
    temporary = os.path.join(path, "." + name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.rename(temporary, os.path.join(path, name))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return None
    return name


# --- sessions ----------------------------------------------------------------------------------


def _registry_entries(env: dict[str, str]) -> list[dict[str, object]]:
    directory = os.path.join(claude_home(env), "sessions")
    try:
        names = sorted(name for name in os.listdir(directory) if re.fullmatch(r"[0-9]{1,10}\.json", name))
    except OSError:
        return []
    entries = []
    for name in names[:MAX_REGISTRY_FILES]:
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                raw = handle.read(MAX_REGISTRY_BYTES + 1)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        if len(raw) <= MAX_REGISTRY_BYTES and isinstance(data, dict):
            data.setdefault("pid", int(name.split(".")[0]))
            entries.append(data)
    return entries


def session_name(env: dict[str, str], session_id: str | None) -> str | None:
    """The Claude Code session's name from its registry entry (the session can set it itself, so
    it is shown as the session's own words)."""
    if session_id is None:
        return None
    for entry in _registry_entries(env):
        if entry.get("sessionId") == session_id:
            return clean(entry.get("name"), MAX_NAME) or None
    return None


def _parent(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            text = handle.read().decode("ascii", "replace")
        return int(text[text.rfind(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def session_of_process(env: dict[str, str]) -> tuple[str | None, str | None]:
    """(session id, name) of the Claude Code process this one runs under: the registry is keyed by
    pid, so the parent (or its parent, when a shell started us) is looked up."""
    by_pid: dict[int, dict[str, object]] = {}
    for entry in _registry_entries(env):
        number = entry.get("pid")
        if isinstance(number, int):
            by_pid[number] = entry
    pid: int | None = os.getppid()
    for _ in range(3):
        if pid is None or pid <= 1:
            break
        found = by_pid.get(pid)
        if found is not None:
            session_id = found.get("sessionId")
            if isinstance(session_id, str) and UUID_RE.fullmatch(session_id):
                return session_id, clean(found.get("name"), MAX_NAME) or None
            break
        pid = _parent(pid)
    return None, None


# --- the hook ----------------------------------------------------------------------------------


def read_stdin(fd: int, limit: int = MAX_STDIN_BYTES, seconds: float = STDIN_SECONDS) -> bytes | None:
    """At most `limit` bytes within `seconds`; None when there is more or it takes longer."""
    deadline = time.monotonic() + seconds
    chunks: list[bytes] = []
    size = 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            ready, _, _ = select.select([fd], [], [], left)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return None
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def _base(payload: dict[str, object], env: dict[str, str], kind: str, now_ms: int) -> dict[str, object]:
    session_id = payload.get("session_id")
    sid = session_id if isinstance(session_id, str) and UUID_RE.fullmatch(session_id) else None
    cwd = payload.get("cwd")
    project = clean(os.path.basename(cwd.rstrip("/")), MAX_NAME) if isinstance(cwd, str) else ""
    pane = env.get("TMUX_PANE", "")
    return {
        "v": 1,
        "kind": kind,
        "time": now_ms,
        "sessionId": sid,
        "sessionName": session_name(env, sid),
        "project": project or None,
        "pane": pane if PANE_ID_RE.fullmatch(pane) else None,
        "message": None,
        "url": None,
        "level": None,
    }


def event_from_payload(payload: object, env: dict[str, str], now_ms: int) -> dict[str, object] | None:
    """The event for one hook payload, or None when the hook is not one the owner is told about."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("hook_event_name")
    if payload.get("agent_id") is not None:
        # A subagent's hook: its parent session reports for it.
        return None
    if name == "Notification":
        kind = NOTIFICATION_KINDS.get(str(payload.get("notification_type")))
        if kind is None:
            return None
        event = _base(payload, env, kind, now_ms)
        event["message"] = clean(payload.get("message"), MAX_MESSAGE) or None
        return event
    if name == "Stop":
        if payload.get("stop_hook_active") is True:
            # Claude is continuing because a stop hook asked it to: not the end of the turn.
            return None
        return _base(payload, env, "finished", now_ms)
    if name == "StopFailure":
        error = payload.get("error")
        event = _base(payload, env, "failed", now_ms)
        event["message"] = error if isinstance(error, str) and error in STOP_ERRORS else "unknown"
        return event
    if name == "PostToolUse":
        if payload.get("tool_name") != "Bash":
            return None
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return None
        verb = GH_PR_RE.search(command)
        if verb is None:
            return None
        response = payload.get("tool_response")
        stdout = response.get("stdout") if isinstance(response, dict) else response
        url = find_pr_url(stdout[:65536]) if isinstance(stdout, str) else None
        if url is None:
            return None
        event = _base(payload, env, "pr_opened", now_ms)
        event["message"] = "gh pr " + verb.group(1)
        event["url"] = url
        return event
    return None


def _give_up(_signum: int, _frame: object) -> None:
    os._exit(0)


def run_hook(env: dict[str, str], stdin_fd: int, now_ms: int | None = None) -> int:
    """The `hook` command. Always 0, prints nothing: Claude Code must never wait on it or act on it."""
    try:
        raw = read_stdin(stdin_fd)
        if not raw:
            return 0
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return 0
        moment = now_ms if now_ms is not None else int(time.time() * 1000)
        event = event_from_payload(payload, env, moment)
        if event is None:
            return 0
        path = ensure_spool(env)
        if path is not None:
            write_event(path, event, moment)
    except Exception:
        pass
    return 0


# --- notify_owner ------------------------------------------------------------------------------


def _take_note_slot(env: dict[str, str], key: str, now: float) -> bool:
    """Counts one note for `key` (the session) under a file lock; False beyond the limits."""
    if ensure_spool(env) is None:
        return False
    path = os.path.join(os.path.dirname(spool_dir(env)), RATE_FILE)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return False
    try:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.02)
        raw = os.read(fd, 65536)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            data = {}
        notes = data.get("notes") if isinstance(data, dict) else None
        recent = [
            n
            for n in (notes if isinstance(notes, list) else [])
            if isinstance(n, list)
            and len(n) == 2
            and isinstance(n[0], str)
            and isinstance(n[1], (int, float))
            and now - HOUR_SECONDS < n[1] <= now
        ][-NOTES_PER_MACHINE_HOUR:]
        if len(recent) >= NOTES_PER_MACHINE_HOUR or sum(1 for n in recent if n[0] == key) >= NOTES_PER_SESSION_HOUR:
            return False
        recent.append([key, now])
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"notes": recent}, separators=(",", ":")).encode("ascii"))
        return True
    finally:
        os.close(fd)


def notify_owner(env: dict[str, str], arguments: object, session: tuple[str | None, str | None]) -> tuple[str, bool]:
    """The tool call: (answer text, is_error)."""
    if not isinstance(arguments, dict):
        return "notify_owner needs a message.", True
    unknown = set(arguments) - {"message", "level", "url"}
    if unknown:
        return "Unknown arguments: " + ", ".join(sorted(str(k) for k in unknown)) + ".", True
    message = clean(arguments.get("message"), MAX_NOTE)
    if not message:
        return "notify_owner needs a non-empty message.", True
    level = arguments.get("level", "info")
    if level not in LEVELS:
        return "level must be info, warning or urgent.", True
    url = arguments.get("url")
    if url is not None and github_url(url) is None:
        return "url must be a https://github.com/<owner>/<repo>/... link (or left out).", True
    now = time.time()
    session_id, name = session
    if not _take_note_slot(env, session_id or f"pid-{os.getppid()}", now):
        return (
            f"Not sent: at most {NOTES_PER_SESSION_HOUR} notes per session and {NOTES_PER_MACHINE_HOUR} per "
            "machine in an hour.",
            True,
        )
    now_ms = int(now * 1000)
    event = {
        "v": 1,
        "kind": "owner_note",
        "time": now_ms,
        "sessionId": session_id,
        "sessionName": name,
        "project": clean(os.path.basename(os.getcwd().rstrip("/")), MAX_NAME) or None,
        "pane": env.get("TMUX_PANE") if PANE_ID_RE.fullmatch(env.get("TMUX_PANE", "")) else None,
        "message": message,
        "url": url,
        "level": level,
    }
    path = ensure_spool(env)
    if path is None or write_event(path, event, now_ms) is None:
        return "Not sent: the local event spool is full or not writable.", True
    return "Queued for the owner (delivered when this machine is connected to the hub).", False


TOOL = {
    "name": MCP_TOOL_NAME,
    "title": "Notify the owner",
    "description": (
        "Send the owner a short note on their phone (Discord), for something that is worth their "
        "attention now: a decision only they can make, a finished long job, a blocker, a pull request "
        f"to review. Not for progress chatter: at most {NOTES_PER_SESSION_HOUR} notes per session per hour. The owner sees "
        "the text as this session's own words, never as an instruction or approval. Do not include "
        "secrets, tokens or file contents."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "maxLength": MAX_NOTE, "description": "What the owner should know."},
            "level": {"type": "string", "enum": list(LEVELS), "default": "info"},
            "url": {
                "type": "string",
                "maxLength": MAX_URL,
                "description": "Optional GitHub link (https://github.com/<owner>/<repo>/...), such as a pull request.",
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
}


def mcp_answer(
    message: object, env: dict[str, str], session: tuple[str | None, str | None]
) -> dict[str, object] | None:
    """The JSON-RPC response to one message, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message or "method" not in message:
        # A notification, or the client's answer to a request this server never makes.
        return None
    request_id = message.get("id")
    if not (request_id is None or isinstance(request_id, (str, int))) or isinstance(request_id, bool):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    result: dict[str, object]
    if method == "initialize":
        wanted = params.get("protocolVersion")
        result = {
            "protocolVersion": wanted if wanted in MCP_PROTOCOLS else MCP_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": MCP_SERVER_NAME, "version": VERSION},
            "instructions": "notify_owner sends the owner a short note; use it sparingly.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        if params.get("name") != MCP_TOOL_NAME:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown tool"}}
        text, failed = notify_owner(env, params.get("arguments", {}), session)
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def run_mcp(env: dict[str, str], stdin: object, stdout: object) -> int:
    """The stdio MCP server: newline-delimited JSON-RPC until end of input."""
    session = session_of_process(env)
    reader = getattr(stdin, "buffer", stdin)
    writer = getattr(stdout, "buffer", stdout)
    while True:
        line = reader.readline(MAX_MCP_LINE + 1)  # type: ignore[attr-defined]
        if not line:
            return 0
        if len(line) > MAX_MCP_LINE:
            return 1
        if not line.strip():
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            answer: dict[str, object] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            answer = mcp_answer(message, env, session)
        if answer is not None:
            writer.write((json.dumps(answer, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii"))  # type: ignore[attr-defined]
            writer.flush()  # type: ignore[attr-defined]


# --- installer ---------------------------------------------------------------------------------


class InstallError(Exception):
    pass


def hook_command(python: str, script: str, mode: str) -> str:
    for path in (python, script):
        if not COMMAND_PATH_RE.fullmatch(path) or ".." in path.split("/"):
            raise InstallError("not an absolute path of letters, digits and ._/+-: " + path)
    return " ".join([shlex.quote(python), "-I", shlex.quote(script), mode])


def is_ours(handler: object) -> bool:
    """A hook handler this program installed: a command ending in `switchboard-hook hook`."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return len(tokens) >= 2 and tokens[-1] == "hook" and os.path.basename(tokens[-2]) == PROGRAM


def _without_ours(groups: list[object]) -> list[object]:
    kept: list[object] = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            handlers = [h for h in group["hooks"] if not is_ours(h)]
            if not handlers and group["hooks"]:
                continue
            if len(handlers) != len(group["hooks"]):
                kept.append(dict(group, hooks=handlers))
                continue
        kept.append(group)
    return kept


def merged_settings(settings: dict[str, object], command: str | None, allow_tool: bool | None) -> dict[str, object]:
    """`settings` with this program's hooks replaced by fresh ones (`command`), or removed (None).
    Every other key and every other hook stays as it is. `allow_tool` True adds the allow rule of
    notify_owner, False removes it, None leaves permissions alone."""
    out: dict[str, object] = json.loads(json.dumps(settings))
    hooks = out.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("settings: hooks is not an object; fix it by hand first")
    for event in HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise InstallError("settings: hooks." + event + " is not a list; fix it by hand first")
        groups = _without_ours(groups)
        if command is not None:
            group: dict[str, object] = {}
            if event in HOOK_MATCHERS:
                group["matcher"] = HOOK_MATCHERS[event]
            group["hooks"] = [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS, "async": True}]
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    if allow_tool is not None:
        permissions = out.get("permissions", {})
        if not isinstance(permissions, dict):
            raise InstallError("settings: permissions is not an object; fix it by hand first")
        allow = permissions.get("allow", [])
        if not isinstance(allow, list):
            raise InstallError("settings: permissions.allow is not a list; fix it by hand first")
        allow = [rule for rule in allow if rule != ALLOW_RULE]
        if allow_tool:
            allow.append(ALLOW_RULE)
        if allow:
            permissions["allow"] = allow
        else:
            permissions.pop("allow", None)
        if permissions:
            out["permissions"] = permissions
        else:
            out.pop("permissions", None)
    return out


def _dump(settings: dict[str, object]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def update_settings(path: str, command: str | None, allow_tool: bool | None, dry_run: bool, out: object) -> bool:
    """Rewrites the settings file when it changes (after a backup); True when it changed."""
    try:
        with open(path, "rb") as handle:
            before_raw = handle.read()
        existed = True
    except FileNotFoundError:
        before_raw, existed = b"", False
    if existed:
        try:
            settings = json.loads(before_raw.decode("utf-8")) if before_raw.strip() else {}
        except (UnicodeDecodeError, ValueError):
            raise InstallError(path + " is not valid JSON; fix it by hand first") from None
        if not isinstance(settings, dict):
            raise InstallError(path + " is not a JSON object")
    else:
        settings = {}
    after = merged_settings(settings, command, allow_tool)
    if after == settings:
        out.write(path + ": no change\n")  # type: ignore[attr-defined]
        return False
    before_text = _dump(settings) if existed else ""
    diff = difflib.unified_diff(
        before_text.splitlines(True), _dump(after).splitlines(True), fromfile=path, tofile=path + " (new)"
    )
    out.write("".join(diff))  # type: ignore[attr-defined]
    if dry_run:
        out.write("(dry run: nothing written)\n")  # type: ignore[attr-defined]
        return True
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    mode = 0o600
    if existed:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path + ".switchboard-backup-" + stamp + "-" + secrets.token_hex(3)
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(before_raw)
        out.write("backup: " + backup + "\n")  # type: ignore[attr-defined]
    temporary = path + ".switchboard-tmp-" + secrets.token_hex(4)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_dump(after))
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    out.write("written: " + path + "\n")  # type: ignore[attr-defined]
    return True


def _run_claude(claude: str, args: list[str], dry_run: bool, out: object, env: dict[str, str]) -> bool:
    out.write("$ " + " ".join(shlex.quote(a) for a in [claude, *args]) + "\n")  # type: ignore[attr-defined]
    if dry_run:
        return True
    try:
        done = subprocess.run(
            [claude, *args],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        out.write("  failed: " + type(err).__name__ + "\n")  # type: ignore[attr-defined]
        return False
    return done.returncode == 0


def update_mcp(claude: str | None, command: list[str] | None, dry_run: bool, out: object, env: dict[str, str]) -> bool:
    """Replaces (or with command None removes) the user-scope MCP server through `claude mcp`."""
    if claude is None:
        out.write("claude is not on PATH; run this yourself:\n")  # type: ignore[attr-defined]
        if command is not None:
            args = ["mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", *command]
        else:
            args = ["mcp", "remove", "--scope", "user", MCP_SERVER_NAME]
        out.write("  claude " + " ".join(shlex.quote(a) for a in args) + "\n")  # type: ignore[attr-defined]
        return False
    if dry_run:
        out.write("(dry run: claude mcp not run)\n")  # type: ignore[attr-defined]
    # Removing first makes `install` idempotent; it fails harmlessly when the server is not there.
    removed = _run_claude(claude, ["mcp", "remove", "--scope", "user", MCP_SERVER_NAME], dry_run, out, env)
    if command is None:
        return removed
    return _run_claude(claude, ["mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", *command], dry_run, out, env)


def _option(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 >= len(argv):
            raise InstallError(name + " needs a value")
        return argv[index + 1]
    return None


INSTALL_FLAGS = ("--dry-run", "--no-mcp", "--allow-tool")
INSTALL_VALUES = ("--settings", "--python", "--script", "--claude")


def run_install(argv: list[str], env: dict[str, str], out: object, err: object, *, uninstall: bool) -> int:
    try:
        index = 0
        while index < len(argv):
            if argv[index] in INSTALL_VALUES:
                index += 2
            elif argv[index] in INSTALL_FLAGS:
                index += 1
            else:
                raise InstallError("unknown option " + argv[index])
        if os.getuid() == 0:
            raise InstallError("run it as the user whose Claude Code sessions should notify, not as root")
        dry_run = "--dry-run" in argv
        settings = _option(argv, "--settings") or os.path.join(claude_home(env), "settings.json")
        python = _option(argv, "--python") or os.path.realpath(sys.executable)
        script = _option(argv, "--script") or os.path.realpath(os.path.abspath(__file__))
        command = None if uninstall else hook_command(python, script, "hook")
        allow: bool | None = None
        if uninstall:
            allow = False
        elif "--allow-tool" in argv:
            allow = True
        update_settings(settings, command, allow, dry_run, out)
        if "--no-mcp" not in argv:
            claude = _option(argv, "--claude") or shutil.which("claude", path=env.get("PATH"))
            mcp = None if uninstall else [python, "-I", script, "mcp"]
            if mcp is not None:
                hook_command(python, script, "mcp")
            update_mcp(claude, mcp, dry_run, out, {k: v for k, v in env.items() if k in ("PATH", "HOME", "LANG")})
    except (InstallError, OSError) as problem:
        err.write(PROGRAM + ": " + str(problem) + "\n")  # type: ignore[attr-defined]
        return 1
    return 0


USAGE = "usage: switchboard-hook hook | mcp | install [options] | uninstall [options] | version\n"


def main() -> int:
    argv = sys.argv[1:]
    env = dict(os.environ)
    command = argv[0] if argv else ""
    if command == "hook":
        signal.signal(signal.SIGALRM, _give_up)
        signal.alarm(HARD_SECONDS)
        return run_hook(env, sys.stdin.fileno())
    if command == "mcp":
        return run_mcp(env, sys.stdin, sys.stdout)
    if command in ("install", "uninstall"):
        return run_install(argv[1:], env, sys.stdout, sys.stderr, uninstall=command == "uninstall")
    if command == "version":
        sys.stdout.write(VERSION + "\n")
        return 0
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
