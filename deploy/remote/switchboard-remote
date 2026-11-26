#!/usr/bin/env python3
"""switchboard-remote: the only program the Switchboard hub runs on a machine.

Standard library only, Python 3.8 or newer, one file (remote machines may have an older Python
than the hub). `deploy/remote/switchboard-remote` is a byte-identical copy of this file;
`voice-coding-mcp remote-dispatcher` prints it. Design: docs/SWITCHBOARD.md, "The dispatcher".

Invocation, one of:

- Forced command (recommended), in the remote user's `~/.ssh/authorized_keys` for the hub's key:

    restrict,command="/usr/bin/python3 -I /home/USER/.local/bin/switchboard-remote --sessions 'api,web-*'" ssh-ed25519 ...

  sshd puts the hub's request into SSH_ORIGINAL_COMMAND: `switchboard-remote <verb> [args]`. Policy
  options come only from this real argv, which the remote user owns.
- Explicit path: the hub runs `<path> <verb> [args]`. No options are accepted then; the policy
  comes from `~/.config/switchboard-remote/policy.json` (without it everything is refused). Not a
  security boundary: a key without `command=` opens a shell.
- Local transport: `python3 -I <this file> --policy-from-hub <policy options> -- <verb> [args]`,
  started by the hub as a child process (refused under sshd).

Policy options: --sessions <globs> (comma separated, required), --allow-usage, --allow-send,
--allow-permission-answers, --send-to-any-pane, --slash-commands <names>, --claude-home <path>.
Test only (refused unless SWITCHBOARD_REMOTE_TEST=1 is in the environment): --tmux-socket-name.

Verbs: `version`, `list`, `capture <paneId> <lines>`, `send <paneId> <panePid> <sessionId|none>` (text
on stdin, only with --allow-send). Every response is one JSON document on stdout,
`{"protocol": 1, "ok": true, ...}` or `{"protocol": 1, "ok": false, "error": "<code>"}`; the exit
code is 0 also for refusals. Nothing from the request ever reaches a shell: tmux runs from argv with
a fixed environment, and the text of a send reaches tmux only on the stdin of `load-buffer`.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import pwd
import re
import secrets
import shlex
import subprocess
import sys
import unicodedata

VERSION = "0.2.0"
PROTOCOL = 1
VERBS = ("version", "list", "capture", "send")
PROGRAM = "switchboard-remote"

ARG_RE = re.compile(r"[A-Za-z0-9%_.-]{1,64}")
"""Every token of a request: shell-neutral in any login shell."""
PANE_ID_RE = re.compile(r"%[0-9]{1,6}")
DECIMAL_RE = re.compile(r"[0-9]{1,9}")
NUMBER_RE = re.compile(r"[0-9]{1,15}")
GLOB_RE = re.compile(r"[A-Za-z0-9_.*?-]{1,64}")
SLASH_COMMAND_RE = re.compile(r"[a-z][a-z0-9:-]{0,31}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,255}")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
REGISTRY_FILE_RE = re.compile(r"[0-9]+\.json")
TMUX_SOCKET_RE = re.compile(r"sb-test-[A-Za-z0-9]{1,32}")
SEND_NO_SESSION = "none"
"""The `sessionId` argument of a send to a pane that runs no Claude Code (`-` would read as an option)."""
PERMISSION_ANSWER_RE = re.compile(r"[1-9]")
"""The only text accepted for a pane that shows a permission dialog (a choice number)."""

MAX_SESSION_GLOBS = 64
MAX_LINES = 500
MAX_PANES = 512
MAX_CAPTURE_BYTES = 64 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024
MAX_SEND_BYTES = 8 * 1024
SEND_CHECK_LINES = 40
"""Screen lines a send reads for the permission, draft and vim checks (the last 15 non-empty count)."""
CHECK_TAIL = 15
NAME_MAX = 64
WAITING_MAX = 200
ANCESTOR_STEPS = 32
TMUX_TIMEOUT_SECONDS = 8
TMUX_PATH = "/usr/local/bin:/usr/bin:/bin"
POLICY_FILE = os.path.join(".config", "switchboard-remote", "policy.json")
"""Relative to HOME."""
TEST_FLAG = "SWITCHBOARD_REMOTE_TEST"
TEST_TMUX_BIN = "SWITCHBOARD_REMOTE_TEST_TMUX"

LIST_FIELDS = (
    "session_name",
    "session_id",
    "window_index",
    "window_id",
    "window_name",
    "pane_index",
    "pane_id",
    "pane_pid",
    "pane_current_command",
    "pane_active",
    "session_attached",
    "window_activity",
)
LIST_FORMAT = "\t".join("#{" + name + "}" for name in LIST_FIELDS)

FLAG_OPTIONS = ("--allow-usage", "--allow-send", "--allow-permission-answers", "--send-to-any-pane")
VALUE_OPTIONS = ("--sessions", "--slash-commands", "--claude-home", "--tmux-socket-name")

_INVISIBLE = frozenset(
    [0x061C, 0x200B, 0x200E, 0x200F, 0x2028, 0x2029, 0xFEFF, *range(0x202A, 0x202F), *range(0x2060, 0x206A)]
)


class RefusedError(Exception):
    """A refusal with its error code, answered as `{"ok": false, "error": code}`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Policy:
    """What this machine allows, from the forced command, the hub (local) or the policy file."""

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.allow_usage = False
        self.allow_send = False
        self.allow_permission_answers = False
        self.send_to_any_pane = False
        self.slash_commands: list[str] = []
        self.claude_home: str | None = None
        self.tmux_socket_name: str | None = None

    def allows(self, session_name: str) -> bool:
        return any(fnmatch.fnmatchcase(session_name, glob) for glob in self.sessions)


