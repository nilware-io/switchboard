"""Spoken results of the remote tools (docs/SWITCHBOARD.md, "Tools").

The hub's own facts come first; anything that programs or sessions on a machine control (screen
text, summaries, `waitingFor`, window and Claude Code session names) is quoted and labelled as
coming from the screen, never spoken as a sentence of the hub's own. Names are cleaned and clipped
to 40 characters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..text import clip, plural
from .inventory import MachineView
from .menu import ScreenMenu
from .pending import PendingEntry, PendingReport, waits_for_permission
from .protocol import ClaudeInfo, PaneInfo
from .redact import clean_name, redact
from .send import CLEAR, KEYS, RESIZE, Confirmation, key_words, risk_words, yes_phrase
from .targets import Ambiguous, NoTarget, NotFound
from .transport import TransportError

SESSIONS_PER_MACHINE = 8

UNREACHABLE: dict[str, str] = {
    "timeout": "the connection timed out",
    "host_key": "its host key is not trusted, check the known hosts file",
    "auth": "the key was refused",
    "refused": "the connection was refused",
    "dispatcher_missing": "switchboard-remote is not installed there",
    "bad_response": "it answered something unexpected",
    "refused_by_policy": "switchboard-remote refused the request",
    "remote_failed": "something failed on the machine",
    "other": "something went wrong",
    "not_connected": "it is not connected right now",
    "disconnected": "the connection was lost",
}


def unreachable_reason(error: TransportError | None, detail: str | None = None) -> str:
    if error == "remote_failed" and detail == "tmux_failed":
        # The machine answered: tmux there failed (stopped, or its disk went read-only).
        return "tmux failed there"
    if error == "refused_by_policy" and detail in ("no_policy", "bad_policy"):
        return "switchboard-remote there has no valid policy"
    if error == "refused_by_policy" and detail == "busy":
        # An agent machine still running earlier requests (a timed-out capture keeps its worker).
        return "it is busy, try again in a moment"
    return UNREACHABLE.get(error or "other", UNREACHABLE["other"])


def unreachable_sentence(name: str, error: TransportError | None, detail: str | None = None) -> str:
    """ "dev is not reachable: <reason>.", or for an agent machine without a connection "laptop is
    not connected right now." """
    if error == "not_connected":
        return f"{name} is not connected right now."
    if error == "remote_failed":
        return f"{name} answered, but {unreachable_reason(error, detail)}."
    return f"{name} is not reachable: {unreachable_reason(error, detail)}."


def quote(text: str) -> str:
    """Screen text in double quotes (inner double quotes become single ones)."""
    return '"' + text.replace('"', "'") + '"'


def is_permission(claude: ClaudeInfo) -> bool:
    return waits_for_permission(claude.waiting_for)


def state_words(claude: ClaudeInfo) -> str:
    match claude.status:
        case "busy":
            return "working"
        case "waiting":
            return "waiting for your permission" if is_permission(claude) else "waiting for your answer"
        case "idle":
            return "idle"
        case _:
            return "running"


def claude_label(pane: PaneInfo) -> str:
    """ "Claude Code" plus its session name in quotes when that differs from the tmux session name."""
    claude = pane.claude
    name = clean_name(claude.name) if claude else ""
    if name and name.lower() != pane.session.lower():
        return f"Claude Code {quote(name)}"
    return "Claude Code"


def _session_line(session: str, panes: Sequence[PaneInfo]) -> str:
    name = clean_name(session) or "unnamed"
    claude = [pane for pane in panes if pane.claude is not None]
    if not claude:
        return f"{name}, no Claude Code"
    if len(claude) == 1:
        pane = claude[0]
        assert pane.claude is not None
        return f"{name}, {claude_label(pane)} {state_words(pane.claude)}"
    states = ", ".join(f"one {state_words(p.claude)}" for p in claude if p.claude is not None)
    return f"{name}, {len(claude)} Claude Code panes: {states}"


