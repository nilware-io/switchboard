"""Spoken results of the remote tools (docs/SWITCHBOARD.md, "Tools").

The hub's own facts come first; anything that programs or sessions on a machine control (screen
text, summaries, `waitingFor`, window and Claude Code session names) is quoted and labelled as
coming from the screen, never spoken as a sentence of the hub's own. Names are cleaned and clipped
to 40 characters.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..text import plural
from .inventory import MachineView
from .pending import PendingEntry, PendingReport, waits_for_permission
from .protocol import ClaudeInfo, PaneInfo
from .redact import clean_name, redact
from .send import Confirmation, risk_words, yes_phrase
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
    "other": "something went wrong",
}


def unreachable_reason(error: TransportError | None, detail: str | None = None) -> str:
    if error == "refused_by_policy" and detail in ("no_policy", "bad_policy"):
        return "switchboard-remote there has no valid policy"
    return UNREACHABLE.get(error or "other", UNREACHABLE["other"])


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
        return f"{name}: not reachable, {unreachable_reason(view.error, view.detail)}."
    sessions = view.sessions()
    cut = " It has more panes than I can list, so some are missing." if view.truncated else ""
    if not sessions:
        return f"{name}: no allowed tmux sessions.{cut}"
    items = list(sessions.items())
    shown = ". ".join(_session_line(session, panes) for session, panes in items[:SESSIONS_PER_MACHINE])
    more = f" And {len(items) - SESSIONS_PER_MACHINE} more." if len(items) > SESSIONS_PER_MACHINE else ""
    return f"{name}: {plural(len(items), 'session')}. {shown}.{more}{cut}"


def format_list(views: Sequence[MachineView], *, total_machines: int) -> str:
    if total_machines == 0:
        return "No machines are configured."
    lines = [f"{plural(len(views), 'machine')}."] if len(views) > 1 else []
    lines += [machine_line(view) for view in views]
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


def format_status(label: str, pane: PaneInfo, *, summary: str | None, line: str | None) -> str:
    fact = status_fact(pane)
    if summary:
        return f"{label}: {fact} A summary of the screen: {quote(summary)}"
    if line:
        return f"{label}: {fact} The screen says: {quote(redact(line))}"
    return f"{label}: {fact} The screen is empty."


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


def format_readback(item: Confirmation, *, question: str | None, dropped: Sequence[Confirmation]) -> str:
    """What `remote_send` returns: the target, the question being answered (labelled as the screen's),
    the exact message, the risk and the yes phrase, then the confirmation id for the model."""
    parts: list[str] = []
    if dropped:
        places = ", ".join(sorted({d.place for d in dropped}))
        parts.append(f"I dropped the earlier message for {places}.")
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


_NOT_SENT: dict[str, str] = {
    "gone": "Not sent: the pane is gone.",
    "changed": "Not sent: that pane now runs a different session.",
    "dialog_changed": "Not sent: the permission question you answered is no longer on that screen.",
    "draft_present": "Not sent: something is already typed in that session's input.",
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
}


def format_confirm_outcome(item: Confirmation, outcome: str, detail: str | None) -> str:
    """The spoken result of `remote_send_confirm` for an audit outcome."""
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
    if code in _NOT_SENT:
        return _NOT_SENT[code].format(machine=machine)
    return f"Not sent: {machine} could not be reached."
