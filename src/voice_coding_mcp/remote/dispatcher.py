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
- Serve mode (outbound agents): `python3 -I <this file> --serve <policy options>`, started by
  `switchboard-connect` on the machine with its stdin and stdout wired to an SSH connection out to
  the hub. It reads request frames (newline-delimited JSON) and answers each with the documents the
  forced command would write; the policy comes only from this real argv (refused under sshd).

Policy options: --sessions <globs> (comma separated, required), --allow-usage, --allow-send,
--allow-permission-answers, --send-to-any-pane, --slash-commands <names>, --claude-home <path>.
Test only (refused unless SWITCHBOARD_REMOTE_TEST=1 is in the environment): --tmux-socket-name.

Verbs: `version`, `list`, `capture <paneId> <lines>`, `send <paneId> <panePid> <sessionId|none> message`
or `send <paneId> <panePid> <sessionId|none> answer <dialogHash|none>` (text on stdin, only with
--allow-send; the mode says what the owner confirmed: a message, or the answer to one permission
dialog), `keys <paneId> <panePid> <sessionId|none> menu <menuHash|none> <key>...` or
`keys <paneId> <panePid> <sessionId|none> answer <dialogHash|none> <key>...` (1 to MAX_KEYS key names
from KEY_NAMES, only with --allow-send; bound to the menu or permission dialog the owner heard),
`resize <paneId> <panePid> <sessionId|none> <columns> <rows>` (the pane's window, only with
--allow-send), `clear <paneId> <panePid> <sessionId> <draftHash>` (one Ctrl+C that clears the draft
in an idle Claude Code input box, only the draft the owner heard, only with --allow-send),
`usage <sinceEpochSeconds> <maxMegabytes>` (the hub's known transcripts as JSON on stdin,
only with --allow-usage), `health` (free space and the read-only flag of the file systems that matter,
load, memory and uptime; read-only, no opt-in, 0.9.0). Every response is one JSON document on stdout,
`{"protocol": 1, "ok": true, ...}` or `{"protocol": 1, "ok": false, "error": "<code>"}`, except a
successful `usage`, which is JSON lines (a header document, then file headers and token records
without any text, then `{"done": true, ...}`); the exit code is 0 also for refusals. Nothing from the request ever reaches a shell: tmux runs from argv with
a fixed environment, and the text of a send reaches tmux only on the stdin of `load-buffer`.
"""

from __future__ import annotations

import calendar
import datetime
import fnmatch
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import select
import shlex
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from collections.abc import Iterator
from typing import BinaryIO

VERSION = "0.9.0"
PROTOCOL = 1
VERBS = ("version", "list", "capture", "send", "usage", "keys", "resize", "health", "clear")
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
SEND_MESSAGE = "message"
SEND_ANSWER = "answer"
"""The send modes: a message (never into a dialog) or a permission answer (only into the dialog the
owner heard, identified by `dialog_hash`)."""
DIALOG_HASH_RE = re.compile(r"[0-9a-f]{24}")
NO_DIALOG = "none"
"""The dialog hash of a permission wait that only the registry shows (nothing on the screen)."""
KEYS_MENU = "menu"
"""The `keys` mode for anything but a permission dialog: bound to `menu_hash` of the screen the owner
heard (`none`: no menu was on it). The other mode is SEND_ANSWER, bound to `dialog_hash`."""
NO_MENU = "none"
KEY_NAMES = ("Up", "Down", "Left", "Right", "Enter", "Escape", "Tab", "BTab", *"123456789", "y", "n")
"""The only keys `keys` presses: tmux key names (and single characters tmux types as they are), never
free text, modifiers (`C-c`, `M-x`) or tmux commands."""
MODE_KEYS = ("Tab", "BTab")
"""Keys that switch modes at a Claude Code input (Shift+Tab cycles its permission mode up to accept
edits or bypass permissions): `keys` presses them only into a bound menu or permission dialog, and
never after a FINAL_KEYS key (`keys_problem`)."""
FINAL_KEYS = ("Enter", "Escape", *"123456789", "y", "n")
"""Keys that end a selection (choose, dismiss or answer): only the last key of a `keys` request, so
nothing lands on the screen that follows the menu, which nobody checked."""
HISTORY_KEYS = ("Up", "Down")
"""At an input without a menu these recall earlier prompts or shell commands: never followed by Enter
there, which would submit text nobody heard."""
MAX_KEYS = 10
RESIZE_COLUMNS = (80, 300)
RESIZE_ROWS = (24, 120)
"""Inclusive bounds of `resize`."""
CLEAR_KEY = "C-c"
"""The one key `clear` presses: Ctrl+C clears a whole Claude Code input draft (any length, any
cursor position, a paste placeholder, an open slash list) in Claude Code 2.1.332. It also arms
Claude Code's exit for about 0.8 s ("Press Ctrl-C again to exit"), so it is pressed once, never
repeated, and never while that hint shows. Not in KEY_NAMES: `keys` never presses modifiers."""
CLEAR_STABLE_SECONDS = 0.5
"""`clear` checks the screen, waits this long and checks it again before the key (race guard): a
Ctrl+C just pressed at the keyboard shows its exit hint within this time, and a screen that changes
(typing, a dialog opening, Claude Code starting to work) is not cleared."""
CLEAR_SETTLE_SECONDS = 2.0
"""After the key `clear` waits this long before it checks the result and returns, past Claude
Code's exit window, so no later request can land as a second Ctrl+C inside it."""
TEST_CLEAR_NO_WAIT = "SWITCHBOARD_REMOTE_TEST_CLEAR_NO_WAIT"
"""Test only (with SWITCHBOARD_REMOTE_TEST=1): `clear` skips both waits."""

MAX_SESSION_GLOBS = 64
MAX_LINES = 500
MAX_PANES = 512
MAX_CAPTURE_BYTES = 64 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024
MAX_SEND_BYTES = 8 * 1024
MAX_USAGE_KNOWN_BYTES = 1024 * 1024
"""The hub's list of known transcripts on the stdin of `usage`."""
MAX_USAGE_MEGABYTES = 2048
MAX_USAGE_LINE_BYTES = 1024 * 1024
"""Transcript lines longer than this are skipped (pasted files, huge tool results)."""
MAX_TOKENS = 10**12
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
    "pane_width",
    "pane_height",
    "alternate_on",
)
LIST_FORMAT = "\t".join("#{" + name + "}" for name in LIST_FIELDS)

SERVE_OPTION = "--serve"
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


_BORDER_RE = re.compile(r"[\u2500-\u257f\s_-]+")
_ANY_OPTION_RE = re.compile(r"(?:[❯>›▶]\s*)?[1-9][.)]\s")
DIALOG_TITLE_LINES = 6
DIALOG_BODY_LINES = 12


def _dialog_anchor(tail: list[str]) -> int | None:
    """The index in `tail` of the line `shows_dialog` recognises, or None."""
    for index, line in enumerate(tail):
        if _DIALOG_QUESTION_RE.match(line):
            return index
        if _YES_OPTION_RE.match(line) and any(_NO_OPTION_RE.match(other) for other in tail[index + 1 : index + 7]):
            return index
    return None


def shows_dialog(screen: str) -> bool:
    """A permission dialog may be on the screen: a line that starts with "Do you want to ...?", or a
    `1. Yes` option followed within 6 lines by a `2.`..`9.` `No` option. Broader than the hub's
    `pending_input` heuristic on purpose: a missed dialog would let Enter select its highlighted
    option. The hub runs this same function when it prepares a send."""
    return _dialog_anchor(_screen_tail(screen)) is not None


def dialog_hash(screen: str) -> str | None:
    """What identifies the dialog `shows_dialog` sees, so a permission answer goes only into the
    dialog the owner heard: a hash of the dialog block (up to DIALOG_TITLE_LINES lines above the
    recognised line, stopping at a border, through the last numbered option after it), each line
    cleaned and with its whitespace collapsed. None when no dialog is on the screen. Hub and
    dispatcher run this function on the same kind of capture."""
    tail = _screen_tail(screen)
    anchor = _dialog_anchor(tail)
    if anchor is None:
        return None
    start = anchor
    while start > 0 and anchor - start < DIALOG_TITLE_LINES and not _BORDER_RE.fullmatch(tail[start - 1]):
        start -= 1
    end = anchor
    for index in range(anchor + 1, min(len(tail), anchor + 1 + DIALOG_BODY_LINES)):
        if _BORDER_RE.fullmatch(tail[index]):
            break
        if _ANY_OPTION_RE.match(tail[index]):
            end = index
    block = "\n".join(" ".join(clean_text(line, 400).split()) for line in tail[start : end + 1])
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:24]


_SCROLLED_BORDER_RE = re.compile(r"[\u2500-\u257f]+\s*↑\s*\d+\s+more\s*[\u2500-\u257f]+")
"""The top border of a Claude Code input box whose draft is taller than the box (`─── ↑ 35 more ───`):
the prompt line has scrolled out of view, only continuation lines show."""


def shows_draft(screen: str) -> bool:
    """Text is already typed in Claude Code's input box: the last input line (`>` or `❯`) has text
    after the prompt that is not the empty box's hint, or a box border says that lines scrolled out
    of view above (`_SCROLLED_BORDER_RE`, a draft taller than the box, whose prompt line is not on
    the screen). A heuristic; it refuses rather than submit the owner's unsent draft together with
    the send."""
    tail = _screen_tail(screen)
    if any(_SCROLLED_BORDER_RE.fullmatch(line) for line in tail):
        return True
    for line in reversed(tail):
        if line.startswith(_INPUT_PROMPTS):
            typed = line[1:].strip()
            return bool(typed) and not _PLACEHOLDER_RE.match(typed)
    return False