def machine_line(view: MachineView) -> str:
    name = view.machine.name
    if not view.ok:
        if view.error == "not_connected":
            return f"{name}: not connected right now."
        if view.error == "remote_failed":
            return f"{name}: answered, but {unreachable_reason(view.error, view.detail)}."
        return f"{name}: not reachable, {unreachable_reason(view.error, view.detail)}."
    sessions = view.sessions()
    cut = " It has more panes than I can list, so some are missing." if view.truncated else ""
    if not sessions:
        return f"{name}: no allowed tmux sessions.{cut}"
    items = list(sessions.items())
    shown = ". ".join(_session_line(session, panes) for session, panes in items[:SESSIONS_PER_MACHINE])
    more = f" And {len(items) - SESSIONS_PER_MACHINE} more." if len(items) > SESSIONS_PER_MACHINE else ""
    return f"{name}: {plural(len(items), 'session')}. {shown}.{more}{cut}"


def format_list(views: Sequence[MachineView], *, total_machines: int, notes: Mapping[str, str] | None = None) -> str:
    """One line per machine; `notes` adds a short clause to a machine's line (an open incident)."""
    if total_machines == 0:
        return "No machines are configured."
    lines = [f"{plural(len(views), 'machine')}."] if len(views) > 1 else []
    lines += [machine_line(view) + (notes or {}).get(view.machine.name, "") for view in views]
    return "\n".join(lines)


def pane_label(machine: str, pane: PaneInfo, name_window: bool) -> str:
    """ "dev, fpga", or "dev, fpga window 2" when `name_window` (`Resolved.name_window`)."""
    label = f"{machine}, {clean_name(pane.session) or 'unnamed'}"
    if name_window and pane.window_index is not None:
        label += f" window {pane.window_index}"
    return label


def status_fact(pane: PaneInfo) -> str:
    """The hub's own fact about a pane, from the registry."""
    if pane.claude is None:
        return "no Claude Code here."
    return f"{claude_label(pane)} is {state_words(pane.claude)}."


MENU_WORDS_MAX = 400
MENU_WORDS_DETAILED_MAX = 600
"""The heuristic status names each option's description too, within this many characters."""
MENU_AFTER_SUMMARY_MAX = 200
"""After a model summary only the options, within this many characters."""
DESCRIPTION_WORDS_MAX = 60


def _option_words(menu: ScreenMenu, limit: int, used: int, details: bool) -> tuple[list[str], bool]:
    """(the spoken options within `limit`, with "and N more" when cut; whether all fit)."""
    parts: list[str] = []
    for index, (number, text) in enumerate(menu.options):
        item = (f"{number} " if menu.numbered else "") + quote(redact(text))
        description = menu.descriptions[index] if details and index < len(menu.descriptions) else None
        if description:
            item += f" (details: {quote(clip(redact(description), DESCRIPTION_WORDS_MAX))})"
        item += ", selected" if number == menu.selected else ""
        if parts and used + len(item) + 2 > limit:
            parts.append(f"and {len(menu.options) - index} more")
            return parts, False
        parts.append(item)
        used += len(item) + 2
    return parts, True


def menu_words(
    menu: ScreenMenu, limit: int = MENU_WORDS_MAX, *, hints: bool = True, details: bool = False, question: bool = True
) -> str:
    """'A menu on the screen asks: "<question>". Options: 1 "Small", selected; 2 "Medium".' (a menu
    without numbers: '"Small", selected; "Medium"'), the options cut (with "and N more") so the
    whole stays within about `limit` characters, then the unusual key hints unless `hints` is off.
    With `details`, each option's description in short ('1 "A" (details: "...")'), dropped again
    when then not every option fits. Without `question`: 'The menu's options: ...'. Screen text, so
    quoted and redacted."""
    if not question:
        head = "The menu's options:"
    elif menu.question:
        head = f"A menu on the screen asks: {quote(redact(menu.question))}. Options:"
    else:
        head = "The screen shows a menu. Options:"
    used = len(head) + 1
    parts, fits = _option_words(menu, limit, used, details)
    if details and not fits:
        parts, _ = _option_words(menu, limit, used, False)
    hints_said = footer_words(menu.footer) if hints else None
    tail = f" Its key hints also say: {quote(hints_said)}." if hints_said else ""
    return f"{head} {'; '.join(parts)}.{tail}"