# --- text ------------------------------------------------------------------------------------


def clean_text(value: object, limit: int) -> str:
    """Text a pane program or session controls: control, format (bidi, zero-width), surrogate and
    private-use characters dropped, at most `limit` characters."""
    if not isinstance(value, str):
        return ""
    out = []
    for char in value:
        if ord(char) in _INVISIBLE or unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co"):
            continue
        out.append(char)
        if len(out) >= limit:
            break
    return "".join(out)


def text_problem(text: str, *, claude_pane: bool, slash_commands: list[str]) -> str | None:
    """Why `text` may not be typed into a pane, or None. The same rules run on the hub (which turns the
    reason into words) and here (any reason is `bad_text`):

    - `empty`: nothing but whitespace; `not_nfc`: not in Unicode NFC;
    - `tab`: a tab (completes or switches modes in Claude Code and in shells);
    - `newline`: a newline into a pane that runs no Claude Code (a shell runs every line);
    - `control`: other C0 or C1 controls (escape sequences could end the bracketed paste), U+2028,
      U+2029, surrogates and private-use characters; `format`: Unicode format characters (bidi
      controls, zero-width characters);
    - `bang`, `hash`: starts (after leading whitespace) with `!` (Claude Code's bash mode runs it
      outside its permission checks) or `#` (memory shortcut);
    - `slash`: starts with `/` and is not one of `slash_commands`.
    """
    if not text.strip():
        return "empty"
    if unicodedata.normalize("NFC", text) != text:
        return "not_nfc"
    for char in text:
        if char == "\t":
            return "tab"
        if char == "\n":
            if not claude_pane:
                return "newline"
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Cs", "Co", "Zl", "Zp"):
            return "control"
        if category == "Cf" or ord(char) in _INVISIBLE:
            return "format"
    start = text.lstrip()
    if start.startswith("!"):
        return "bang"
    if start.startswith("#"):
        return "hash"
    if start.startswith("/"):
        found = re.match(r"/([a-z][a-z0-9:-]{0,31})(?=\s|$)", start)
        if found is None or found.group(1) not in slash_commands:
            return "slash"
    return None


_BOX_EDGES = "│┃║|"
_DIALOG_QUESTION_RE = re.compile(r"do you want to\b[^?]{0,200}\?", re.I)
_YES_OPTION_RE = re.compile(r"(?:[❯>›▶]\s*)?1[.)]\s*yes\b", re.I)
_NO_OPTION_RE = re.compile(r"(?:[❯>›▶]\s*)?[2-9][.)]\s*no\b", re.I)
_INPUT_PROMPTS = ("❯", ">")
_PLACEHOLDER_RE = re.compile(r'try "', re.I)
"""Claude Code's empty input box shows a hint such as `> Try "fix lint errors"` (dim, which a plain
capture loses); it is not a draft."""
_VIM_NORMAL_RE = re.compile(r"-- NORMAL --")


