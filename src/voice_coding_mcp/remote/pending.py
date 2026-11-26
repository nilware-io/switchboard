"""Which sessions wait on the owner, and what each asks (docs/SWITCHBOARD.md, "pending_input").

A Claude Code pane waits when its registry `status` is `waiting` (what it asks: `waitingFor`, or the
permission dialog or question on its screen), or when it is `idle` and the last lines of its screen
show a permission dialog or end with a question. Captures are made only for such panes, at most
`MAX_CAPTURES` per machine and within the caller's deadline. The hub's own registry adds local
sessions that do not run inside tmux, filtered by `projectRoots` like the upstream session tools.

Everything taken from a screen or a registry is untrusted text: it is cleaned, redacted and clipped
here, and quoted by the speech layer. The fast path never runs the summariser.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..log import Logger
from ..projects import is_inside
from ..sessions import LiveSession, read_live_sessions
from ..text import clip
from .inventory import Inventory, MachineView
from .protocol import CaptureResponse, PaneInfo
from .redact import clean_name, clean_screen, redact
from .summarize import is_border, is_input_line, is_status_line, strip_box

CAPTURE_LINES = 15
"""Screen lines a question check reads (the tail of the pane)."""
MAX_CAPTURES = 6
"""Panes captured per machine for one check; waiting panes go first."""
QUESTION_MAX = 100
MIN_CAPTURE_BUDGET_SECONDS = 0.5
LOCAL = "local"
"""Session keys of the hub's own registry start with this word (machine names cannot contain `|`)."""

PendingKind = Literal["permission", "question"]
PendingSource = Literal["registry", "screen"]

_DIALOG_RE = re.compile(r"\bdo you want to\b[^?]{0,200}\?", re.I)
_GENERIC_DIALOG_RE = re.compile(r"^do you want to proceed\s*\?", re.I)
_YES_RE = re.compile(r"^(?:[❯>›▶]\s*)?1[.)]\s*yes\b", re.I)
_NO_RE = re.compile(r"^(?:[❯>›▶]\s*)?[2-9][.)]\s*no\b", re.I)
_MARKER_RE = re.compile(r"^[⏺●•◆✻*]\s*")


def waits_for_permission(waiting_for: str | None) -> bool:
    """A registry `waitingFor` that names a permission decision."""
    return "permission" in (waiting_for or "").lower()


@dataclass(frozen=True)
class ScreenAsk:
    kind: PendingKind
    text: str | None
    """Redacted and clipped to QUESTION_MAX; None when the screen gives nothing to quote."""


def _question_text(value: str | None) -> str | None:
    if not value:
        return None
    text = redact(clean_name(value, 400))
    return clip(text, QUESTION_MAX) if text else None