FOOTER_WORDS_MAX = 120
_HINT_SPLIT_RE = re.compile(r"\s*(?:·|•|\||\s{2,})\s*")
_USUAL_HINT_RE = re.compile(r"(?:↑\s*/?\s*↓|(?:enter|return|esc|escape)\b)", re.I)
_ARROW_WORDS = (("↑/↓", "up/down"), ("←/→", "left/right"), ("↑", "up"), ("↓", "down"), ("←", "left"), ("→", "right"))


def footer_words(footer: str | None) -> str | None:
    """The key hints of a menu footer beyond the usual up/down, enter and esc ones ("←/→ change
    size" becomes "left/right change size"), redacted and clipped; None when there are none. Those
    hints name keys that do something else in that menu, so the owner hears them before choosing."""
    if not footer:
        return None
    extra: list[str] = []
    for part in _HINT_SPLIT_RE.split(footer.strip()):
        if not part or _USUAL_HINT_RE.match(part):
            continue
        spoken = part
        for arrow, word in _ARROW_WORDS:
            spoken = spoken.replace(arrow, word)
        extra.append(" ".join(spoken.split()))
    if not extra:
        return None
    return clip(redact(", ".join(extra)), FOOTER_WORDS_MAX)


def format_status(
    label: str, pane: PaneInfo, *, summary: str | None, line: str | None, menu: ScreenMenu | None = None
) -> str:
    fact = status_fact(pane)
    if summary:
        # The summary names the question and the options already; only a short list of the menu's
        # own options follows, so they reach the owner whatever the summary says.
        more = f" {menu_words(menu, MENU_AFTER_SUMMARY_MAX, hints=False, question=False)}" if menu is not None else ""
        return f"{label}: {fact} A summary of the screen: {quote(summary)}{more}"
    if menu is not None:
        return f"{label}: {fact} {menu_words(menu, MENU_WORDS_DETAILED_MAX, details=True)}"
    if line:
        return f"{label}: {fact} The screen says: {quote(redact(line))}"
    return f"{label}: {fact} The screen is empty."


def cut_note(rows: int, can_resize: bool, *, menu_cut: bool = False) -> str:
    """Appended when the pane is on its alternate screen (no scrollback) and short, or (`menu_cut`)
    shows a picker footer whose menu cannot be read, at any height."""
    more = " I can make it bigger if you want." if can_resize else ""
    if menu_cut:
        return (
            f" The pane is {rows} rows high and shows a full-screen program, and the menu on it is cut off: "
            f"I cannot see its question or first options.{more}"
        )
    return (
        f" The pane is only {rows} rows high and shows a full-screen program, so part of a menu may be cut off.{more}"
    )


def format_pending_task(label: str, task_id: int) -> str:
    return f"{label}: I am still reading it. That is task {task_id}; ask me for it in a moment."


def format_target_problem(machine: str, problem: NotFound | Ambiguous | NoTarget) -> str:
    def names(values: Sequence[str]) -> str:
        cleaned = [clean_name(v) for v in values[:SESSIONS_PER_MACHINE]]
        more = f" and {len(values) - SESSIONS_PER_MACHINE} more" if len(values) > SESSIONS_PER_MACHINE else ""
        return ", ".join(c for c in cleaned if c) + more

    if isinstance(problem, NoTarget):
        if not problem.sessions:
            return f"{machine} has no allowed tmux sessions."
        return f"Which session on {machine}? Its sessions: {names(problem.sessions)}."
    target = quote(clean_name(problem.target) or "?")
    if isinstance(problem, Ambiguous):
        if problem.panes:
            first = problem.panes[0]
            choices = ", ".join(
                f"window {p.window_index}, {quote(clean_name(p.window_name) or str(p.window_index))}"
                for p in problem.panes
            )
            return f"Session {clean_name(first.session)} on {machine} has {len(problem.panes)} Claude Code panes: {choices}. Which one?"
        return f"{target} matches several sessions on {machine}: {names(problem.sessions)}. Which one?"
    if problem.sessions:
        return f"{machine} has no session called {target}. Its sessions: {names(problem.sessions)}."
    return f"{machine} has no session called {target}, and no allowed tmux sessions."