def shows_vim_normal(screen: str) -> bool:
    """Claude Code's vim mode in NORMAL state: pasted text would be read as commands."""
    return any(_VIM_NORMAL_RE.search(line) for line in _screen_tail(screen))


SUGGESTION_MAX = 300
"""At most this many characters of a prompt suggestion are reported (`capture`'s `suggestion`)."""
_ESCAPE_RE = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"  # CSI, group 1 the parameters, group 2 the final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (a hyperlink), to BEL or ST
    r"|\x1b[ -/]*[0-~]"  # any other escape sequence
    r"|\x1b"  # a lone escape at the end
)


def _dim_after(params: str, dim: bool) -> bool:
    """The dim attribute after an SGR (`ESC [ <params> m`): 2 sets it, 0, an empty parameter and 22
    clear it; colour arguments (38, 48, 58 with 5;n or 2;r;g;b) are skipped, not read as 2 or 0."""
    values = params.split(";")
    index = 0
    while index < len(values):
        value = values[index].split(":")[0]
        if value in ("", "0", "22"):
            dim = False
        elif value == "2":
            dim = True
        elif value in ("38", "48", "58") and ":" not in values[index] and index + 1 < len(values):
            index += 2 if values[index + 1] == "5" else 4 if values[index + 1] == "2" else 0
        index += 1
    return dim


def _styled_lines(raw: str) -> list[list[tuple[str, bool]]]:
    """A `capture-pane -e` capture as lines of (character, dim) cells, every escape sequence removed.
    tmux carries attributes across lines, so the state carries too."""
    lines: list[list[tuple[str, bool]]] = [[]]
    dim = False
    position = 0
    for found in _ESCAPE_RE.finditer(raw):
        for char in raw[position : found.start()]:
            if char == "\n":
                lines.append([])
            else:
                lines[-1].append((char, dim))
        if found.group(2) == "m":
            dim = _dim_after(found.group(1) or "", dim)
        position = found.end()
    for char in raw[position:]:
        if char == "\n":
            lines.append([])
        else:
            lines[-1].append((char, dim))
    return lines


def _dim_input(cells: list[tuple[str, bool]]) -> tuple[int, int, str] | None:
    """(start, end, text) of the content of an input line (the cells after its prompt, up to a closing
    box edge) when all of it is dim and it is not the empty box's hint, else None."""
    start = 0
    while start < len(cells) and (cells[start][0].isspace() or cells[start][0] in _BOX_EDGES):
        start += 1
    if start >= len(cells) or cells[start][0] not in _INPUT_PROMPTS:
        return None
    start += 1
    end = len(cells)
    while end > start and (cells[end - 1][0].isspace() or cells[end - 1][0] in _BOX_EDGES):
        end -= 1
    content = cells[start:end]
    text = "".join(char for char, _ in content).strip()
    if not text or _PLACEHOLDER_RE.match(text):
        return None
    if any(not dim for char, dim in content if not char.isspace()):
        return None
    return start, end, text


def split_suggestion(raw: str) -> tuple[str, str | None]:
    """(the plain screen, Claude Code's prompt suggestion or None) from a `capture-pane -e` capture.

    Claude Code (2.1.332) shows a suggested next prompt in its empty input box as dim text (SGR 2),
    accepted with Tab or Right; nobody typed it. The input line is the one `shows_draft` reads (the
    last line in the screen tail that starts with an input prompt). When every character after its
    prompt (up to a closing box edge) is dim and is not the empty box's hint (`Try "..."`, also dim,
    which stays), that text is the suggestion: it is cut from the plain screen, so `shows_draft`,
    `draft_hash` and the menu checks see an empty input, and returned on its own, cleaned and
    clipped to SUGGESTION_MAX characters. Older input boxes in the scrollback (a dim input line right
    under a border line) lose their suggestion too, without reporting it. A line with any non-dim
    character is typed text and is left whole. The plain screen is otherwise the capture without its
    escape sequences, as a capture without -e gives it."""
    lines = _styled_lines(raw)
    plain = ["".join(char for char, _ in cells) for cells in lines]

    def cut(index: int) -> str | None:
        found = _dim_input(lines[index])
        if found is None:
            return None
        start, end, text = found
        kept = plain[index][:start] + " " * (end - start) + plain[index][end:]
        plain[index] = kept.rstrip() if end == len(lines[index]) else kept
        return text

    stripped = [line.strip().strip(_BOX_EDGES).strip() for line in plain]
    tail = [index for index, line in enumerate(stripped) if line][-CHECK_TAIL:]
    current = next((index for index in reversed(tail) if stripped[index].startswith(_INPUT_PROMPTS)), None)
    above: str | None = None
    for index, line in enumerate(stripped):
        if index != current and line.startswith(_INPUT_PROMPTS) and above is not None and _BORDER_RE.fullmatch(above):
            cut(index)
        if line:
            above = line
    text = cut(current) if current is not None else None
    suggestion = " ".join(clean_text(text, SUGGESTION_MAX * 2).split())[:SUGGESTION_MAX] if text else None
    return "\n".join(plain), suggestion or None


_MENU_OPTION_RE = re.compile(r"(?:([❯>›▶])\s*)?([1-9][0-9]?)[.)]\s+(\S.*)")
_MARKED_LINE_RE = re.compile(r"[❯>›▶]\s*(\S.*)")
MENU_FOOTER_RE = re.compile(
    r"↑\s*/?\s*↓|←\s*/?\s*→|\benter\s+(?:to\s+)?(?:confirm|select|submit|choose)\b"
    r"|\besc\s+(?:to\s+)?(?:cancel|not now|go back|exit|close)\b|(?<![+-])\btab\s+to\s+[a-z]",
    re.I,
)
"""The key hints under an interactive menu ("↑/↓ choose · enter confirm · esc not now"). Not Claude
Code's input status line ("shift+tab to cycle", INPUT_STATUS_RE)."""
INPUT_STATUS_RE = re.compile(r"\bshift\s*[+-]\s*tab\s+to\s+cycle\b|^\?\s+for\s+shortcuts\b", re.I)
"""Claude Code's status line under its input box ("⏵⏵ accept edits on (shift+tab to cycle)", "? for
shortcuts"): below a menu it is skipped, never a menu footer."""
_PICKER_MOVE_RE = re.compile(r"↑\s*/?\s*↓")
_PICKER_PICK_RE = re.compile(
    r"\benter\s+(?:to\s+)?(?:confirm|select|submit|choose)\b|\besc\s+(?:to\s+)?(?:cancel|not now|go back|exit|close)\b",
    re.I,
)
_ASSISTANT_MARK_RE = re.compile(r"[⏺●]\s*")
MENU_GAP_LINES = 4
"""Non-option lines (an option's description) allowed between two options, or under the last one."""
MENU_GAP_LINES_PICKER = 12
"""The same under a picker footer (`_picker_footer`), which no prose with a numbered list has."""
MENU_PLAIN_OPTIONS = 12
"""At most this many lines in a menu without numbers."""
MENU_QUESTION_LINES = 3


def _menu_lines(screen: str) -> list[str]:
    return [line.strip().strip(_BOX_EDGES).strip() for line in screen.split("\n")]


def _picker_footer(footer: list[str]) -> bool:
    """Key hints that only an interactive picker shows: up/down to move and enter or esc to choose
    or dismiss ("↑/↓ choose · enter confirm · esc not now"). Claude Code's input status line has
    neither."""
    text = " ".join(footer)
    return _PICKER_MOVE_RE.search(text) is not None and _PICKER_PICK_RE.search(text) is not None


def _menu_footer(lines: list[str]) -> tuple[list[str], int]:
    """(the key hint lines at the bottom, the index of the last line above them that is neither
    empty, a border nor a key hint)."""
    index = len(lines) - 1
    footer: list[str] = []
    while index >= 0 and (
        not lines[index]
        or _BORDER_RE.fullmatch(lines[index])
        or INPUT_STATUS_RE.search(lines[index])
        or MENU_FOOTER_RE.search(lines[index])
    ):
        if lines[index] and not _BORDER_RE.fullmatch(lines[index]) and not INPUT_STATUS_RE.search(lines[index]):
            footer.insert(0, lines[index])
        index -= 1
    return footer, index


def _input_line(lines: list[str], index: int) -> bool:
    """`lines[index]` is alone between two borders: a one-line input box (Claude Code's prompt, a
    draft such as `❯ 2. fix it`), never a menu option."""
    return (
        0 < index < len(lines) - 1
        and _BORDER_RE.fullmatch(lines[index - 1]) is not None
        and _BORDER_RE.fullmatch(lines[index + 1]) is not None
    )


def _input_box(lines: list[str], first: int, last: int) -> bool:
    """Options `lines[first]`..`lines[last]` (`last` the last option or description line) inside a
    box: a border under `last` with only empty lines between (a draft ending in an empty line), and
    a border above `first` whose first non-empty line under it starts with an input prompt (`❯`,
    `>`). That is a multi-line Claude Code input draft such as `> 1. fix the parser` / `2. update
    tests`, or one whose options follow a prompt line (`> notes` / `> 1. fix the parser` / `2. update
    tests`), never a menu (no picker footer is under an input box, only its status line)."""
    below = last + 1
    while below < len(lines) and not lines[below]:
        below += 1
    if below >= len(lines) or _BORDER_RE.fullmatch(lines[below]) is None:
        return False
    top = first - 1
    while top >= 0 and _BORDER_RE.fullmatch(lines[top]) is None:
        top -= 1
    if top < 0:
        return False
    opening = next(line for line in lines[top + 1 : first + 1] if line)
    return opening.startswith(_INPUT_PROMPTS)