def _screen_tail(screen: str) -> list[str]:
    """The last CHECK_TAIL non-empty lines, without surrounding whitespace and box edges."""
    lines = [line.strip().strip(_BOX_EDGES).strip() for line in screen.split("\n")]
    return [line for line in lines if line][-CHECK_TAIL:]


def shows_dialog(screen: str) -> bool:
    """A permission dialog may be on the screen: a line that starts with "Do you want to ...?", or a
    `1. Yes` option followed within 6 lines by a `2.`..`9.` `No` option. Broader than the hub's
    heuristic on purpose: a missed dialog would let Enter select its highlighted option."""
    tail = _screen_tail(screen)
    for index, line in enumerate(tail):
        if _DIALOG_QUESTION_RE.match(line):
            return True
        if _YES_OPTION_RE.match(line) and any(_NO_OPTION_RE.match(other) for other in tail[index + 1 : index + 7]):
            return True
    return False


def shows_draft(screen: str) -> bool:
    """Text is already typed in Claude Code's input box: the last input line (`>` or `❯`) has text
    after the prompt that is not the empty box's hint. A heuristic; it refuses rather than submit the
    owner's unsent draft together with the send."""
    tail = _screen_tail(screen)
    for line in reversed(tail):
        if line.startswith(_INPUT_PROMPTS):
            typed = line[1:].strip()
            return bool(typed) and not _PLACEHOLDER_RE.match(typed)
    return False


def shows_vim_normal(screen: str) -> bool:
    """Claude Code's vim mode in NORMAL state: pasted text would be read as commands."""
    return any(_VIM_NORMAL_RE.search(line) for line in _screen_tail(screen))


# --- options and policy ----------------------------------------------------------------------


def home_dir(env: dict[str, str]) -> str:
    """HOME of the environment the dispatcher was started with, else the passwd entry's."""
    home = env.get("HOME")
    if home and os.path.isabs(home):
        return home
    return pwd.getpwuid(os.getuid()).pw_dir