def format_machine_problem(spoken: str | None, candidates: Sequence[str], *, ambiguous: bool) -> str:
    names = ", ".join(candidates)
    if not candidates:
        return "No machines are configured."
    if spoken is None or not spoken.strip():
        return f"Which machine? {names}."
    if ambiguous:
        return f"{quote(clean_name(spoken))} matches several machines: {names}. Which one?"
    return f"No machine matches {quote(clean_name(spoken))}. Machines: {names}."


PENDING_SHOWN = 8


def _pending_line(entry: PendingEntry) -> str:
    if entry.kind == "permission":
        head = f"{entry.label} asks for permission"
    else:
        head = f"{entry.label} asks" if entry.question else f"{entry.label} asks you something"
    return f"{head}: {quote(entry.question)}." if entry.question else f"{head}."


def format_pending(report: PendingReport) -> str:
    """ "2 sessions need you." then one line per waiting session (permission first), the hint to
    approve permissions at the keyboard, and the machines that could not be checked."""
    entries = sorted(report.entries, key=lambda e: e.kind != "permission")
    unreachable = [view for view in report.views if not view.ok]
    lines: list[str] = []
    if not entries:
        lines.append(
            "Nobody is waiting for you on the machines I could check."
            if unreachable or report.partial
            else "Nobody is waiting for you."
        )
    else:
        need = "needs" if len(entries) == 1 else "need"
        lines.append(f"{plural(len(entries), 'session')} {need} you.")
        lines += [_pending_line(entry) for entry in entries[:PENDING_SHOWN]]
        if len(entries) > PENDING_SHOWN:
            lines.append(f"And {len(entries) - PENDING_SHOWN} more.")
        asks = [e for e in entries if e.kind == "permission" and not e.permission_answers]
        if asks:
            it = "it" if len(asks) == 1 else "them"
            lines.append(f"Approve {it} at the keyboard or in the Claude app.")
    for view in unreachable:
        if view.error == "not_connected":
            lines.append(f"{view.machine.name} is not connected right now.")
        else:
            lines.append(f"{view.machine.name} could not be checked: {unreachable_reason(view.error, view.detail)}.")
    for name in report.partial:
        lines.append(f"Some sessions on {name} could not be checked.")
    return "\n".join(lines)


# --- remote_send ----------------------------------------------------------------------------------


def send_place(machine: str, pane: PaneInfo, name_window: bool) -> str:
    """ "fpga on dev", or "fpga window 2 on dev" when `name_window`."""
    name = clean_name(pane.session) or "unnamed"
    if name_window and pane.window_index is not None:
        name += f" window {pane.window_index}"
    return f"{name} on {machine}"


def text_refusal(problem: str, slash_commands: Sequence[str]) -> str:
    """Why a text is not sent, for a `dispatcher.text_problem` reason."""
    match problem:
        case "empty":
            return "There is nothing to send."
        case "bang":
            return "I will not send text starting with an exclamation mark, it would run as a shell command."
        case "hash":
            return "I will not send text starting with a hash sign, Claude Code would save it as a memory."
        case "slash":
            if not slash_commands:
                return "Sending slash commands is turned off."
            return f"I can only send these slash commands: {', '.join(slash_commands)}."
        case "newline":
            return "That pane runs no Claude Code, so I only send it a single line."
        case _:
            return "That message has characters I will not type, such as tabs or control characters."


def _message(text: str) -> str:
    """The owner's own words, quoted exactly as they will be typed."""
    return f'"{text}"'


_DROPPED_KIND = {KEYS: "key presses", RESIZE: "resize", CLEAR: "input clearing"}


def dropped_words(dropped: Sequence[Confirmation]) -> str:
    """ "I dropped the earlier message for fpga on dev." (or key presses, or resize)."""
    kinds = sorted({_DROPPED_KIND.get(d.kind, "message") for d in dropped})
    places = ", ".join(sorted({d.place for d in dropped}))
    return f"I dropped the earlier {' and '.join(kinds)} for {places}."


