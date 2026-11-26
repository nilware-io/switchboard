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
from .protocol import ClaudeInfo, PaneInfo
from .redact import clean_name, redact
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
    return "permission" in (claude.waiting_for or "").lower()


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
    if not sessions:
        return f"{name}: no allowed tmux sessions."
    items = list(sessions.items())
    shown = ". ".join(_session_line(session, panes) for session, panes in items[:SESSIONS_PER_MACHINE])
    more = f" And {len(items) - SESSIONS_PER_MACHINE} more." if len(items) > SESSIONS_PER_MACHINE else ""
    return f"{name}: {plural(len(items), 'session')}. {shown}.{more}"


def format_list(views: Sequence[MachineView], *, total_machines: int) -> str:
    if total_machines == 0:
        return "No machines are configured."
    lines = [f"{plural(len(views), 'machine')}."] if len(views) > 1 else []
    lines += [machine_line(view) for view in views]
    return "\n".join(lines)


def pane_label(machine: str, pane: PaneInfo, windows: int) -> str:
    """ "dev, fpga" or, in a session with several windows, "dev, fpga window 2"."""
    label = f"{machine}, {clean_name(pane.session) or 'unnamed'}"
    if windows > 1 and pane.window_index is not None:
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