def _split_list(value: str, pattern: re.Pattern[str], option: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    for item in items:
        if not pattern.fullmatch(item):
            raise RefusedError("bad_policy")
    if option == "--sessions" and not 1 <= len(items) <= MAX_SESSION_GLOBS:
        raise RefusedError("bad_policy")
    return items


def parse_options(argv: list[str], env: dict[str, str]) -> tuple[dict[str, object], bool, list[str]]:
    """Leading options, whether `--policy-from-hub` was given, and the remaining tokens (after an
    optional `--`). Raises RefusedError("bad_policy") for unknown or malformed options."""
    options: dict[str, object] = {}
    from_hub = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        if token == "--policy-from-hub":
            from_hub = True
        elif token in FLAG_OPTIONS:
            options[token] = True
        elif token in VALUE_OPTIONS:
            if index + 1 >= len(argv) or token in options:
                raise RefusedError("bad_policy")
            options[token] = argv[index + 1]
            index += 1
        else:
            raise RefusedError("bad_policy")
        index += 1
    if "--tmux-socket-name" in options and env.get(TEST_FLAG) != "1":
        raise RefusedError("bad_policy")
    return options, from_hub, argv[index:]


def policy_from_options(options: dict[str, object]) -> Policy:
    policy = Policy()
    sessions = options.get("--sessions")
    if not isinstance(sessions, str):
        raise RefusedError("bad_policy")
    policy.sessions = _split_list(sessions, GLOB_RE, "--sessions")
    policy.allow_usage = options.get("--allow-usage") is True
    policy.allow_send = options.get("--allow-send") is True
    policy.allow_permission_answers = options.get("--allow-permission-answers") is True
    policy.send_to_any_pane = options.get("--send-to-any-pane") is True
    slash = options.get("--slash-commands")
    if isinstance(slash, str):
        policy.slash_commands = _split_list(slash, SLASH_COMMAND_RE, "--slash-commands")
    home = options.get("--claude-home")
    if isinstance(home, str):
        if not PATH_RE.fullmatch(home) or ".." in home.split("/"):
            raise RefusedError("bad_policy")
        policy.claude_home = home
    socket_name = options.get("--tmux-socket-name")
    if isinstance(socket_name, str):
        if not TMUX_SOCKET_RE.fullmatch(socket_name):
            raise RefusedError("bad_policy")
        policy.tmux_socket_name = socket_name
    return policy


def policy_from_file(path: str) -> Policy:
    """The explicit-path policy file (camelCase keys, same meaning as the options). Missing or
    invalid: RefusedError("no_policy")."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise RefusedError("no_policy") from None
    if len(raw) > MAX_POLICY_BYTES or not isinstance(data, dict):
        raise RefusedError("no_policy")
    known = {
        "sessions": "--sessions",
        "allowUsage": "--allow-usage",
        "allowSend": "--allow-send",
        "allowPermissionAnswers": "--allow-permission-answers",
        "sendToAnyPane": "--send-to-any-pane",
        "slashCommands": "--slash-commands",
        "claudeHome": "--claude-home",
    }
    options: dict[str, object] = {}
    for key, value in data.items():
        option = known.get(key)
        if option is None:
            raise RefusedError("no_policy")
        if option in FLAG_OPTIONS:
            if not isinstance(value, bool):
                raise RefusedError("no_policy")
            options[option] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            options[option] = ",".join(value)
        elif isinstance(value, str):
            options[option] = value
        else:
            raise RefusedError("no_policy")
    try:
        return policy_from_options(options)
    except RefusedError:
        raise RefusedError("no_policy") from None


def resolve_request(argv: list[str], env: dict[str, str]) -> tuple[Policy, list[str]]:
    """The policy and the request tokens (`<verb> [args]`) for this invocation."""
    options, from_hub, rest = parse_options(argv, env)
    if from_hub:
        if "SSH_ORIGINAL_COMMAND" in env or "SSH_CONNECTION" in env:
            raise RefusedError("bad_policy")
        return policy_from_options(options), rest
    if rest:
        # Explicit path: the hub sent the whole command line, so it may carry no options.
        if options:
            raise RefusedError("bad_request")
        return policy_from_file(os.path.join(home_dir(env), POLICY_FILE)), rest
    policy = policy_from_options(options)
    command = env.get("SSH_ORIGINAL_COMMAND")
    if command is None:
        raise RefusedError("bad_request")
    try:
        tokens = shlex.split(command)
    except ValueError:
        raise RefusedError("bad_request") from None
    if not tokens or tokens[0] != PROGRAM:
        raise RefusedError("bad_request")
    return policy, tokens[1:]


def validate_request(tokens: list[str]) -> tuple[str, list[str]]:
    if not tokens or any(not ARG_RE.fullmatch(token) for token in tokens):
        raise RefusedError("bad_request")
    verb, args = tokens[0], tokens[1:]
    if verb not in VERBS:
        raise RefusedError("bad_request")
    if verb in ("version", "list") and args:
        raise RefusedError("bad_request")
    if verb == "capture":
        if len(args) != 2 or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if not 1 <= int(args[1]) <= MAX_LINES:
            raise RefusedError("bad_request")
    if verb == "send":
        if len(args) != 3 or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if args[2] != SEND_NO_SESSION and not UUID_RE.fullmatch(args[2]):
            raise RefusedError("bad_request")
    return verb, args


# --- tmux ------------------------------------------------------------------------------------


def tmux_env(env: dict[str, str]) -> dict[str, str]:
    """A fixed environment, so nothing from the session environment chooses which tmux runs."""
    out = {"PATH": TMUX_PATH, "LC_ALL": "C.UTF-8"}
    if "TMUX_TMPDIR" in env:
        out["TMUX_TMPDIR"] = env["TMUX_TMPDIR"]
    if "HOME" in env:
        out["HOME"] = env["HOME"]
    if env.get(TEST_FLAG) == "1":
        out.update({key: value for key, value in env.items() if key.startswith("FAKE_TMUX_")})
    return out


def run_tmux(args: list[str], policy: Policy, env: dict[str, str], stdin: bytes | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of one tmux call; argv only, no shell, stdin closed unless `stdin`
    is given (the text of a send, for `load-buffer -`)."""
    binary = "tmux"
    if env.get(TEST_FLAG) == "1" and env.get(TEST_TMUX_BIN):
        binary = env[TEST_TMUX_BIN]
    argv = [binary]
    if policy.tmux_socket_name:
        argv += ["-L", policy.tmux_socket_name]
    try:
        done = subprocess.run(
            argv + args,
            env=tmux_env(env),
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise RefusedError("tmux_failed") from None
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def _no_server(stderr: str) -> bool:
    text = stderr.lower()
    return "no server running" in text or "error connecting to" in text or "no sessions" in text


def list_panes(policy: Policy, env: dict[str, str]) -> list[dict[str, str]]:
    """Every pane of the tmux server as a dict of LIST_FIELDS; empty without a tmux server."""
    code, out, err = run_tmux(["list-panes", "-a", "-F", LIST_FORMAT], policy, env)
    if code != 0:
        if _no_server(err):
            return []
        raise RefusedError("tmux_failed")
    panes = []
    for line in out.split("\n"):
        fields = line.split("\t")
        if len(fields) != len(LIST_FIELDS):
            continue
        pane = dict(zip(LIST_FIELDS, fields))  # noqa: B905 (strict= needs Python 3.10)
        if not PANE_ID_RE.fullmatch(pane["pane_id"]) or not DECIMAL_RE.fullmatch(pane["pane_pid"]):
            continue
        panes.append(pane)
    return panes


# --- registry ----------------------------------------------------------------------------------


def claude_home(policy: Policy, env: dict[str, str]) -> str:
    if policy.claude_home:
        return policy.claude_home
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured and os.path.isabs(configured):
        return configured
    return os.path.join(home_dir(env), ".claude")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def read_registry(home: str) -> list[dict[str, object]]:
    """Live Claude Code sessions from `<home>/sessions/<pid>.json` (dead processes and entries
    without a UUID session id skipped; the key files next to them are never read)."""
    directory = os.path.join(home, "sessions")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    entries = []
    for name in names:
        if not REGISTRY_FILE_RE.fullmatch(name):
            continue
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                raw = handle.read(MAX_REGISTRY_BYTES + 1)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        if len(raw) > MAX_REGISTRY_BYTES or not isinstance(data, dict):
            continue
        pid = _number(data.get("pid"))
        if pid is None:
            pid = int(name.split(".")[0])
        session_id = data.get("sessionId")
        if pid <= 0 or not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id) or not _alive(pid):
            continue
        data["pid"] = pid
        entries.append(data)
    return entries


def parent_pid(pid: int, env: dict[str, str]) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read().decode("ascii", "replace")
        fields = stat[stat.rfind(")") + 2 :].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        pass
    if os.path.isdir("/proc"):
        return None
    try:
        done = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            env={"PATH": TMUX_PATH, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return int(done.stdout.decode("ascii", "replace").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def join_registry(
    panes: list[dict[str, str]], entries: list[dict[str, object]], env: dict[str, str]
) -> dict[str, dict[str, object]]:
    """Registry entry per pane id: from the entry's `tmux` field (`0:@0.%0`), else the pane whose
    pid is the nearest ancestor of the session's pid."""
    by_pid = {pane["pane_pid"]: pane["pane_id"] for pane in panes}
    pane_ids = {pane["pane_id"] for pane in panes}
    joined: dict[str, dict[str, object]] = {}
    for entry in entries:
        pane_id = None
        tmux = entry.get("tmux")
        if isinstance(tmux, str) and "." in tmux:
            candidate = tmux[tmux.rfind(".") + 1 :]
            if PANE_ID_RE.fullmatch(candidate) and candidate in pane_ids:
                pane_id = candidate
        if pane_id is None:
            pid: int | None = entry["pid"] if isinstance(entry["pid"], int) else None
            for _ in range(ANCESTOR_STEPS):
                if pid is None or pid <= 1:
                    break
                if str(pid) in by_pid:
                    pane_id = by_pid[str(pid)]
                    break
                pid = parent_pid(pid, env)
        if pane_id is not None and pane_id not in joined:
            joined[pane_id] = entry
    return joined


def claude_view(entry: dict[str, object]) -> dict[str, object]:
    """What leaves the machine of a registry entry: no paths, sockets or bridge ids."""
    cwd = entry.get("cwd")
    status = entry.get("status")
    return {
        "sessionId": entry.get("sessionId"),
        "name": clean_text(entry.get("name"), NAME_MAX) or None,
        "status": status if status in ("idle", "busy", "waiting") else None,
        "waitingFor": clean_text(entry.get("waitingFor"), WAITING_MAX) or None,
        "statusUpdatedAt": _number(entry.get("statusUpdatedAt")),
        "startedAt": _number(entry.get("startedAt")),
        "cwdName": clean_text(os.path.basename(cwd.rstrip("/")), NAME_MAX) or None if isinstance(cwd, str) else None,
    }


# --- verbs -----------------------------------------------------------------------------------


def verb_version(policy: Policy) -> dict[str, object]:
    return {
        "version": VERSION,
        "verbs": list(VERBS),
        "allowSend": policy.allow_send,
        "allowUsage": policy.allow_usage,
    }


def _flag(value: str) -> bool:
    return value not in ("", "0")


def _int(value: str) -> int | None:
    return int(value) if NUMBER_RE.fullmatch(value) else None


def verb_list(policy: Policy, env: dict[str, str]) -> dict[str, object]:
    panes = [pane for pane in list_panes(policy, env) if policy.allows(pane["session_name"])]
    truncated = len(panes) > MAX_PANES
    panes = panes[:MAX_PANES]
    joined = join_registry(panes, read_registry(claude_home(policy, env)), env) if panes else {}
    out = []
    for pane in panes:
        entry = joined.get(pane["pane_id"])
        out.append(
            {
                "session": clean_text(pane["session_name"], NAME_MAX),
                "sessionId": clean_text(pane["session_id"], NAME_MAX),
                "windowIndex": _int(pane["window_index"]),
                "windowId": clean_text(pane["window_id"], NAME_MAX),
                "windowName": clean_text(pane["window_name"], NAME_MAX),
                "paneIndex": _int(pane["pane_index"]),
                "paneId": pane["pane_id"],
                "panePid": int(pane["pane_pid"]),
                "command": clean_text(pane["pane_current_command"], NAME_MAX),
                "active": _flag(pane["pane_active"]),
                "attached": _flag(pane["session_attached"]),
                "activity": _int(pane["window_activity"]),
                "claude": claude_view(entry) if entry is not None else None,
            }
        )
    return {"panes": out, "truncated": truncated}


def verb_capture(policy: Policy, env: dict[str, str], pane_id: str, lines: int) -> dict[str, object]:
    pane = next((p for p in list_panes(policy, env) if p["pane_id"] == pane_id), None)
    # A pane outside the allowed sessions answers like a missing one, so names do not leak.
    if pane is None or not policy.allows(pane["session_name"]):
        raise RefusedError("gone")
    code, out, err = run_tmux(["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{lines}"], policy, env)
    if code != 0:
        raise RefusedError("gone" if "can't find" in err.lower() else "tmux_failed")
    data = out.encode("utf-8")
    if len(data) > MAX_CAPTURE_BYTES:
        out = data[-MAX_CAPTURE_BYTES:].decode("utf-8", "ignore")
    return {
        "text": out,
        "paneId": pane_id,
        "session": clean_text(pane["session_name"], NAME_MAX),
        "window": _int(pane["window_index"]),
    }


def read_send_text(stdin: object) -> str:
    """The text of a send: at most MAX_SEND_BYTES of UTF-8 from `stdin` (bytes or a binary file)."""
    if isinstance(stdin, bytes):
        raw = stdin[: MAX_SEND_BYTES + 1]
    elif stdin is not None and hasattr(stdin, "read"):
        try:
            raw = stdin.read(MAX_SEND_BYTES + 1)
        except (OSError, ValueError):
            raise RefusedError("bad_text") from None
    else:
        raw = b""
    if not isinstance(raw, bytes) or len(raw) > MAX_SEND_BYTES:
        raise RefusedError("bad_text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RefusedError("bad_text") from None


def _send_state(
    policy: Policy, env: dict[str, str], pane_id: str
) -> tuple[dict[str, str] | None, dict[str, object] | None, str]:
    """(the pane if it exists in an allowed session, its registry entry, its screen) right now."""
    panes = [p for p in list_panes(policy, env) if policy.allows(p["session_name"])]
    pane = next((p for p in panes if p["pane_id"] == pane_id), None)
    if pane is None:
        return None, None, ""
    entry = join_registry(panes, read_registry(claude_home(policy, env)), env).get(pane_id)
    code, out, _ = run_tmux(["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{SEND_CHECK_LINES}"], policy, env)
    if code != 0:
        raise RefusedError("tmux_failed")
    return pane, entry, out


def _asks_permission(entry: dict[str, object] | None, screen: str) -> bool:
    if entry is not None and entry.get("status") == "waiting":
        waiting_for = entry.get("waitingFor")
        if isinstance(waiting_for, str) and "permission" in waiting_for.lower():
            return True
    return shows_dialog(screen)


def verb_send(policy: Policy, env: dict[str, str], args: list[str], stdin: object) -> dict[str, object]:
    """Types the text from stdin into a pane and presses Enter, after rechecking everything the hub
    checked when it prepared the send. See docs/SWITCHBOARD.md, "The dispatcher"."""
    if not policy.allow_send:
        raise RefusedError("not_allowed")
    pane_id, pane_pid, session_id = args
    text = read_send_text(stdin)
    pane, entry, screen = _send_state(policy, env, pane_id)
    if pane is None:
        raise RefusedError("gone")
    if pane["pane_pid"] != pane_pid:
        raise RefusedError("changed")
    if session_id == SEND_NO_SESSION:
        if entry is not None:
            raise RefusedError("changed")
        if not policy.send_to_any_pane:
            raise RefusedError("not_claude")
    elif entry is None or entry.get("sessionId") != session_id:
        raise RefusedError("changed")
    if text_problem(text, claude_pane=entry is not None, slash_commands=policy.slash_commands) is not None:
        raise RefusedError("bad_text")
    permission_answer = False
    if _asks_permission(entry, screen):
        if not policy.allow_permission_answers or not PERMISSION_ANSWER_RE.fullmatch(text):
            raise RefusedError("permission_prompt")
        permission_answer = True
    elif entry is not None:
        if shows_draft(screen):
            raise RefusedError("draft_present")
        if shows_vim_normal(screen):
            raise RefusedError("vim_normal")
    buffer = "sb-" + secrets.token_hex(8)
    data = text.encode("utf-8")
    code, _, _ = run_tmux(["load-buffer", "-b", buffer, "-"], policy, env, stdin=data)
    if code != 0:
        raise RefusedError("tmux_failed")
    code, _, _ = run_tmux(["paste-buffer", "-p", "-d", "-b", buffer, "-t", pane_id], policy, env)
    if code != 0:
        run_tmux(["delete-buffer", "-b", buffer], policy, env)
        raise RefusedError("tmux_failed")
    if permission_answer:
        # A choice number selects in Claude Code's dialog; Enter could answer the next one.
        return {"sent": True, "bytes": len(data), "enter": False}
    # Race guard: a dialog that opened since the first check would take the Enter as "Yes".
    pane, entry, screen = _send_state(policy, env, pane_id)
    if pane is None or pane["pane_pid"] != pane_pid or _asks_permission(entry, screen):
        raise RefusedError("pasted_no_enter")
    code, _, _ = run_tmux(["send-keys", "-t", pane_id, "Enter"], policy, env)
    if code != 0:
        raise RefusedError("pasted_no_enter")
    return {"sent": True, "bytes": len(data), "enter": True}


def handle(argv: list[str], env: dict[str, str], stdin: object = None) -> dict[str, object]:
    """The response document for one invocation. Never raises for a bad request. `stdin` (bytes or a
    binary file) is read only by `send`."""
    try:
        policy, tokens = resolve_request(argv, env)
        verb, args = validate_request(tokens)
        if verb == "version":
            body = verb_version(policy)
        elif verb == "list":
            body = verb_list(policy, env)
        elif verb == "send":
            body = verb_send(policy, env, args, stdin)
        else:
            body = verb_capture(policy, env, args[0], int(args[1]))
    except RefusedError as refused:
        return {"protocol": PROTOCOL, "ok": False, "error": refused.code}
    response: dict[str, object] = {"protocol": PROTOCOL, "ok": True}
    response.update(body)
    return response


def main() -> int:
    response = handle(sys.argv[1:], dict(os.environ), sys.stdin.buffer)
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