def _numbered_options(
    lines: list[str], last: int, max_gap: int
) -> tuple[list[tuple[int, str, bool, str | None]], int] | None:
    """(options `1.`..`n.` top down as (number, text, marked, description), the index of option 1)
    ending at `last`, or None. An option's description is the lines under it, up to the next option
    (or the bottom)."""
    index = last
    options: list[tuple[int, str, bool, str | None]] = []
    gap = 0
    below: list[str] = []
    while index >= 0:
        line = lines[index]
        found = _MENU_OPTION_RE.fullmatch(line)
        if found is not None:
            number = int(found.group(2))
            if (options and number != options[-1][0] - 1) or (not options and number < 2):
                return None
            marked = found.group(1) is not None
            if marked and _input_line(lines, index):
                return None
            description = " ".join(" ".join(below).split()) or None
            options.append((number, found.group(3).strip(), marked, description))
            gap = 0
            below = []
            if number == 1:
                break
        elif line and not _BORDER_RE.fullmatch(line):
            # An input prompt or an assistant line between the options and the bottom: prose with a
            # numbered list above the input box, not a menu.
            if line.startswith(_INPUT_PROMPTS) or _ASSISTANT_MARK_RE.match(line):
                return None
            gap += 1
            if gap > max_gap:
                return None
            below.insert(0, line)
        index -= 1
    if not options or options[-1][0] != 1:
        return None
    options.reverse()
    return options, index


def _plain_options(
    lines: list[str], last: int
) -> tuple[list[tuple[int, str, bool, str | None]], int, list[str]] | None:
    """A menu without numbers above a picker footer: the block of lines ending at `last` (up to an
    empty line or a border, at most MENU_PLAIN_OPTIONS) with exactly one selection-marked line. An
    unmarked first line ending in `?` or `:` is the question. Returns (options numbered by position
    top down as (number, text, marked), the index of the first line, question lines), or None."""
    index = last
    block: list[str] = []
    while index >= 0 and lines[index] and not _BORDER_RE.fullmatch(lines[index]):
        if _ASSISTANT_MARK_RE.match(lines[index]) or len(block) >= MENU_PLAIN_OPTIONS + MENU_QUESTION_LINES:
            return None
        block.insert(0, lines[index])
        index -= 1
    first = index + 1
    question: list[str] = []
    while len(block) > 1 and not _MARKED_LINE_RE.fullmatch(block[0]) and block[0].endswith(("?", ":")):
        question.append(block.pop(0))
        if len(question) >= MENU_QUESTION_LINES:
            break
    marked = [_MARKED_LINE_RE.fullmatch(line) for line in block]
    if len(block) < 2 or len(block) > MENU_PLAIN_OPTIONS or sum(m is not None for m in marked) != 1:
        return None
    options: list[tuple[int, str, bool, str | None]] = [
        (position, (mark.group(1) if mark is not None else line).strip(), mark is not None, None)
        for position, line, mark in ((index + 1, block[index], marked[index]) for index in range(len(block)))
    ]
    return options, first, question


def _find_menu(lines: list[str]) -> dict[str, object] | None:
    footer, last = _menu_footer(lines)
    picker = _picker_footer(footer)
    numbered = _numbered_options(lines, last, MENU_GAP_LINES_PICKER if picker else MENU_GAP_LINES)
    question: list[str] = []
    if numbered is not None:
        options, first = numbered
    else:
        plain = _plain_options(lines, last) if picker else None
        if plain is None:
            return None
        options, first, question = plain
    marked = [number for number, _, mark, _ in options if mark]
    if not marked and not footer:
        return None
    if numbered is not None and not picker and _input_box(lines, first, last):
        return None
    if not question:
        probe = first - 1
        while probe >= 0 and (not lines[probe] or _BORDER_RE.fullmatch(lines[probe])):
            probe -= 1
        while (
            probe >= 0
            and lines[probe]
            and not _BORDER_RE.fullmatch(lines[probe])
            and len(question) < MENU_QUESTION_LINES
        ):
            line = lines[probe]
            if MENU_FOOTER_RE.search(line) or line.startswith(_INPUT_PROMPTS):
                break
            assistant = _ASSISTANT_MARK_RE.match(line)
            question.insert(0, line[assistant.end() :] if assistant else line)
            first = probe
            if assistant:
                break
            probe -= 1
    return {
        "question": " ".join(" ".join(question).split()) or None,
        "options": [(number, " ".join(text.split())) for number, text, _, _ in options],
        "descriptions": [description for _, _, _, description in options],
        "selected": marked[0] if marked else None,
        "footer": " ".join(" ".join(footer).split()) or None,
        "numbered": numbered is not None,
        "first": first,
        "last": last,
    }


def find_menu(screen: str) -> dict[str, object] | None:
    """An interactive menu at the bottom of the screen (Claude Code's questions with choices, its
    permission dialogs, other pickers), or None. Either a run of consecutive numbered options
    `1.`..`99.` (an optional `❯ > › ▶` marks the selected one; up to MENU_GAP_LINES description lines
    between options, MENU_GAP_LINES_PICKER under a picker footer) with only borders, empty lines,
    Claude Code's input status line (INPUT_STATUS_RE, skipped) and key hints (MENU_FOOTER_RE) below
    it, and either a selection marker or key hints; a marked line alone between two borders is an
    input box (a draft such as `❯ 2. fix it`), never an option, and so are options inside a box
    whose first line starts with an input prompt without a picker footer (a multi-line draft
    `> 1. fix the parser` / `2. update tests`, also ending in an empty line or under a `> notes`
    line, `_input_box`); or, under a
    picker footer (up/down plus enter or esc hints), a block of up to MENU_PLAIN_OPTIONS lines
    without numbers, exactly one of them selection-marked (`numbered` False; the options are then
    numbered by position and a digit key does not choose them). Prose with a numbered list above an
    input box is no menu. Returns `question` (up to MENU_QUESTION_LINES lines directly above the
    options, stopping at an assistant line), `options` [(number, text)], `descriptions` (per
    option, its lines below it joined, or None), `selected` (a number or None), `footer`, `numbered`
    and the line range `first`..`last`. The hub runs this same function
    for what it reads back."""
    return _find_menu(_menu_lines(screen))


def shows_picker(screen: str) -> bool:
    """The bottom of the screen has a picker footer (`_picker_footer`): a menu is open, whether or
    not `find_menu` can read it. `keys` without a bound menu is refused then (`menu_unreadable`):
    the selection mark of an unread menu is no input draft, and Enter would choose an option nobody
    heard."""
    return _picker_footer(_menu_footer(_menu_lines(screen))[0])


def dialog_bound(screen: str, claude_pane: bool = True) -> bool:
    """A permission dialog's answer options are at the bottom of the screen: `dialog_hash` sees a
    dialog, `find_menu` reads a menu there (numbered options with nothing but borders, the input
    status line and key hints under them), and the line `shows_dialog` recognised is part of that
    menu (its question or an option). Assistant prose that starts a line with "Do you want to ...?"
    or lists "1. Yes ... 2. No" above an idle input box is no bound dialog: in `answer` mode keys
    then follow the rules without a bound menu (`keys_problem` with `menu_bound` False, no picker
    footer, no draft, no vim normal mode). In a pane without Claude Code (`claude_pane` False) only
    a dialog under a picker footer binds: shell output can print any dialog text."""
    if not claude_pane and not shows_picker(screen):
        return False
    tail = _screen_tail(screen)
    anchor = _dialog_anchor(tail)
    if anchor is None:
        return False
    lines = _menu_lines(screen)
    menu = _find_menu(lines)
    if menu is None:
        return False
    first, last = menu["first"], menu["last"]
    assert isinstance(first, int)
    assert isinstance(last, int)
    return tail[anchor] in lines[first : last + 1]


SUGGESTION_KEYS = ("Right", "Tab")
"""Keys that accept Claude Code's prompt suggestion into the input (2.1.332; Enter alone does not
submit it). Tab is refused at an input anyway (`mode_keys`)."""


def accepts_suggestion(keys: list[str]) -> bool:
    """`keys` would accept a prompt suggestion that `split_suggestion` found: at an input without a
    menu they are refused (`suggestion_present`), since the next Enter would submit a prompt nobody
    typed or heard."""
    return any(key in SUGGESTION_KEYS for key in keys)


def keys_problem(keys: list[str], menu_bound: bool, answer: bool = False) -> str | None:
    """Why a `keys` request may not be pressed as it is, or None; the hub runs the same function
    before it reads back. A FINAL_KEYS key (a choice, Enter, Escape, y, n) may only be the last key
    (`key_order`): what follows it would land on whatever screen the choice opens, which nobody
    checked. In a permission dialog (`answer` True) the MODE_KEYS are final too: Shift+Tab is itself
    a choice in Claude Code's edit dialog ("allow all edits during this session") and closes it, so
    a key after it would answer a queued dialog nobody heard or submit a draft. Without a bound menu or permission dialog (`menu_bound` False), no MODE_KEYS
    (`mode_keys`) and no HISTORY_KEYS before Enter (`history_submit`: that recalls and submits an
    earlier prompt or shell command nobody heard)."""
    if any(key in FINAL_KEYS or (answer and key in MODE_KEYS) for key in keys[:-1]):
        return "key_order"
    if not menu_bound:
        if any(key in MODE_KEYS for key in keys):
            return "mode_keys"
        if keys and keys[-1] == "Enter" and any(key in HISTORY_KEYS for key in keys):
            return "history_submit"
    return None