def format_readback(item: Confirmation, *, question: str | None, dropped: Sequence[Confirmation]) -> str:
    """What `remote_send` returns: the target, the question being answered (labelled as the screen's),
    the exact message, the risk and the yes phrase, then the confirmation id for the model."""
    parts: list[str] = [dropped_words(dropped)] if dropped else []
    if question:
        asks = "asks for permission" if item.permission_answer else "asks"
        parts.append(
            f"Ready to send to {item.place}, which {asks}: {quote(question)}. Your message: {_message(item.text)}."
        )
    elif item.permission_answer:
        parts.append(f"Ready to answer the permission question of {item.place} with {_message(item.text)}.")
    else:
        parts.append(f"Ready to send to {item.place}: {_message(item.text)}.")
    if item.risky:
        parts.append(f"Careful, {risk_words(item.risky)}. To send it anyway, say {yes_phrase(item.risky)}.")
    else:
        parts.append("Say yes to send it.")
    parts.append(f"(confirmation {item.id}, do not read this number aloud)")
    return " ".join(parts)


def format_keys_readback(
    item: Confirmation,
    *,
    menu: ScreenMenu | None,
    question: str | None,
    chosen: tuple[int, str] | None,
    dropped: Sequence[Confirmation],
    dialog_shown: bool = True,
) -> str:
    """What `remote_send_keys` returns: the keys, the target, the menu or question on its screen
    (labelled as the screen's), the option the keys should choose, the risk and the yes phrase.
    `dialog_shown` False: a permission answer whose screen has no answer options at its bottom
    (`dispatcher.dialog_bound`, prose that looks like a dialog), said as such."""
    parts: list[str] = [dropped_words(dropped)] if dropped else []
    keys = key_words(item.keys)
    if item.permission_answer and not dialog_shown:
        asked = f" ({quote(question)})" if question else ""
        parts.append(
            f"Ready to press {keys} in {item.place}. Its screen may ask for permission{asked}, but I see no "
            "answer options at its bottom, so the keys go to its input."
        )
    elif item.permission_answer:
        asked = f" It asks for permission: {quote(question)}." if question else " It shows a permission question."
        parts.append(f"Ready to press {keys} in {item.place}.{asked}")
    elif menu is not None:
        # The footer's unusual hints matter only for keys beyond moving, choosing and confirming.
        plain = all(key in ("Up", "Down", "Enter") or key.isdigit() for key in item.keys)
        parts.append(f"Ready to press {keys} in {item.place}. {menu_words(menu, hints=not plain)}")
    elif question:
        parts.append(f"Ready to press {keys} in {item.place}, which asks: {quote(question)}.")
    else:
        parts.append(f"Ready to press {keys} in {item.place}. I see no menu on its screen.")
    if chosen is not None:
        number = f"{chosen[0]}, " if menu is None or menu.numbered else ""
        parts.append(f"That should choose {number}{quote(redact(chosen[1]))}.")
    if item.risky:
        parts.append(f"Careful, {risk_words(item.risky)}. To press them anyway, say {yes_phrase(item.risky)}.")
    else:
        parts.append("Say yes to press them.")
    parts.append(f"(confirmation {item.id}, do not read this number aloud)")
    return " ".join(parts)


def format_resize_readback(
    item: Confirmation, *, current: tuple[int, int] | None, dropped: Sequence[Confirmation]
) -> str:
    parts: list[str] = [dropped_words(dropped)] if dropped else []
    assert item.size is not None
    columns, rows = item.size
    now = f" It is now {current[0]} by {current[1]}." if current is not None else ""
    parts.append(f"Ready to resize the window of {item.place} to {columns} columns by {rows} rows.{now}")
    parts.append(
        "The size stays fixed afterwards, and it also changes the other panes in that window "
        "and every terminal attached to it. To undo it, run tmux set-option -w -u window-size in that "
        "window at the keyboard, which goes back to automatic sizing."
    )
    parts.append("Say yes to resize it.")
    parts.append(f"(confirmation {item.id}, do not read this number aloud)")
    return " ".join(parts)


DRAFT_WORDS = 12
"""At most this many words of a draft are read back by `remote_clear_input`."""
DRAFT_CHARS = 100


