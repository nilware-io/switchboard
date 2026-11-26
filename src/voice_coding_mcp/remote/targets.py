"""A spoken target (tmux session, `session:window.pane`, or a Claude Code session name) resolved to one
pane of a machine's view (docs/SWITCHBOARD.md, "Target resolution").

Only panes of allowed sessions are in the view, so a session outside the allow-list answers exactly
like a missing one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..projects import MatchOk, is_path_like, match_by_name
from .inventory import MachineView
from .protocol import PaneInfo

LITERAL_TARGET_RE = re.compile(r"([A-Za-z0-9_.-]{1,64})(?::([A-Za-z0-9_-]{1,64})(?:\.([0-9]{1,3}))?)?", re.ASCII)
_SHELL_META_RE = re.compile(r"[;&|`$<>(){}\[\]\\'\"*?!~\n\r\t]")


@dataclass(frozen=True)
class Resolved:
    pane: PaneInfo
    windows: int
    """Windows of the pane's session."""
    name_window: bool = False
    """The spoken label names the window: the session has several windows and the pane is not its
    only Claude Code pane ("dev, fpga" for the only one, "dev, fpga window 2" otherwise)."""


@dataclass(frozen=True)
class NotFound:
    target: str
    sessions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Ambiguous:
    """Several sessions match, or one session has several Claude Code panes (`panes` set)."""

    target: str
    sessions: list[str] = field(default_factory=list)
    panes: list[PaneInfo] = field(default_factory=list)


@dataclass(frozen=True)
class NoTarget:
    """No target given and the machine has not exactly one Claude Code pane."""

    sessions: list[str] = field(default_factory=list)


TargetResult = Resolved | NotFound | Ambiguous | NoTarget


def _window_count(panes: Sequence[PaneInfo]) -> int:
    return len({pane.window_index for pane in panes})


def _resolved(pane: PaneInfo, all_session_panes: Sequence[PaneInfo]) -> Resolved:
    windows = _window_count(all_session_panes)
    only_claude = pane.claude is not None and sum(p.claude is not None for p in all_session_panes) == 1
    return Resolved(pane=pane, windows=windows, name_window=windows > 1 and not only_claude)


def pick_pane(session_panes: Sequence[PaneInfo], target: str, all_session_panes: Sequence[PaneInfo]) -> TargetResult:
    """One pane out of a session or window: the only pane, else the only Claude Code pane, else the
    active pane when none runs Claude Code."""
    if len(session_panes) == 1:
        return _resolved(session_panes[0], all_session_panes)
    claude = [pane for pane in session_panes if pane.claude is not None]
    if len(claude) == 1:
        return _resolved(claude[0], all_session_panes)
    if len(claude) > 1:
        return Ambiguous(target=target, panes=claude)
    active = [pane for pane in session_panes if pane.active] or list(session_panes)
    return _resolved(active[0], all_session_panes)


def _literal(view: MachineView, target: str) -> TargetResult | None:
    match = LITERAL_TARGET_RE.fullmatch(target.strip())
    if match is None:
        return None
    session_name, window, pane_index = match.groups()
    sessions = view.sessions()
    panes = sessions.get(session_name)
    if panes is None:
        return None
    chosen = panes
    if window is not None:
        chosen = [p for p in panes if (str(p.window_index) == window if window.isdigit() else p.window_name == window)]
        if pane_index is not None:
            chosen = [p for p in chosen if str(p.pane_index) == pane_index]
        if not chosen:
            return NotFound(target=target, sessions=list(sessions))
    return pick_pane(chosen, session_name, panes)


def resolve_target(view: MachineView, target: str | None) -> TargetResult:
    sessions = view.sessions()
    names = list(sessions)
    if target is None or not target.strip():
        claude = [pane for pane in view.panes if pane.claude is not None]
        if len(claude) == 1:
            return _resolved(claude[0], sessions[claude[0].session])
        return NoTarget(sessions=names)
    if is_path_like(target) or _SHELL_META_RE.search(target):
        return NotFound(target=target, sessions=names)
    literal = _literal(view, target)
    if literal is not None:
        return literal
    by_session = match_by_name(names, lambda name: name, target)
    if isinstance(by_session, MatchOk):
        return pick_pane(sessions[by_session.item], by_session.item, sessions[by_session.item])
    if by_session.reason == "ambiguous":
        return Ambiguous(target=target, sessions=by_session.candidates)
    named = [pane for pane in view.panes if pane.claude is not None and pane.claude.name]
    by_claude = match_by_name(named, lambda pane: pane.claude.name if pane.claude and pane.claude.name else "", target)
    if isinstance(by_claude, MatchOk):
        pane = by_claude.item
        return _resolved(pane, sessions[pane.session])
    if by_claude.reason == "ambiguous":
        return Ambiguous(target=target, sessions=sorted({p.session for p in named if p.claude}))
    return NotFound(target=target, sessions=names)