def menu_hash(screen: str) -> str | None:
    """What identifies the menu `find_menu` sees, so keys go only into the menu the owner heard: a
    hash of its question, options and descriptions with the selection marker (a selection moved at
    the keyboard changes it), each line cleaned and with its whitespace collapsed. None when no menu
    is on the screen. Hub and dispatcher run this function on the same kind of capture."""
    lines = _menu_lines(screen)
    menu = _find_menu(lines)
    if menu is None:
        return None
    first, last = menu["first"], menu["last"]
    assert isinstance(first, int)
    assert isinstance(last, int)
    block = "\n".join(" ".join(clean_text(line, 400).split()) for line in lines[first : last + 1] if line)
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:24]


def bound_menu_hash(screen: str, claude_pane: bool) -> str | None:
    """`menu_hash` as `keys` binds to it: in a pane without Claude Code (`claude_pane` False) only a
    menu under a picker footer (`shows_picker`) binds, None otherwise. Shell output ending in
    `1. Yes` / `❯ 2. No` above the shell prompt (read as an option's description) is no menu: Up
    Enter there reruns the previous command. Hub and dispatcher run this same function."""
    if not claude_pane and not shows_picker(screen):
        return None
    return menu_hash(screen)


INPUT_BOX_BELOW = 10
"""Non-empty lines (status line, hints, an open slash list) allowed under the input box's bottom
border."""
_EXIT_ARMED_RE = re.compile(r"\bagain to exit\b", re.I)


def input_box(screen: str) -> tuple[list[str], bool] | None:
    """Claude Code's input box at the bottom of the screen: (its content lines, cleaned and with
    whitespace collapsed, the input prompt removed from the first; `scrolled`, True when its top
    border says lines scrolled out of view above), or None when no input box is recognised. The box
    is the last border line with at most INPUT_BOX_BELOW non-empty lines under it, up to the border
    above it; its first non-empty line starts with an input prompt (`❯`, `>`), unless the top border
    is a scrolled one. The empty box's hint (`Try "..."`) is no content."""
    lines = _menu_lines(screen)
    while lines and not lines[-1]:
        lines.pop()
    bottom = len(lines) - 1
    below = 0
    while bottom >= 0 and not _BORDER_RE.fullmatch(lines[bottom]):
        if lines[bottom]:
            below += 1
            if below > INPUT_BOX_BELOW:
                return None
        bottom -= 1
    if bottom < 0:
        return None
    top = bottom - 1
    while top >= 0 and not _BORDER_RE.fullmatch(lines[top]) and not _SCROLLED_BORDER_RE.fullmatch(lines[top]):
        top -= 1
    if top < 0:
        return None
    scrolled = _SCROLLED_BORDER_RE.fullmatch(lines[top]) is not None
    content = [" ".join(clean_text(line, 400).split()) for line in lines[top + 1 : bottom]]
    while content and not content[0]:
        content.pop(0)
    while content and not content[-1]:
        content.pop()
    if not scrolled:
        if not content or not content[0].startswith(_INPUT_PROMPTS):
            return None
        content[0] = content[0][1:].strip()
        if len(content) == 1 and (not content[0] or _PLACEHOLDER_RE.match(content[0])):
            content = []
    return content, scrolled


def input_draft(screen: str) -> tuple[list[str], bool] | None:
    """The draft typed in Claude Code's input box, as `input_box` reads it ((lines, scrolled)), or
    None when nothing is typed. When no box is recognised but `shows_draft` sees a draft, its typed
    prompt line (or, for a scrolled box, nothing readable) stands for it."""
    box = input_box(screen)
    if box is not None:
        return box if box[0] or box[1] else None
    if not shows_draft(screen):
        return None
    for line in reversed(_screen_tail(screen)):
        if line.startswith(_INPUT_PROMPTS):
            return [" ".join(clean_text(line[1:], 400).split())], False
    return [], True


def draft_hash(screen: str) -> str | None:
    """What identifies the draft `input_draft` sees, so `clear` removes only the draft the owner
    heard: a hash of its lines (and whether lines scrolled out above). None without a draft. Hub
    and dispatcher run this function on the same kind of capture."""
    draft = input_draft(screen)
    if draft is None:
        return None
    lines, scrolled = draft
    block = ("\u2191\n" if scrolled else "") + "\n".join(lines)
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:24]


def shows_empty_input(screen: str) -> bool:
    """Positive evidence of an empty Claude Code input: `input_box` finds a box with no content and
    nothing scrolled out above, and `shows_draft` sees no draft. A screen without a recognisable box
    is not empty."""
    box = input_box(screen)
    return box is not None and not box[0] and not box[1] and not shows_draft(screen)


def shows_exit_armed(screen: str) -> bool:
    """Claude Code's "Press Ctrl-C again to exit" (or Ctrl-D) hint: a Ctrl+C now would exit it."""
    return any(_EXIT_ARMED_RE.search(line) for line in _screen_tail(screen))


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
        elif token == SERVE_OPTION or token in FLAG_OPTIONS:
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
    if SERVE_OPTION in options:
        # Serve mode has its own entry point (`serve`); here it can only be a broken command line.
        raise RefusedError("bad_policy")
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
    if verb in ("version", "list", "health") and args:
        raise RefusedError("bad_request")
    if verb == "capture":
        if len(args) != 2 or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if not 1 <= int(args[1]) <= MAX_LINES:
            raise RefusedError("bad_request")
    if verb == "send":
        if len(args) not in (4, 5) or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if args[2] != SEND_NO_SESSION and not UUID_RE.fullmatch(args[2]):
            raise RefusedError("bad_request")
        if args[3] == SEND_MESSAGE:
            if len(args) != 4:
                raise RefusedError("bad_request")
        elif args[3] == SEND_ANSWER:
            if len(args) != 5 or (args[4] != NO_DIALOG and not DIALOG_HASH_RE.fullmatch(args[4])):
                raise RefusedError("bad_request")
        else:
            raise RefusedError("bad_request")
    if verb == "keys":
        if not 6 <= len(args) <= 5 + MAX_KEYS or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if args[2] != SEND_NO_SESSION and not UUID_RE.fullmatch(args[2]):
            raise RefusedError("bad_request")
        if args[3] == SEND_ANSWER:
            if args[4] != NO_DIALOG and not DIALOG_HASH_RE.fullmatch(args[4]):
                raise RefusedError("bad_request")
        elif args[3] == KEYS_MENU:
            if args[4] != NO_MENU and not DIALOG_HASH_RE.fullmatch(args[4]):
                raise RefusedError("bad_request")
        else:
            raise RefusedError("bad_request")
        if any(key not in KEY_NAMES for key in args[5:]):
            raise RefusedError("bad_request")
    if verb == "resize":
        if (
            len(args) != 5
            or not PANE_ID_RE.fullmatch(args[0])
            or not all(DECIMAL_RE.fullmatch(a) for a in (args[1], args[3], args[4]))
        ):
            raise RefusedError("bad_request")
        if args[2] != SEND_NO_SESSION and not UUID_RE.fullmatch(args[2]):
            raise RefusedError("bad_request")
        if (
            not RESIZE_COLUMNS[0] <= int(args[3]) <= RESIZE_COLUMNS[1]
            or not RESIZE_ROWS[0] <= int(args[4]) <= RESIZE_ROWS[1]
        ):
            raise RefusedError("bad_request")
    if verb == "clear":
        if len(args) != 4 or not PANE_ID_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if args[2] != SEND_NO_SESSION and not UUID_RE.fullmatch(args[2]):
            raise RefusedError("bad_request")
        if not DIALOG_HASH_RE.fullmatch(args[3]):
            raise RefusedError("bad_request")
    if verb == "usage":
        if len(args) != 2 or not NUMBER_RE.fullmatch(args[0]) or not DECIMAL_RE.fullmatch(args[1]):
            raise RefusedError("bad_request")
        if not 1 <= int(args[1]) <= MAX_USAGE_MEGABYTES:
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


def capture_screen(policy: Policy, env: dict[str, str], pane_id: str, lines: int) -> tuple[int, str, str | None, str]:
    """(exit code, plain screen, prompt suggestion, stderr) of the last `lines` lines of a pane,
    captured with its styling (`capture-pane -p -e -J`) and split by `split_suggestion`. Every
    check and every `capture` answer works on this plain screen."""
    code, out, err = run_tmux(["capture-pane", "-p", "-e", "-J", "-t", pane_id, "-S", f"-{lines}"], policy, env)
    if code != 0:
        return code, "", None, err
    screen, suggestion = split_suggestion(out)
    return code, screen, suggestion, err


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
                "width": _int(pane["pane_width"]),
                "height": _int(pane["pane_height"]),
                "claude": claude_view(entry) if entry is not None else None,
            }
        )
    return {"panes": out, "truncated": truncated}


def verb_capture(policy: Policy, env: dict[str, str], pane_id: str, lines: int) -> dict[str, object]:
    pane = next((p for p in list_panes(policy, env) if p["pane_id"] == pane_id), None)
    # A pane outside the allowed sessions answers like a missing one, so names do not leak.
    if pane is None or not policy.allows(pane["session_name"]):
        raise RefusedError("gone")
    code, out, suggestion, err = capture_screen(policy, env, pane_id, lines)
    if code != 0:
        raise RefusedError("gone" if "can't find" in err.lower() else "tmux_failed")
    data = out.encode("utf-8")
    if len(data) > MAX_CAPTURE_BYTES:
        out = data[-MAX_CAPTURE_BYTES:].decode("utf-8", "ignore")
    extra: dict[str, object] = {"suggestion": suggestion} if suggestion is not None else {}
    return {
        "text": out,
        "paneId": pane_id,
        "session": clean_text(pane["session_name"], NAME_MAX),
        "window": _int(pane["window_index"]),
        "paneWidth": _int(pane["pane_width"]),
        "paneHeight": _int(pane["pane_height"]),
        "alternateOn": _flag(pane["alternate_on"]),
        **extra,
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
) -> tuple[dict[str, str] | None, dict[str, object] | None, str, str | None]:
    """(the pane if it exists in an allowed session, its registry entry, its plain screen, its
    prompt suggestion) right now."""
    panes = [p for p in list_panes(policy, env) if policy.allows(p["session_name"])]
    pane = next((p for p in panes if p["pane_id"] == pane_id), None)
    if pane is None:
        return None, None, "", None
    entry = join_registry(panes, read_registry(claude_home(policy, env)), env).get(pane_id)
    code, out, suggestion, _ = capture_screen(policy, env, pane_id, SEND_CHECK_LINES)
    if code != 0:
        raise RefusedError("tmux_failed")
    return pane, entry, out, suggestion