def draft_words(lines: Sequence[str]) -> str:
    """The first words of a draft (its lines joined), redacted and clipped."""
    words = " ".join(lines).split()
    text = " ".join(words[:DRAFT_WORDS])
    if len(words) > DRAFT_WORDS:
        text += " ..."
    return clip(redact(text), DRAFT_CHARS)


def format_clear_readback(
    item: Confirmation, *, lines: Sequence[str], scrolled: bool, dropped: Sequence[Confirmation]
) -> str:
    """What `remote_clear_input` returns: the target, the first words of the draft (quoted as the
    screen's), that it is thrown away, and the yes phrase, then the confirmation id for the model."""
    parts: list[str] = [dropped_words(dropped)] if dropped else []
    if lines:
        above = ", and more lines above it" if scrolled else ""
        shown = f"Its screen shows this typed there{above}: {quote(draft_words(lines))}."
    else:
        shown = "Its screen shows a long draft whose first lines are scrolled out of view."
    parts.append(f"Ready to clear what is typed in the input of {item.place}. {shown}")
    parts.append(
        "Clearing throws that text away; I send nothing and press only Ctrl+C once. At the keyboard, "
        "the up arrow may bring it back."
    )
    parts.append("Say yes to clear it.")
    parts.append(f"(confirmation {item.id}, do not read this number aloud)")
    return " ".join(parts)


def format_update_readback(item: Confirmation, *, dropped: Sequence[Confirmation]) -> str:
    """What `remote_update` returns: the machine, the two versions, the key whose signature the hub
    verified, that this changes code there, the yes phrase, then the confirmation id for the model."""
    offer = item.offer
    assert offer is not None
    parts: list[str] = [dropped_words(dropped)] if dropped else []
    parts.append(
        f"Ready to update switchboard-remote on {item.machine} from {offer.from_version} to "
        f"{offer.to_version}, signed by the release key {offer.fingerprint}, verified on the hub."
    )
    parts.append(
        f"Careful, this replaces the programs that run on {item.machine}. {item.machine} checks the "
        "signature with its own pinned key before it installs anything."
    )
    parts.append("Say yes, update it.")
    parts.append(f"(confirmation {item.id}, do not read this number aloud)")
    return " ".join(parts)


UPDATE_REFUSALS: dict[str, str] = {
    "not_allowed": "{machine} does not allow self-update: its switchboard-remote runs without --allow-self-update.",
    "not_installed": "switchboard-remote on {machine} is not installed under its own name, so it cannot replace itself.",
    "install_unsafe": "the files of switchboard-remote on {machine} are writable by others or are symlinks, so it refused to replace them.",
    "too_large": "the release is larger than {machine} accepts.",
    "bad_bundle": "{machine} could not read the release bundle.",
    "unknown_target": "the release names a file {machine} does not install.",
    "bad_signature": "the signature did not check out against the release key pinned on {machine}.",
    "wrong_namespace": "the signature was made for something else, not for a Switchboard release.",
    "no_openssh": "{machine} has no ssh-keygen to check the signature.",
    "old_openssh": "the OpenSSH on {machine} is older than 8.1 and cannot check release signatures.",
    "no_signers": "{machine} has no pinned release key (its release_signers file is missing or empty).",
    "signers_unsafe": "the release_signers file on {machine} is writable by others or is a symlink, so it is not trusted.",
    "bad_manifest": "the signed manifest of the release is not valid.",
    "file_mismatch": "a file of the release does not match its signed checksum.",
    "not_newer": "{machine} already had this version or a newer one installed, and it never goes back to an older release.",
    "min_version": "the release needs a newer switchboard-remote on {machine} first.",
    "bad_state": "the update record on {machine} cannot be read.",
    "self_test_failed": "the new switchboard-remote did not start on {machine}; nothing was replaced.",
    "install_failed": "writing the new files failed on {machine}; the old ones were kept.",
    "locked": "another update was still running on {machine}.",
    "bad_request": "switchboard-remote on {machine} is too old to update itself.",
    "changed": "the published release changed since I read it to you. Ask me again.",
    "slow": "{machine} answered too slowly. Try again in a moment.",
    "not_connected": "{machine} is not connected right now.",
    "busy": "switchboard-remote on {machine} was busy. Try again in a moment.",
    "local": "{machine} runs the hub's own copy of switchboard-remote, which updates with the hub.",
}