def _tail(screen: str) -> list[str]:
    lines = [strip_box(raw) for raw in screen.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines[-CAPTURE_LINES:]


def _dialog_title(lines: Sequence[str], anchor: int, question: str | None) -> str | None:
    """What a permission dialog asks: a specific question line ("Do you want to make this edit to
    parser.py?") as is, else the first two lines of the dialog above it ("Bash command",
    "git push origin main"), else the question line."""
    if question and not _GENERIC_DIALOG_RE.match(question):
        return question
    content: list[str] = []
    for line in reversed(lines[:anchor]):
        if not line:
            continue
        if is_border(line):
            break
        content.append(line)
    content.reverse()
    if content:
        return " ".join(content[:2])
    return question


def screen_ask(screen: str) -> ScreenAsk | None:
    """A permission dialog ("Do you want to proceed?", or numbered options with Yes and No) or a
    question (the last assistant line above the input box ends with `?`) in the last
    CAPTURE_LINES lines of a cleaned screen, else None."""
    lines = _tail(screen)
    anchor: int | None = None
    question: str | None = None
    for index, line in enumerate(lines):
        found = _DIALOG_RE.search(line)
        if found is not None:
            anchor, question = index, found.group(0)
            break
    if anchor is None:
        yes = next((i for i, line in enumerate(lines) if _YES_RE.match(line)), None)
        if yes is not None and any(_NO_RE.match(line) for line in lines[yes + 1 :]):
            anchor = yes
    if anchor is not None:
        return ScreenAsk("permission", _question_text(_dialog_title(lines, anchor, question)))
    inputs = [i for i, line in enumerate(lines) if is_input_line(line)]
    above = lines[: inputs[-1]] if inputs else lines
    for line in reversed(above):
        if is_border(line) or is_status_line(line) or is_input_line(line):
            continue
        text = _MARKER_RE.sub("", line).strip()
        if text.rstrip(" \"')").endswith("?"):
            return ScreenAsk("question", _question_text(text))
        return None
    return None


@dataclass(frozen=True)
class TrackedSession:
    """A Claude Code session the check saw, for the watcher's turn tracking."""

    key: str
    """`<machine>|<paneId>|<sessionId>`, or `local|<sessionId>` for the hub's own registry."""
    machine: str | None
    """None for the hub's own registry."""
    place: str
    """Names fit for a push: owner-chosen tmux session and machine names, or a project folder
    ("fpga on dev", "fpga window 2 on dev", "A session in alpha on this machine"); never a Claude
    Code session name, which a session sets itself."""
    status: str | None
    status_updated_at: int | None


@dataclass(frozen=True)
class PendingEntry:
    key: str
    """`<session key>|<kind>`: the watcher's identity of this wait."""
    session: TrackedSession
    label: str
    """Spoken: "On dev, fpga" or 'On this machine, session "billing fix" in alpha'."""
    kind: PendingKind
    question: str | None
    source: PendingSource
    """`registry` (status `waiting`) or `screen` (the heuristic only)."""
    permission_answers: bool
    """The machine opts in to answering permission prompts (`allowPermissionAnswers`)."""


@dataclass(frozen=True)
class PendingReport:
    entries: tuple[PendingEntry, ...]
    sessions: tuple[TrackedSession, ...]
    views: tuple[MachineView, ...]
    partial: tuple[str, ...]
    """Machines where some panes could not be checked (capture failed, too many, out of time)."""
    unknown: frozenset[str]
    """Session keys whose screen could not be checked this time."""


@dataclass(frozen=True)
class _Plan:
    view: MachineView
    claude_panes: dict[str, int]
    """Claude Code panes per tmux session (labels name the window when there are several)."""
    capture: tuple[PaneInfo, ...]
    skipped: tuple[PaneInfo, ...]


_Screen = tuple[tuple[str, str], CaptureResponse | Literal["gone"] | None]
"""((machine, pane id), what its capture gave)."""


def _session_key(machine: str, pane: PaneInfo) -> str:
    sid = pane.claude.session_id if pane.claude and pane.claude.session_id else "-"
    return f"{machine}|{pane.pane_id}|{sid}"


def _plan(view: MachineView) -> _Plan:
    claude = [pane for pane in view.panes if pane.claude is not None]
    per_session: dict[str, int] = {}
    for pane in claude:
        per_session[pane.session] = per_session.get(pane.session, 0) + 1
    waiting = [p for p in claude if p.claude and p.claude.status == "waiting"]
    idle = [p for p in claude if p.claude and p.claude.status == "idle"]
    wanted = waiting + idle
    return _Plan(
        view=view,
        claude_panes=per_session,
        capture=tuple(wanted[:MAX_CAPTURES]),
        skipped=tuple(p for p in idle if p not in wanted[:MAX_CAPTURES]),
    )


def _pane_place(machine: str, pane: PaneInfo, plan: _Plan) -> tuple[str, str]:
    """(push place, spoken label) of a tmux pane."""
    name = clean_name(pane.session) or "unnamed"
    if plan.claude_panes.get(pane.session, 0) > 1 and pane.window_index is not None:
        name += f" window {pane.window_index}"
    return f"{name} on {machine}", f"On {machine}, {name}"


@dataclass
class PendingCheckerOptions:
    inventory: Inventory
    logger: Logger
    project_roots: Sequence[str]
    claude_home: str
    """The hub's own Claude Code home, for local sessions that do not run in tmux."""
    read_live: Callable[[str], Mapping[str, LiveSession]] = read_live_sessions


class PendingChecker:
    """Never raises for machine failures; they are reported in the views."""

    def __init__(self, options: PendingCheckerOptions) -> None:
        self._opts = options
        self._roots = [os.path.realpath(root) for root in options.project_roots]

    async def check(self, names: Sequence[str], *, include_local: bool, left: Callable[[], float]) -> PendingReport:
        """Checks `names` in parallel (with the hub's registry when `include_local`): per machine its
        list, then its waiting and idle Claude Code panes captured in parallel, all within `left()`
        seconds."""
        inventory = self._opts.inventory

        async def local() -> Mapping[str, LiveSession]:
            if not include_local:
                return {}
            try:
                return await asyncio.to_thread(self._opts.read_live, self._opts.claude_home)
            except Exception as err:
                self._opts.logger.warn("pending: local registry failed", {"error": type(err).__name__})
                return {}

        async def capture(machine: str, pane: PaneInfo) -> CaptureResponse | Literal["gone"] | None:
            """The screen, "gone" when the pane no longer exists (it waits for nobody), or None when it
            could not be read in time."""
            budget = left()
            if budget < MIN_CAPTURE_BUDGET_SECONDS:
                return None
            result = await inventory.capture(machine, pane.pane_id, CAPTURE_LINES, budget_seconds=budget)
            if isinstance(result, CaptureResponse):
                return result
            return "gone" if result.detail == "gone" else None

        async def one_machine(name: str) -> tuple[MachineView, _Plan | None, list[_Screen]]:
            """One machine: its list, then its captures in parallel (a slow machine does not hold
            the captures of the others)."""
            view = await inventory.view(name, budget_seconds=left())
            if not view.ok:
                return view, None, []
            plan = _plan(view)
            screens = await asyncio.gather(*(capture(name, pane) for pane in plan.capture))
            return (
                view,
                plan,
                [((name, pane.pane_id), screen) for pane, screen in zip(plan.capture, screens, strict=True)],
            )

        results, live = await asyncio.gather(asyncio.gather(*(one_machine(name) for name in names)), local())
        views = [view for view, _, _ in results]
        plans = [plan for _, plan, _ in results if plan is not None]
        captured = {key: screen for _, _, screens in results for key, screen in screens}

        entries: list[PendingEntry] = []
        sessions: list[TrackedSession] = []
        partial: list[str] = []
        unknown: set[str] = set()
        for plan in plans:
            machine = plan.view.machine
            missed = bool(plan.skipped)
            for pane in plan.view.panes:
                claude = pane.claude
                if claude is None:
                    continue
                place, label = _pane_place(machine.name, pane, plan)
                tracked = TrackedSession(
                    key=_session_key(machine.name, pane),
                    machine=machine.name,
                    place=place,
                    status=claude.status,
                    status_updated_at=claude.status_updated_at,
                )
                sessions.append(tracked)
                if claude.status not in ("waiting", "idle"):
                    continue
                checked = (machine.name, pane.pane_id) in captured
                response = captured.get((machine.name, pane.pane_id))
                if response == "gone":
                    continue
                ask = screen_ask(clean_screen(response.text)) if isinstance(response, CaptureResponse) else None
                if claude.status == "idle" and ((checked and response is None) or pane in plan.skipped):
                    # Only an idle pane depends on its screen; a waiting one is known from the registry.
                    missed = True
                    unknown.add(tracked.key)
                if claude.status == "waiting":
                    kind: PendingKind = "permission" if waits_for_permission(claude.waiting_for) else "question"
                    text = ask.text if ask is not None and ask.kind == kind and ask.text else None
                    entries.append(
                        PendingEntry(
                            key=f"{tracked.key}|{kind}",
                            session=tracked,
                            label=label,
                            kind=kind,
                            question=text or _question_text(claude.waiting_for),
                            source="registry",
                            permission_answers=machine.allow_permission_answers,
                        )
                    )
                elif ask is not None:
                    entries.append(
                        PendingEntry(
                            key=f"{tracked.key}|{ask.kind}",
                            session=tracked,
                            label=label,
                            kind=ask.kind,
                            question=ask.text,
                            source="screen",
                            permission_answers=machine.allow_permission_answers,
                        )
                    )
            if missed:
                partial.append(machine.name)
        for sid, session in live.items():
            entry = self._local_entry(sid, session)
            if entry is None:
                continue
            tracked, pending = entry
            sessions.append(tracked)
            if pending is not None:
                entries.append(pending)
        return PendingReport(
            entries=tuple(entries),
            sessions=tuple(sessions),
            views=tuple(views),
            partial=tuple(partial),
            unknown=frozenset(unknown),
        )

    def _project(self, cwd: str | None) -> str | None:
        """The project folder of a cwd strictly inside an allowed root."""
        if not cwd:
            return None
        real = os.path.realpath(cwd)
        for root in self._roots:
            if is_inside(real, root):
                return os.path.relpath(real, root).split(os.sep)[0]
        return None

    def _local_entry(self, sid: str, session: LiveSession) -> tuple[TrackedSession, PendingEntry | None] | None:
        """A session of the hub's own registry: not in tmux (machines cover those), not a headless
        run (`sdk-cli`, which never waits on the owner), inside `projectRoots`."""
        if session.tmux or session.entrypoint == "sdk-cli":
            return None
        project = self._project(session.cwd)
        if project is None:
            return None
        folder = clean_name(project) or "a project"
        tracked = TrackedSession(
            key=f"{LOCAL}|{sid}",
            machine=None,
            place=f"A session in {folder} on this machine",
            status=session.status,
            status_updated_at=session.status_updated_at_ms,
        )
        if session.status != "waiting":
            return tracked, None
        name = clean_name(session.name)
        quoted = '"' + name.replace('"', "'") + '"'
        label = f"On this machine, session {quoted} in {folder}" if name else f"On this machine, {folder}"
        kind: PendingKind = "permission" if waits_for_permission(session.waiting_for) else "question"
        return tracked, PendingEntry(
            key=f"{tracked.key}|{kind}",
            session=tracked,
            label=label,
            kind=kind,
            question=_question_text(session.waiting_for),
            source="registry",
            permission_answers=False,
        )