def _asks_permission(entry: dict[str, object] | None, screen: str) -> bool:
    if entry is not None and entry.get("status") == "waiting":
        waiting_for = entry.get("waitingFor")
        if isinstance(waiting_for, str) and "permission" in waiting_for.lower():
            return True
    return shows_dialog(screen)


def _same_session(entry: dict[str, object] | None, session_id: str) -> bool:
    """The pane still runs the Claude Code session the hub saw (or still none)."""
    if session_id == SEND_NO_SESSION:
        return entry is None
    return entry is not None and entry.get("sessionId") == session_id


def verb_send(policy: Policy, env: dict[str, str], args: list[str], stdin: object) -> dict[str, object]:
    """Types the text from stdin into a pane and presses Enter, after rechecking everything the hub
    checked when it prepared the send. See docs/SWITCHBOARD.md, "The dispatcher".

    The mode binds what the owner confirmed: a `message` is refused while a permission dialog shows
    (`permission_prompt`), and an `answer` is typed only while the same dialog shows that the hub
    read back (`dialog_changed` otherwise)."""
    if not policy.allow_send:
        raise RefusedError("not_allowed")
    pane_id, pane_pid, session_id, mode = args[:4]
    text = read_send_text(stdin)
    pane, entry, screen, _ = _send_state(policy, env, pane_id)
    if pane is None:
        raise RefusedError("gone")
    if pane["pane_pid"] != pane_pid:
        raise RefusedError("changed")
    if not _same_session(entry, session_id):
        raise RefusedError("changed")
    if session_id == SEND_NO_SESSION and not policy.send_to_any_pane:
        raise RefusedError("not_claude")
    if text_problem(text, claude_pane=entry is not None, slash_commands=policy.slash_commands) is not None:
        raise RefusedError("bad_text")
    permission_answer = mode == SEND_ANSWER
    asks = _asks_permission(entry, screen)
    if permission_answer:
        if not policy.allow_permission_answers or not PERMISSION_ANSWER_RE.fullmatch(text):
            raise RefusedError("permission_prompt")
        if not asks or (dialog_hash(screen) or NO_DIALOG) != args[4]:
            raise RefusedError("dialog_changed")
    elif asks:
        raise RefusedError("permission_prompt")
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
    # Race guard: a dialog that opened since the first check would take the Enter as "Yes", and a
    # Claude Code restarted in the same shell is not the session the owner confirmed.
    pane, entry, screen, _ = _send_state(policy, env, pane_id)
    if (
        pane is None
        or pane["pane_pid"] != pane_pid
        or not _same_session(entry, session_id)
        or _asks_permission(entry, screen)
    ):
        raise RefusedError("pasted_no_enter")
    code, _, _ = run_tmux(["send-keys", "-t", pane_id, "Enter"], policy, env)
    if code != 0:
        raise RefusedError("pasted_no_enter")
    return {"sent": True, "bytes": len(data), "enter": True}


def verb_keys(policy: Policy, env: dict[str, str], args: list[str]) -> dict[str, object]:
    """Presses 1 to MAX_KEYS keys from KEY_NAMES in a pane (one `send-keys` without `-l`), after
    rechecking the pane, its process and Claude Code session, and the screen the owner heard: in
    `menu` mode no permission dialog may show (`permission_prompt`) and the menu must be the one read
    back (`menu_changed`, `none` when there was none; then also no draft and no vim normal mode); in
    `answer` mode the permission dialog must be the one read back (`dialog_changed`), and only with
    --allow-permission-answers, and when its answer options are not at the bottom of the screen
    (`dialog_bound` False: prose that looks like a dialog) the unbound rules apply as in `menu none`; `answer none` (a permission wait only the registry shows, no dialog
    on the screen to bind to) is refused whatever the keys (`dialog_not_visible`). Without a bound
    menu, a screen with a picker footer is refused (`menu_unreadable`). `keys_problem` limits the
    order of the keys (`key_order`, `mode_keys`, `history_submit`; in `answer` mode Tab and Shift+Tab
    count as final keys too), and every check runs before the one `send-keys`, so a key that ends the
    selection is the last one pressed. In `menu` mode a bound menu with a draft on the screen and no
    picker footer is refused (`draft_present`: the options may be the draft's own lines), and in a
    pane without Claude Code only a menu under a picker footer binds (`bound_menu_hash`), so shell
    output that looks like a menu gets the unbound rules."""
    if not policy.allow_send:
        raise RefusedError("not_allowed")
    pane_id, pane_pid, session_id, mode, bound = args[:5]
    keys = args[5:]
    if mode == SEND_ANSWER and bound == NO_DIALOG:
        # A permission answer is bound only by a dialog on the screen. A registry-only wait (a stale
        # entry, or a dialog shows_dialog cannot see) may show an input box, where Enter submits a
        # draft, Up Enter an earlier prompt and Shift+Tab cycles the permission mode: nothing goes in.
        raise RefusedError("dialog_not_visible")
    problem = keys_problem(keys, menu_bound=bound != NO_MENU, answer=mode == SEND_ANSWER)
    if problem is not None:
        raise RefusedError(problem)
    pane, entry, screen, suggestion = _send_state(policy, env, pane_id)
    if pane is None:
        raise RefusedError("gone")
    if pane["pane_pid"] != pane_pid or not _same_session(entry, session_id):
        raise RefusedError("changed")
    if session_id == SEND_NO_SESSION and not policy.send_to_any_pane:
        raise RefusedError("not_claude")
    asks = _asks_permission(entry, screen)
    if mode == SEND_ANSWER:
        if not policy.allow_permission_answers:
            raise RefusedError("permission_prompt")
        if not asks or (dialog_hash(screen) or NO_DIALOG) != bound:
            raise RefusedError("dialog_changed")
        if not dialog_bound(screen, claude_pane=entry is not None):
            # The dialog hash matched prose ("Do you want to ...?" above an idle input box), not
            # answer options at the bottom: the keys land at an input, so the unbound rules apply.
            problem = keys_problem(keys, menu_bound=False)
            if problem is not None:
                raise RefusedError(problem)
            if shows_picker(screen):
                raise RefusedError("menu_unreadable")
            if shows_draft(screen):
                raise RefusedError("draft_present")
            if shows_vim_normal(screen):
                raise RefusedError("vim_normal")
            if suggestion is not None and accepts_suggestion(keys):
                raise RefusedError("suggestion_present")
    elif asks:
        raise RefusedError("permission_prompt")
    else:
        current = bound_menu_hash(screen, claude_pane=entry is not None)
        if (current or NO_MENU) != bound:
            raise RefusedError("menu_changed")
        if current is not None and shows_draft(screen) and not shows_picker(screen):
            # Defence in depth: a "menu" without a picker footer over a draft in the input box may be
            # the draft's own numbered lines, where Enter submits it and Shift+Tab cycles the mode.
            raise RefusedError("draft_present")
        if current is None and shows_picker(screen):
            raise RefusedError("menu_unreadable")
        if current is None and entry is not None:
            if shows_draft(screen):
                raise RefusedError("draft_present")
            if shows_vim_normal(screen):
                raise RefusedError("vim_normal")
            if suggestion is not None and accepts_suggestion(keys):
                raise RefusedError("suggestion_present")
    code, _, _ = run_tmux(["send-keys", "-t", pane_id, *keys], policy, env)
    if code != 0:
        raise RefusedError("tmux_failed")
    return {"sent": True, "keys": len(keys)}


def verb_resize(policy: Policy, env: dict[str, str], args: list[str]) -> dict[str, object]:
    """Resizes the window of a pane (`resize-window -x -y`, which also sets that window's
    `window-size` to manual; `set-option -w -u window-size` in that window undoes it) within
    RESIZE_COLUMNS and RESIZE_ROWS, after rechecking the pane, its process and the Claude Code
    session the owner heard (`changed`; `none` only with --send-to-any-pane, else `not_claude`)."""
    if not policy.allow_send:
        raise RefusedError("not_allowed")
    pane_id, pane_pid, session_id, columns, rows = args
    panes = [p for p in list_panes(policy, env) if policy.allows(p["session_name"])]
    pane = next((p for p in panes if p["pane_id"] == pane_id), None)
    if pane is None:
        raise RefusedError("gone")
    entry = join_registry(panes, read_registry(claude_home(policy, env)), env).get(pane_id)
    if pane["pane_pid"] != pane_pid or not _same_session(entry, session_id):
        raise RefusedError("changed")
    if session_id == SEND_NO_SESSION and not policy.send_to_any_pane:
        raise RefusedError("not_claude")
    code, _, err = run_tmux(["resize-window", "-t", pane_id, "-x", columns, "-y", rows], policy, env)
    if code != 0:
        raise RefusedError("gone" if "can't find" in err.lower() else "tmux_failed")
    return {"resized": True, "columns": int(columns), "rows": int(rows)}