def update_refusal(machine: str, code: str) -> str:
    """Why an update was not installed, as a clause."""
    text = UPDATE_REFUSALS.get(code)
    return text.format(machine=machine) if text else f"{machine} could not be reached."


def format_update_outcome(item: Confirmation, outcome: str, detail: str | None, *, agent: bool) -> str:
    """The spoken result of a confirmed update."""
    offer = item.offer
    assert offer is not None
    machine = item.machine
    if outcome == "sent":
        then = (
            "Its connector starts the new version now, so it is back in a few seconds."
            if agent
            else "The next call to it runs the new version."
        )
        return f"Updated switchboard-remote on {machine} to {offer.to_version}. {then}"
    if outcome == "unknown":
        return (
            f"I do not know whether {machine} installed the update: it stopped answering. Ask me for the "
            f"sessions on {machine} in a moment; its version shows there."
        )
    code = outcome.removeprefix("not_sent:")
    return "Not updated: " + update_refusal(machine, code)


_NOT_SENT: dict[str, str] = {
    "not_connected": "Not sent: {machine} is not connected right now.",
    "busy": "Not sent: switchboard-remote on {machine} was busy. Try again in a moment.",
    "gone": "Not sent: the pane is gone.",
    "changed": "Not sent: that pane now runs a different session.",
    "dialog_changed": "Not sent: the permission question you answered is no longer on that screen.",
    "vim_normal": "Not sent: that session's input is in vim normal mode, so the text would be read as commands.",
    "not_claude": "Not sent: switchboard-remote on {machine} does not allow typing into panes without Claude Code.",
    "not_allowed": "Not sent: switchboard-remote on {machine} does not allow sending.",
    "not_enabled": "Not sent: sending is not enabled for {machine}.",
    "bad_text": "Not sent: switchboard-remote on {machine} refused the text.",
    "bad_request": "Not sent: switchboard-remote on {machine} is too old for sending. Update it there.",
    "bad_policy": "Not sent: switchboard-remote on {machine} has no valid policy.",
    "no_policy": "Not sent: switchboard-remote on {machine} has no valid policy.",
    "tmux_failed": "Not sent: tmux failed on {machine}.",
    "slow": "Not sent: {machine} answered too slowly. Try again in a moment.",
    "locked": "Not sent: another send to {machine} was still running. Try again in a moment.",
}


_NOT_DONE_KEYS: dict[str, str] = {
    "menu_changed": "Not pressed: the menu on that screen changed since I read it to you. Ask me to read it again.",
    "mode_keys": "Not pressed: there is no menu on that screen; tab and shift tab there would change Claude Code's mode.",
    "key_order": "Not pressed: switchboard-remote on {machine} presses a number, enter, escape, y or n only as the last key.",
    "history_submit": "Not pressed: there is no menu on that screen; up or down and then enter would send an earlier prompt or command again.",
    "menu_unreadable": "Not pressed: that screen now shows a menu I cannot read. Ask me what it says.",
    "dialog_not_visible": "Not pressed: the permission question is not visible on that screen. Check it at the keyboard.",
    "suggestion_present": "Not pressed: that input now shows a prompt Claude Code suggests, and right arrow would accept it. Tell me the message instead.",
    "bad_request": "Not pressed: switchboard-remote on {machine} refused the request. If it is older than 0.5.0, update it there.",
}
_NOT_DONE_CLEAR: dict[str, str] = {
    "nothing_to_clear": "Not cleared: nothing is typed in that input any more.",
    "draft_changed": "Not cleared: what is typed there changed since I read it to you. Ask me to read it again.",
    "not_idle": "Not cleared: that session is working or waiting for an answer now, and Ctrl+C would interrupt it.",
    "menu_present": "Not cleared: that screen now shows a menu, and Ctrl+C would close it. Ask me what it says.",
    "exit_armed": "Not cleared: that screen asks to press Ctrl-C again to exit, so I pressed nothing. Try again in a few seconds.",
    "vim_normal": "Not cleared: that input is in vim normal mode. Clear it at the keyboard.",
    "not_claude": "Not cleared: that pane no longer runs Claude Code.",
    "no_input_box": "Not cleared: I cannot see the input box on that screen any more, so I pressed nothing. Clear it at the keyboard.",
    "bad_request": "Not cleared: switchboard-remote on {machine} refused the request. If it is older than 0.11.0, update it there.",
}
_NOT_DONE_RESIZE: dict[str, str] = {
    "not_claude": "Not resized: switchboard-remote on {machine} does not allow resizing panes without Claude Code.",
    "bad_request": "Not resized: switchboard-remote on {machine} refused the request. If it is older than 0.5.0, update it there.",
}


