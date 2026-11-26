"""The notification watcher (docs/SWITCHBOARD.md, "Notification watcher"): a push when a session
newly needs a decision or finished a long turn, and optionally when a machine stops or starts
answering.

Off unless `watch.intervalSeconds` > 0. Each tick runs the `pending_input` fast path. A wait is
identified by (machine, pane id, Claude Code session id, kind); a registry wait pushes on its first
sighting, a wait seen only on the screen after 2 ticks in a row, and a wait pushes again only after
it was gone for a tick. A wait whose screen or machine could not be checked counts as still there.
The announced waits and unreachable machines are kept in `stateDir/watch.json`, so a restart does
not repeat pushes.

Pushes name only the machine and the tmux session (or the project folder of a local session), never
a Claude Code session name and never pane text; with `watch.includeQuestion` the redacted question
is appended. The watcher never sends anything into a pane.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..clock import MINUTE_MS, Clock, now_ms
from ..json_file import ensure_dir, read_json_object, write_json_atomic
from ..log import Logger
from ..notify import TextSender
from ..text import clip
from .config import WatchConfig
from .pending import PendingEntry, PendingReport, TrackedSession

HEURISTIC_TICKS = 2
"""Ticks in a row a screen-only wait must be seen before it pushes."""
UNREACHABLE_TICKS = 3
PUSH_QUESTION_MAX = 120
STATE_VERSION = 1
MAX_STATE_KEYS = 4096
MAX_STATE_BYTES = 1024 * 1024


def _quoted(text: str) -> str:
    return '"' + clip(text, PUSH_QUESTION_MAX).replace('"', "'") + '"'


def entry_text(entry: PendingEntry, include_question: bool) -> str:
    """ "fpga on dev needs a decision." or "fpga on dev asks you something.", plus the quoted
    question with `include_question`."""
    what = "needs a decision" if entry.kind == "permission" else "asks you something"
    text = f"{entry.session.place} {what}"
    if include_question and entry.question:
        return f"{text}: {_quoted(entry.question)}"
    return f"{text}."


def turn_text(session: TrackedSession, minutes: int) -> str:
    return f"{session.place} finished its turn after {minutes} minutes."


@dataclass
class WatcherOptions:
    watch: WatchConfig
    check: Callable[[], Awaitable[PendingReport]]
    """The `pending_input` fast path (`RemoteTools.collect_pending`)."""
    send: TextSender
    state_dir: str
    logger: Logger
    now: Clock | None = None


class Watcher:
    """Call `tick` directly (tests) or `start` the periodic loop and `stop` it at shutdown."""

    def __init__(self, options: WatcherOptions) -> None:
        self._opts = options
        self._watch = options.watch
        self._now = options.now or now_ms
        self._path = os.path.join(options.state_dir, "watch.json")
        self._announced: set[str] = set()
        self._down: set[str] = set()
        self._seen: dict[str, int] = {}
        """Screen-only waits not yet announced: ticks in a row they were seen."""
        self._busy: dict[str, int] = {}
        """Session key: when its current busy turn started (ms)."""
        self._fails: dict[str, int] = {}
        self._deferred: dict[str, str] = {}
        """Session key: a "finished its turn" push held back for one tick, because a screen-only wait
        of the same session was still being confirmed (one push per event)."""
        self._loop: asyncio.Task[None] | None = None
        self._load()

    @property
    def enabled(self) -> bool:
        return self._watch.interval_seconds > 0

    def start(self) -> None:
        if self.enabled and self._loop is None:
            self._loop = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._watch.interval_seconds)
            await self.safe_tick()

    async def safe_tick(self) -> None:
        """A tick of the loop: a failure is logged by its type only (its text may come from a
        machine) and the loop goes on."""
        try:
            await self.tick()
        except Exception as err:
            self._opts.logger.error("watch tick failed", {"error": type(err).__name__})

    # --- state file ---------------------------------------------------------------------------

    def _load(self) -> None:
        data = read_json_object(self._path, MAX_STATE_BYTES)
        if data is None or data.get("version") != STATE_VERSION:
            return

        def keys(name: str) -> set[str]:
            value = data.get(name)
            if not isinstance(value, list):
                return set()
            return {item for item in value[:MAX_STATE_KEYS] if isinstance(item, str) and len(item) <= 256}

        self._announced = keys("announced")
        self._down = keys("unreachable")

    def _save(self) -> None:
        data = {"version": STATE_VERSION, "announced": sorted(self._announced), "unreachable": sorted(self._down)}
        try:
            ensure_dir(os.path.dirname(self._path))
            write_json_atomic(self._path, data)
        except OSError as err:
            self._opts.logger.warn("watch state not saved", {"error": type(err).__name__})

    # --- one tick -----------------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """One check: sends the pushes that are due and returns their texts."""
        report = await self._opts.check()
        now = self._now()
        before = (frozenset(self._announced), frozenset(self._down))
        pushes: list[str] = []
        pushed_sessions: set[str] = set()

        down_machines = {view.machine.name for view in report.views if not view.ok}

        def unknown(session_key: str) -> bool:
            machine = session_key.split("|", 1)[0]
            return machine in down_machines or session_key in report.unknown

        current = {entry.key: entry for entry in report.entries}
        for key, entry in current.items():
            if key in self._announced:
                continue
            if entry.source == "screen":
                self._seen[key] = self._seen.get(key, 0) + 1
                if self._seen[key] < HEURISTIC_TICKS:
                    continue
            self._seen.pop(key, None)
            self._announced.add(key)
            pushed_sessions.add(entry.session.key)
            pushes.append(entry_text(entry, self._watch.include_question))
            self._opts.logger.info(
                "watch push", {"event": entry.kind, "machine": entry.session.machine or "local", "source": entry.source}
            )

        def still(key: str) -> bool:
            return key in current or unknown(key.rsplit("|", 1)[0])

        self._announced = {key for key in self._announced if still(key)}
        self._seen = {key: count for key, count in self._seen.items() if still(key)}

        deferred, self._deferred = self._deferred, {}
        pushes += [text for key, text in deferred.items() if key not in pushed_sessions]
        confirming = {current[key].session.key for key in self._seen if key in current}
        pushes += self._turns(report, now, pushed_sessions, unknown, confirming)
        pushes += self._reachability(report)

        if (frozenset(self._announced), frozenset(self._down)) != before:
            self._save()
        for text in pushes:
            await self._opts.send(text)
        return pushes

    def _turns(
        self,
        report: PendingReport,
        now: int,
        pushed: set[str],
        unknown: Callable[[str], bool],
        confirming: set[str],
    ) -> list[str]:
        """ "finished its turn" pushes for sessions that were busy at least `longTurnMinutes` and are
        now idle or waiting. A session that just pushed a wait gets no second push for the same change;
        one whose screen-only wait is still being confirmed (`confirming`) has its push held back a
        tick, and dropped when that wait then pushes."""
        pushes: list[str] = []
        present: set[str] = set()
        for session in report.sessions:
            present.add(session.key)
            if session.status == "busy":
                if session.key not in self._busy:
                    since = session.status_updated_at
                    self._busy[session.key] = since if since is not None and since <= now else now
                continue
            start = self._busy.pop(session.key, None)
            if start is None or session.status not in ("idle", "waiting"):
                continue
            end = session.status_updated_at
            if end is None or not start <= end <= now:
                end = now
            minutes = (end - start) // MINUTE_MS
            limit = self._watch.long_turn_minutes
            if limit > 0 and minutes >= limit and session.key not in pushed:
                if session.key in confirming:
                    self._deferred[session.key] = turn_text(session, minutes)
                    continue
                pushes.append(turn_text(session, minutes))
                self._opts.logger.info("watch push", {"event": "turn", "machine": session.machine or "local"})
        for key in list(self._busy):
            if key not in present and not unknown(key):
                del self._busy[key]
        return pushes

    def _reachability(self, report: PendingReport) -> list[str]:
        pushes: list[str] = []
        for view in report.views:
            name = view.machine.name
            if view.ok:
                self._fails.pop(name, None)
                if name in self._down:
                    self._down.discard(name)
                    if self._watch.notify_unreachable:
                        pushes.append(f"Machine {name} answers again.")
                continue
            self._fails[name] = self._fails.get(name, 0) + 1
            if self._watch.notify_unreachable and self._fails[name] >= UNREACHABLE_TICKS and name not in self._down:
                self._down.add(name)
                pushes.append(f"Machine {name} cannot be reached.")
                self._opts.logger.info("watch push", {"event": "unreachable", "machine": name})
        return pushes