# --- health ------------------------------------------------------------------------------------

WSL_HOST_DRIVE = "/mnt/c"
"""The Windows drive a WSL machine's disk image usually lives on, as DrvFs mounts it."""
HOST_PROBE_SECONDS = 3.0
"""Longest wait for the statvfs of the Windows drive: DrvFs goes through the Windows host, which may
hang. The probe runs in a daemon thread; while one hangs, no second one starts."""
_host_probe_lock = threading.Lock()
_host_probe_busy = [False]


def _filesystem(name: str, path: str) -> dict[str, object]:
    """Free and total bytes of the file system holding `path` (statvfs, never a walk) and whether it
    is mounted read-only. `freeBytes` is what an unprivileged user may still write."""
    try:
        info = os.statvfs(path)
        device = os.stat(path).st_dev
    except OSError:
        return {"name": name, "error": "unavailable"}
    return {
        "name": name,
        "totalBytes": info.f_blocks * info.f_frsize,
        "freeBytes": info.f_bavail * info.f_frsize,
        "readOnly": bool(info.f_flag & os.ST_RDONLY),
        "_device": device,
    }


def _read_small(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def is_wsl(proc: str = "/proc") -> bool:
    """A WSL kernel names Microsoft in its release (`6.6.87.2-microsoft-standard-WSL2`)."""
    text = _read_small(os.path.join(proc, "sys", "kernel", "osrelease")) or _read_small(os.path.join(proc, "version"))
    return "microsoft" in text.lower()


def is_mounted(path: str, proc: str = "/proc") -> bool:
    """`path` is a mount point in /proc/self/mounts (read, never walked)."""
    for line in _read_small(os.path.join(proc, "self", "mounts"), 1024 * 1024).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == path:
            return True
    return False


def _probe_host_drive(path: str, seconds: float) -> dict[str, object]:
    with _host_probe_lock:
        if _host_probe_busy[0]:
            return {"name": "windows", "error": "timeout"}
        _host_probe_busy[0] = True
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result.update(_filesystem("windows", path))
        finally:
            with _host_probe_lock:
                _host_probe_busy[0] = False

    worker = threading.Thread(target=run, name="host-drive-probe", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return {"name": "windows", "error": "timeout"}
    return dict(result)


def _meminfo(proc: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read_small(os.path.join(proc, "meminfo")).splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            out[key] = int(parts[0]) * 1024
    return out


def verb_health(
    policy: Policy, env: dict[str, str], proc: str = "/proc", host_drive: str = WSL_HOST_DRIVE
) -> dict[str, object]:
    """Free space and the read-only flag of `/`, `$HOME` and the Claude Code home, on WSL also of the
    Windows drive (statvfs of `/mnt/c` when it is mounted), plus load, memory and uptime. Labels
    only, never paths. A file system already reported under an earlier label says `sameAs`."""
    wsl = is_wsl(proc)
    systems = [
        _filesystem("root", "/"),
        _filesystem("home", home_dir(env)),
        _filesystem("claudeHome", claude_home(policy, env)),
    ]
    if wsl:
        systems.append(
            _probe_host_drive(host_drive, HOST_PROBE_SECONDS)
            if is_mounted(host_drive, proc)
            else {"name": "windows", "error": "not_mounted"}
        )
    seen: dict[object, str] = {}
    for entry in systems:
        device = entry.pop("_device", None)
        if device is None:
            continue
        if device in seen:
            entry["sameAs"] = seen[device]
        else:
            seen[device] = str(entry["name"])
    try:
        load: list[float] | None = [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    memory = _meminfo(proc)
    uptime_text = _read_small(os.path.join(proc, "uptime")).split()
    try:
        uptime: float | None = round(float(uptime_text[0]), 1) if uptime_text else None
    except ValueError:
        uptime = None
    return {
        "wsl": wsl,
        "filesystems": systems,
        "load": load,
        "memoryTotalBytes": memory.get("MemTotal"),
        "memoryAvailableBytes": memory.get("MemAvailable"),
        "uptimeSeconds": uptime,
    }


def _clear_check(policy: Policy, env: dict[str, str], args: list[str]) -> str:
    """The screen of the pane `clear` would press its key in, after every check; raises the refusal.
    Claude Code panes only (a shell's Ctrl+C interrupts its program; `none` is `not_claude`, also
    with --send-to-any-pane), idle (`not_idle`: Ctrl+C interrupts a working session and cancels a
    question), no permission dialog (`permission_prompt`), no menu or picker footer (`menu_present`),
    no vim normal mode, no exit hint (`exit_armed`), a draft (`nothing_to_clear`) and the one the
    owner heard (`draft_changed`)."""
    pane_id, pane_pid, session_id, draft = args
    pane, entry, screen, _ = _send_state(policy, env, pane_id)
    if pane is None:
        raise RefusedError("gone")
    if pane["pane_pid"] != pane_pid or not _same_session(entry, session_id):
        raise RefusedError("changed")
    if entry is None:
        raise RefusedError("not_claude")
    if _asks_permission(entry, screen):
        raise RefusedError("permission_prompt")
    if bound_menu_hash(screen, claude_pane=True) is not None or shows_picker(screen):
        raise RefusedError("menu_present")
    if entry.get("status") != "idle":
        raise RefusedError("not_idle")
    if shows_vim_normal(screen):
        raise RefusedError("vim_normal")
    if shows_exit_armed(screen):
        raise RefusedError("exit_armed")
    current = draft_hash(screen)
    if current is None:
        raise RefusedError("nothing_to_clear")
    if current != draft:
        raise RefusedError("draft_changed")
    return screen


def _pause(seconds: float) -> None:
    """The waits of `clear` (a function of its own, so tests record them instead of sleeping)."""
    time.sleep(seconds)


def verb_clear(policy: Policy, env: dict[str, str], args: list[str]) -> dict[str, object]:
    """Clears the draft in a Claude Code pane's input box with one Ctrl+C (CLEAR_KEY), after the
    checks of `_clear_check`, run twice CLEAR_STABLE_SECONDS apart (race guard; the second run is
    right before the key). Waits CLEAR_SETTLE_SECONDS after the key, past Claude Code's exit window,
    captures again and reports whether the input is now empty (`shows_empty_input`: a visible empty
    box, not just a missing draft). Never presses a second key, whatever the result. Returns
    `{"pressed": true, "cleared": bool, "remainingLines": n, "remainingChars": n}`."""
    if not policy.allow_send:
        raise RefusedError("not_allowed")
    pane_id = args[0]
    wait = not (env.get(TEST_FLAG) == "1" and env.get(TEST_CLEAR_NO_WAIT) == "1")
    _clear_check(policy, env, args)
    if wait:
        _pause(CLEAR_STABLE_SECONDS)
    _clear_check(policy, env, args)
    code, _, _ = run_tmux(["send-keys", "-t", pane_id, CLEAR_KEY], policy, env)
    if code != 0:
        raise RefusedError("tmux_failed")
    if wait:
        # Serve mode runs mutating verbs one at a time (`send_lock`), so nothing else is typed into
        # any pane before Claude Code's exit window has closed.
        _pause(CLEAR_SETTLE_SECONDS)
    # The prompt suggestion that Claude Code shows again in the emptied box is cut off here.
    code, screen, _, _ = capture_screen(policy, env, pane_id, SEND_CHECK_LINES)
    if code != 0:
        return {"pressed": True, "cleared": False, "remainingLines": None, "remainingChars": None}
    left = input_draft(screen)
    lines = left[0] if left is not None else []
    return {
        "pressed": True,
        "cleared": shows_empty_input(screen),
        "remainingLines": len(lines),
        "remainingChars": sum(len(line) for line in lines),
    }


# --- usage -------------------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:[.]([0-9]{1,9}))?"
    r"(Z|z|[+-][0-9]{2}:?[0-9]{2})?"
)
_RECORD_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_USER_MARK_RE = re.compile(rb'"type"\s*:\s*"user"')
_USAGE_MARK = b'"usage"'


def parse_timestamp(value: object) -> int | None:
    """Epoch milliseconds of a transcript timestamp (`2026-11-26T15:08:31.121Z`; no zone is UTC)."""
    if not isinstance(value, str):
        return None
    found = _TIMESTAMP_RE.fullmatch(value.strip())
    if found is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in found.groups()[:6])
    fraction = found.group(7) or ""
    zone = found.group(8) or "Z"
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    millis = int((fraction + "000")[:3])
    offset = 0
    if zone not in ("Z", "z"):
        digits = zone[1:].replace(":", "")
        offset = (int(digits[:2]) * 60 + int(digits[2:])) * 60_000
        if zone[0] == "+":
            offset = -offset
    return calendar.timegm(moment.timetuple()) * 1000 + millis + offset


def _tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_TOKENS)


def _record_id(value: object) -> str | None:
    return value if isinstance(value, str) and _RECORD_ID_RE.fullmatch(value) else None


def is_owner_prompt(entry: dict[str, object]) -> bool:
    """A prompt line of the session's driver (the owner, or `claude -p`): not meta, not a subagent,
    not a tool result."""
    if entry.get("isMeta") is True or entry.get("isSidechain") is True:
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    prompt = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False
        if block.get("type") in ("text", "image"):
            prompt = True
    return prompt


def usage_record(entry: dict[str, object]) -> dict[str, object] | None:
    """The compact usage record of one transcript line, or None. No text, tool input or path."""
    moment = parse_timestamp(entry.get("timestamp"))
    if moment is None:
        return None
    kind = entry.get("type")
    if kind == "user":
        return {"t": moment, "k": "u"} if is_owner_prompt(entry) else None
    if kind != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    cache = usage.get("cache_creation")
    if isinstance(cache, dict) and ("ephemeral_5m_input_tokens" in cache or "ephemeral_1h_input_tokens" in cache):
        write_5m = _tokens(cache.get("ephemeral_5m_input_tokens"))
        write_1h = _tokens(cache.get("ephemeral_1h_input_tokens"))
    else:
        write_5m, write_1h = _tokens(usage.get("cache_creation_input_tokens")), 0
    model = message.get("model")
    model_name = clean_text(model, NAME_MAX) if isinstance(model, str) else ""
    return {
        "t": moment,
        "k": "a",
        "m": model_name or None,
        "id": _record_id(message.get("id")),
        "rq": _record_id(entry.get("requestId")),
        "in": _tokens(usage.get("input_tokens")),
        "out": _tokens(usage.get("output_tokens")),
        "cw5": write_5m,
        "cw1": write_1h,
        "cr": _tokens(usage.get("cache_read_input_tokens")),
    }


def parse_transcript(handle: BinaryIO, limit: int) -> tuple[str, list[dict[str, object]]]:
    """(project, records) of a transcript opened in binary mode, reading at most `limit` bytes.
    Lines over MAX_USAGE_LINE_BYTES are skipped; only lines that may carry usage or a prompt are
    parsed. The project is the basename of the session's latest `cwd`."""
    project = ""
    records: list[dict[str, object]] = []
    left = limit
    skipping = False
    while left > 0:
        line = handle.readline(min(left, MAX_USAGE_LINE_BYTES + 1))
        if not line:
            break
        left -= len(line)
        if skipping or len(line) > MAX_USAGE_LINE_BYTES:
            # The rest of an overlong line is skipped up to its newline.
            skipping = not line.endswith(b"\n")
            continue
        if _USAGE_MARK not in line and not _USER_MARK_RE.search(line):
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd.strip("/") and entry.get("isSidechain") is not True:
            project = clean_text(os.path.basename(cwd.rstrip("/")), NAME_MAX)
        record = usage_record(entry)
        if record is not None:
            records.append(record)
    return project, records


def read_usage_known(stdin: object) -> dict[str, tuple[int, int]]:
    """The hub's known transcripts, `[{"id", "size", "mtime"}]` on stdin: id to (size, mtime ms)."""
    if isinstance(stdin, bytes):
        raw = stdin[: MAX_USAGE_KNOWN_BYTES + 1]
    elif stdin is not None and hasattr(stdin, "read"):
        try:
            raw = stdin.read(MAX_USAGE_KNOWN_BYTES + 1)
        except (OSError, ValueError):
            raise RefusedError("bad_request") from None
    else:
        raw = b""
    if not isinstance(raw, bytes) or len(raw) > MAX_USAGE_KNOWN_BYTES:
        raise RefusedError("bad_request")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RefusedError("bad_request") from None
    if not isinstance(data, list):
        raise RefusedError("bad_request")
    known: dict[str, tuple[int, int]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise RefusedError("bad_request")
        file_id, size, mtime = item.get("id"), _number(item.get("size")), _number(item.get("mtime"))
        if not isinstance(file_id, str) or not UUID_RE.fullmatch(file_id) or size is None or mtime is None:
            raise RefusedError("bad_request")
        known[file_id] = (size, mtime)
    return known


def transcript_files(home: str) -> list[tuple[str, str, int, int]]:
    """(session id, path, size, mtime ms) of `<home>/projects/*/<uuid>.jsonl`: top level of each
    project directory only, no symlinks, the first file of an id wins."""
    out: list[tuple[str, str, int, int]] = []
    seen: set[str] = set()
    try:
        projects = sorted(os.scandir(os.path.join(home, "projects")), key=lambda item: item.name)
    except OSError:
        return out
    for directory in projects:
        try:
            if not directory.is_dir(follow_symlinks=False):
                continue
            files = sorted(os.scandir(directory.path), key=lambda item: item.name)
        except OSError:
            continue
        for item in files:
            name = item.name
            if not name.endswith(".jsonl") or not UUID_RE.fullmatch(name[:-6]):
                continue
            file_id = name[:-6].lower()
            try:
                if file_id in seen or not item.is_file(follow_symlinks=False):
                    continue
                info = item.stat(follow_symlinks=False)
            except OSError:
                continue
            seen.add(file_id)
            out.append((file_id, item.path, info.st_size, info.st_mtime_ns // 1_000_000))
    return out


def usage_documents(
    home: str, since_ms: int, max_bytes: int, known: dict[str, tuple[int, int]]
) -> Iterator[dict[str, object]]:
    """The usage lines for transcripts modified since `since_ms` whose (size, mtime) differs from
    `known`: per file a header `{"file", "size", "mtime", "project", "records"}` and its records, or
    `{"file", "size", "mtime", "skipped": true}` when it is larger than `max_bytes`; then
    `{"done": true, "gone": [known ids that no longer exist]}`."""
    present: set[str] = set()
    for file_id, path, size, mtime in transcript_files(home):
        present.add(file_id)
        if mtime < since_ms or known.get(file_id) == (size, mtime):
            continue
        if size > max_bytes:
            yield {"file": file_id, "size": size, "mtime": mtime, "skipped": True}
            continue
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                project, records = parse_transcript(handle, size)
        except OSError:
            continue
        yield {"file": file_id, "size": size, "mtime": mtime, "project": project, "records": len(records)}
        yield from records
    yield {"done": True, "gone": sorted(set(known) - present)}


def verb_usage(policy: Policy, env: dict[str, str], args: list[str], stdin: object) -> Iterator[dict[str, object]]:
    """Checks the request now; the returned iterator reads the transcripts while it is written out."""
    if not policy.allow_usage:
        raise RefusedError("not_allowed")
    known = read_usage_known(stdin)
    since_ms = int(args[0]) * 1000
    max_bytes = int(args[1]) * 1024 * 1024
    return usage_documents(claude_home(policy, env), since_ms, max_bytes, known)


def respond(argv: list[str], env: dict[str, str], stdin: object = None) -> Iterator[dict[str, object]]:
    """The response documents for one invocation: one document, or for a successful `usage` a header
    document followed by the usage lines. Never raises for a bad request. `stdin` (bytes or a binary
    file) is read only by `send` and `usage`."""
    try:
        policy, tokens = resolve_request(argv, env)
    except RefusedError as refused:
        yield refusal(refused.code)
        return
    yield from run_request(policy, tokens, env, stdin)


def refusal(code: str) -> dict[str, object]:
    return {"protocol": PROTOCOL, "ok": False, "error": code}


def run_request(
    policy: Policy, tokens: list[str], env: dict[str, str], stdin: object = None
) -> Iterator[dict[str, object]]:
    """The response documents for the request `<verb> [args]` under `policy`: the one implementation
    behind the forced command, the explicit path, the local transport and serve mode."""
    lines: Iterator[dict[str, object]] | None = None
    try:
        verb, args = validate_request(tokens)
        if verb == "version":
            body = verb_version(policy)
        elif verb == "list":
            body = verb_list(policy, env)
        elif verb == "send":
            body = verb_send(policy, env, args, stdin)
        elif verb == "keys":
            body = verb_keys(policy, env, args)
        elif verb == "resize":
            body = verb_resize(policy, env, args)
        elif verb == "health":
            body = verb_health(policy, env)
        elif verb == "clear":
            body = verb_clear(policy, env, args)
        elif verb == "usage":
            lines = verb_usage(policy, env, args, stdin)
            body = {"stream": "usage"}
        else:
            body = verb_capture(policy, env, args[0], int(args[1]))
    except RefusedError as refused:
        yield refusal(refused.code)
        return
    response: dict[str, object] = {"protocol": PROTOCOL, "ok": True}
    response.update(body)
    yield response
    if lines is not None:
        yield from lines


def handle(argv: list[str], env: dict[str, str], stdin: object = None) -> dict[str, object]:
    """The (first) response document for one invocation. Never raises for a bad request."""
    return next(respond(argv, env, stdin))


# --- serve mode ------------------------------------------------------------------------------
#
# The machine connects out to the hub (`switchboard-connect` runs `ssh` with its stdin and stdout
# wired to this process). Frames are newline-delimited JSON objects, written ASCII-only. The hub
# sends `req`, `ping`, `cancel`, `welcome` and `bye`; this side sends `hello` once, then `res`, `pong`
# and `error`. A request runs through `run_request` exactly like `SSH_ORIGINAL_COMMAND` does, so every
# rule and refusal code is the forced command's. The policy is this process's own argv, always.

SERVE_PROTOCOL = 1
MAX_HUB_FRAME_BYTES = 2 * 1024 * 1024
"""Longest frame from the hub (a usage known-list of at most 1 MB as a JSON string fits)."""
MAX_MACHINE_FRAME_BYTES = 5 * 1024 * 1024
"""Longest frame to the hub (a 4 MB response document plus its envelope)."""
MAX_FRAME_ID = 2**53 - 1
SERVE_WORKERS = 4
SERVE_MAX_ARGS = 16
SERVE_SILENCE_SECONDS = 180.0
"""End serve mode after this long without any frame from the hub (it pings far more often)."""
EXIT_REPLACED = 75
EXIT_REFUSED = 76
BYE_EXIT = {"replaced": EXIT_REPLACED, "unknown_machine": EXIT_REFUSED, "protocol": EXIT_REFUSED, "shutdown": 0}
MUTATING_VERBS = ("send", "keys", "resize", "clear")
STDIN_VERBS = ("send", "usage")
FRAME_ERRORS = ("bad_frame", "frame_too_large", "duplicate_id", "busy", "crashed")
_HUB_FRAMES = {
    "req": ({"t", "id", "verb", "args"}, {"input"}),
    "ping": ({"t", "id"}, set()),
    "cancel": ({"t", "id"}, set()),
    "welcome": ({"t", "protocol"}, set()),
    "bye": ({"t", "reason"}, set()),
}
_EOF = "eof"
_SILENCE = "silence"
_TOO_LARGE = "too_large"


def serve_requested(argv: list[str]) -> bool:
    """`--serve` among the leading options (skipping option values, as `parse_options` does)."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--" or not token.startswith("-"):
            return False
        if token == SERVE_OPTION:
            return True
        index += 2 if token in VALUE_OPTIONS else 1
    return False


def serve_policy(argv: list[str], env: dict[str, str]) -> Policy | None:
    """The policy of a serve-mode command line, None when it is broken (every request is then
    answered `bad_policy`, so the mistake shows up on the hub)."""
    try:
        options, from_hub, rest = parse_options(argv, env)
        if from_hub or rest or options.pop(SERVE_OPTION, None) is not True:
            return None
        return policy_from_options(options)
    except RefusedError:
        return None


def frame_line(frame: dict[str, object]) -> bytes:
    return (json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _frame_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_FRAME_ID


def parse_hub_frame(line: bytes) -> tuple[dict[str, object] | None, object]:
    """(frame, id): the frame when it has exactly the schema of its type, else None and the id to
    name in the `bad_frame` error (None when there is no valid one)."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    frame_id = data.get("id") if _frame_id(data.get("id")) else None
    kind = data.get("t")
    shape = _HUB_FRAMES.get(kind) if isinstance(kind, str) else None
    if shape is None:
        return None, frame_id
    required, optional = shape
    keys = set(data)
    if not required <= keys or not keys <= required | optional:
        return None, frame_id
    if "id" in required and frame_id is None:
        return None, None
    if kind == "req":
        args = data["args"]
        if (
            not isinstance(data["verb"], str)
            or not isinstance(args, list)
            or len(args) > SERVE_MAX_ARGS
            or not all(isinstance(arg, str) for arg in args)
            or not isinstance(data.get("input", ""), str)
        ):
            return None, frame_id
    elif kind == "welcome":
        if type(data["protocol"]) is not int or data["protocol"] != SERVE_PROTOCOL:
            return None, None
    elif kind == "bye" and data["reason"] not in BYE_EXIT:
        return None, None
    return data, frame_id


class LineReader:
    """Newline-terminated lines from a file descriptor, at most `limit` bytes each (without the
    newline), with a timeout per wait."""

    def __init__(self, fd: int, limit: int) -> None:
        self.fd = fd
        self.limit = limit
        self.buffer = bytearray()

    def next(self, timeout: float) -> bytes | str:
        """A line, or `_EOF`, `_SILENCE` (nothing within `timeout` seconds) or `_TOO_LARGE`."""
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                if end > self.limit:
                    return _TOO_LARGE
                line = bytes(self.buffer[:end])
                del self.buffer[: end + 1]
                return line
            if len(self.buffer) > self.limit:
                return _TOO_LARGE
            try:
                ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
            except (OSError, ValueError):
                return _EOF
            if not ready:
                return _SILENCE
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return _EOF
            if not chunk:
                return _EOF
            self.buffer.extend(chunk)


class Server:
    """One serve-mode connection: the reader runs in the calling thread, requests in worker threads."""

    def __init__(self, policy: Policy | None, env: dict[str, str], out_fd: int) -> None:
        self.policy = policy
        self.env = env
        self.out_fd = out_fd
        self.write_lock = threading.Lock()
        self.send_lock = threading.Lock()
        """`send`, `keys` and `resize` run one at a time: their checks and keystrokes never interleave."""
        self.state_lock = threading.Lock()
        self.inflight: dict[int, threading.Event] = {}
        self.workers: list[threading.Thread] = []
        self.broken = threading.Event()

    def write(self, frame: dict[str, object]) -> bool:
        line = frame_line(frame)
        if len(line) > MAX_MACHINE_FRAME_BYTES:
            # Not reachable with the dispatcher's own caps; the framing stays intact anyway.
            line = frame_line({"t": "error", "id": frame.get("id"), "error": "frame_too_large"})
        with self.write_lock:
            if self.broken.is_set():
                return False
            view = memoryview(line)
            try:
                while view:
                    written = os.write(self.out_fd, view)
                    view = view[written:]
            except OSError:
                self.broken.set()
                return False
        return True

    def hello(self) -> None:
        policy = self.policy
        self.write(
            {
                "t": "hello",
                "protocol": SERVE_PROTOCOL,
                "version": VERSION,
                "verbs": list(VERBS),
                "allowSend": bool(policy and policy.allow_send),
                "allowUsage": bool(policy and policy.allow_usage),
                "policy": "ok" if policy is not None else "bad_policy",
            }
        )

    def documents(self, verb: str, args: list[str], data: str | None) -> Iterator[dict[str, object]]:
        if self.policy is None:
            return iter([refusal("bad_policy")])
        if data is not None and verb not in STDIN_VERBS:
            return iter([refusal("bad_request")])
        # Lone surrogates stay invalid UTF-8, which the verbs refuse as they would on stdin.
        stdin = data.encode("utf-8", "surrogatepass") if data is not None else b""
        return run_request(self.policy, [verb, *args], self.env, stdin)

    def run_request(
        self, request_id: int, verb: str, args: list[str], data: str | None, cancel: threading.Event
    ) -> None:
        try:
            docs = self.documents(verb, args, data)
            if verb in MUTATING_VERBS:
                with self.send_lock:
                    current = next(docs)
            else:
                current = next(docs)
            streaming = verb == "usage"
            while not self.broken.is_set():
                if streaming and cancel.is_set():
                    # The hub gave up: stop at this record and say so in the last frame.
                    self.write({"t": "res", "id": request_id, "doc": current, "more": True})
                    self.write({"t": "res", "id": request_id, "doc": refusal("cancelled")})
                    return
                following = next(docs, None)
                if following is None:
                    self.write({"t": "res", "id": request_id, "doc": current})
                    return
                self.write({"t": "res", "id": request_id, "doc": current, "more": True})
                current = following
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.write({"t": "error", "id": request_id, "error": "crashed"})
        finally:
            with self.state_lock:
                self.inflight.pop(request_id, None)

    def start_request(self, frame: dict[str, object]) -> None:
        request_id = frame["id"]
        assert isinstance(request_id, int)
        with self.state_lock:
            if request_id in self.inflight:
                error = "duplicate_id"
            elif len(self.inflight) >= SERVE_WORKERS:
                error = "busy"
            else:
                error = ""
                cancel = threading.Event()
                self.inflight[request_id] = cancel
        if error:
            self.write({"t": "error", "id": request_id, "error": error})
            return
        verb, args, data = frame["verb"], frame["args"], frame.get("input")
        assert isinstance(verb, str)
        assert isinstance(args, list)
        worker = threading.Thread(
            target=self.run_request,
            args=(request_id, verb, [str(arg) for arg in args], data if isinstance(data, str) else None, cancel),
            name=f"request-{request_id}",
        )
        self.workers = [thread for thread in self.workers if thread.is_alive()]
        self.workers.append(worker)
        worker.start()

    def loop(self, reader: LineReader, silence_seconds: float) -> int:
        """Reads frames until the hub closes, says bye or goes silent; the exit code."""
        while not self.broken.is_set():
            line = reader.next(silence_seconds)
            if line in (_EOF, _SILENCE):
                return 0
            if line == _TOO_LARGE:
                self.write({"t": "error", "id": None, "error": "frame_too_large"})
                return EXIT_REFUSED
            assert isinstance(line, bytes)
            frame, frame_id = parse_hub_frame(line)
            if frame is None:
                self.write({"t": "error", "id": frame_id, "error": "bad_frame"})
                continue
            kind = frame["t"]
            if kind == "req":
                self.start_request(frame)
            elif kind == "ping":
                self.write({"t": "pong", "id": frame["id"]})
            elif kind == "cancel":
                cancel_id = frame["id"]
                with self.state_lock:
                    event = self.inflight.get(cancel_id) if isinstance(cancel_id, int) else None
                if event is not None:
                    event.set()
            elif kind == "bye":
                return BYE_EXIT[str(frame["reason"])]
        return 0

    def finish(self) -> None:
        """Lets running requests end (a send in progress completes; tmux calls are capped)."""
        with self.state_lock:
            for event in self.inflight.values():
                event.set()
        for worker in self.workers:
            worker.join()


def serve(
    argv: list[str], env: dict[str, str], in_fd: int, out_fd: int, silence_seconds: float = SERVE_SILENCE_SECONDS
) -> int:
    """Serve mode on `in_fd` (frames from the hub) and `out_fd` (frames to the hub); the exit code:
    0 at the end of input, after silence or after `bye shutdown`, 75 after `bye replaced`, 76 after
    `bye unknown_machine` or `bye protocol` or an oversized frame."""
    if "SSH_ORIGINAL_COMMAND" in env or "SSH_CONNECTION" in env:
        # Someone made serve mode a forced command: answer like a broken forced command does.
        os.write(out_fd, frame_line(refusal("bad_policy")))
        return 0
    server = Server(serve_policy(argv, env), env, out_fd)
    server.hello()
    try:
        return server.loop(LineReader(in_fd, MAX_HUB_FRAME_BYTES), silence_seconds)
    finally:
        server.finish()


def main() -> int:
    argv = sys.argv[1:]
    if serve_requested(argv):
        return serve(argv, dict(os.environ), sys.stdin.fileno(), sys.stdout.fileno())
    for document in respond(argv, dict(os.environ), sys.stdin.buffer):
        sys.stdout.write(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