def format_confirm_outcome(item: Confirmation, outcome: str, detail: str | None) -> str:
    """The spoken result of `remote_send_confirm` for an audit outcome."""
    machine = item.machine
    code = outcome.removeprefix("not_sent:")
    if item.kind == KEYS:
        if outcome == "sent":
            return f"Pressed {key_words(item.keys)} in {item.place}."
        if outcome == "unknown":
            session = item.place.removesuffix(f" on {machine}")
            return f"I do not know whether the keys arrived, {machine} stopped answering. Ask me what {session} is doing before pressing them again."
        if code in _NOT_DONE_KEYS:
            return _NOT_DONE_KEYS[code].format(machine=machine)
        return "Not pressed: " + _confirm_problem(item, outcome).removeprefix("Not sent: ")
    if item.kind == CLEAR:
        if outcome == "sent":
            return f"Cleared the input of {item.place}."
        if outcome == "not_cleared":
            left = f" (about {detail} characters)" if detail else ""
            return (
                f"I pressed Ctrl+C once in {item.place}, but its input still shows text{left}. I pressed "
                "nothing else. Ask me what its screen says, or clear it at the keyboard."
            )
        if outcome == "not_verified":
            return (
                f"I pressed Ctrl+C once in {item.place}, but could not read its screen afterwards. Ask me "
                "what its screen says before clearing again."
            )
        if outcome == "unknown":
            session = item.place.removesuffix(f" on {machine}")
            return f"I do not know whether the input was cleared, {machine} stopped answering. Ask me what {session} is doing before clearing again."
        if code in _NOT_DONE_CLEAR:
            return _NOT_DONE_CLEAR[code].format(machine=machine)
        return "Not cleared: " + _confirm_problem(item, outcome).removeprefix("Not sent: ")
    if item.kind == RESIZE:
        if outcome == "sent":
            assert item.size is not None
            return (
                f"Resized the window of {item.place} to {item.size[0]} by {item.size[1]}. It stays that size "
                "until someone runs tmux set-option -w -u window-size in that window."
            )
        if outcome == "unknown":
            return f"I do not know whether the window was resized, {machine} stopped answering."
        if code in _NOT_DONE_RESIZE:
            return _NOT_DONE_RESIZE[code].format(machine=machine)
        return "Not resized: " + _confirm_problem(item, outcome).removeprefix("Not sent: ")
    return _confirm_problem(item, outcome)


def _confirm_problem(item: Confirmation, outcome: str) -> str:
    machine = item.machine
    if outcome == "sent":
        return f"Sent to {item.place}."
    if outcome == "pasted_no_enter":
        return "I typed it but did not press Enter, because a permission question just opened. Check that session at the keyboard."
    if outcome == "unknown":
        session = item.place.removesuffix(f" on {machine}")
        return f"I do not know whether it arrived, {machine} stopped answering. Ask me what {session} is doing before sending again."
    code = outcome.removeprefix("not_sent:")
    if code == "permission_prompt":
        return "Not sent: that session is now waiting for a permission decision. Approve it at the keyboard or in the Claude app."
    if code == "draft_present":
        return (
            f"Not sent: something is already typed in the input of {item.place}. If it can go, say "
            f"clear the input of {item.place} first."
        )
    if code in _NOT_SENT:
        return _NOT_SENT[code].format(machine=machine)
    return f"Not sent: {machine} could not be reached."
