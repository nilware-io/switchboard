"""Session events from the machines' hooks (docs/SWITCHBOARD.md, "Session hooks").

`switchboard-hook` on a machine spools one file per event (a session waits for approval or an
answer, finished its turn, stopped on an API error, opened a pull request, or wrote a note with
notify_owner). The hub takes them with the dispatcher's `events` verb (0.12.0, `--allow-events`):
for an agent machine that is one frame over the machine's own outbound link, so the machine still
opens no port and makes no other connection. The ids of the events taken are named in the next
call, which deletes them there; until then they stay in the spool, so an event is delivered at
least once, and ids already seen (kept in `stateDir/events.json`) are not delivered twice.

The machine is the link the answer came on (the transport of that configured machine), never a
field of an event. Everything else in an event is the machine's sessions' words: checked again
here (fixed kinds, UUIDs, validated GitHub URLs, cleaned and redacted texts), rate limited per
machine, and shown as such. Each event goes to the incident log as an `event` entry (a `failed`
one opens an incident) and to Discord.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..clock import HOUR_MS, MINUTE_MS, SECOND_MS, now_ms
from ..json_file import ensure_dir, write_json_atomic
from ..log import Logger
from .config import EventsConfig, MachineConfig, RemoteConfig, command_timeout_seconds
from .discord import FAILURE_WORDS, DiscordNotifier, EventView, Item
from .incidents import IncidentLog, IncidentTracker, clean_text, event_entry
from .protocol import EVENT_KINDS, EVENT_LEVELS, EventsResponse, SessionEvent
from .redact import clean_name
from .transport import Transport

STATE_FILE = "events.json"
MAX_SEEN = 4096
MAX_ROUNDS = 8
"""Calls per machine and poll: each takes up to 16 events and acknowledges the previous ones."""
TICK_SECONDS = 1.0
UNSUPPORTED_RETRY_MS = 10 * MINUTE_MS
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
GITHUB_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
GITHUB_REPO = r"[A-Za-z0-9._-]{1,100}"
PR_URL_RE = re.compile(r"https://github\.com/" + GITHUB_NAME + r"/" + GITHUB_REPO + r"/pull/[0-9]{1,9}", re.ASCII)
GITHUB_URL_RE = re.compile(
    r"https://github\.com/" + GITHUB_NAME + r"/" + GITHUB_REPO + r"(?:/[A-Za-z0-9._~%/#=?&+-]{0,200})?", re.ASCII
)
MAX_URL = 300
EVENT_ID_RE = re.compile(r"[0-9]{13}-[0-9]{10}-[0-9a-f]{8}")
WHAT = {
    "approval_needed": "a session waits for approval",
    "question": "a session waits for an answer",
    "finished": "a session finished its turn",
    "pr_opened": "a session opened a pull request",
    "owner_note": "a session sent a note",
}
UNSUPPORTED = ("bad_request", "not_allowed", "events_unsafe")
HOOKS_COVER_MS = 6 * HOUR_MS
"""A machine whose hooks delivered an event this recently counts as covered by its hooks: the
watcher's own "needs a decision" pushes for it stand down (`EventPoller.covers`)."""
COVERED_KINDS = {"permission": "approval_needed", "question": "question"}
"""The watcher's wait kinds and the session event kind that reports the same wait."""
"""Refusals that mean the machine does not hand out events (an older dispatcher, no
`--allow-events`, a spool directory others can write): asked again after UNSUPPORTED_RETRY_MS."""


@dataclass(frozen=True)
class CheckedEvent:
    """A session event after the hub's own checks."""

    id: str
    view: EventView
    time_ms: int


def check_event(event: SessionEvent, machine: MachineConfig, now: int) -> CheckedEvent | None:
    """The event with every field checked again, or None when it is not one the hub knows."""
    if event.kind not in EVENT_KINDS:
        return None
    session_id = event.session_id if event.session_id and UUID_RE.fullmatch(event.session_id) else None
    url: str | None = None
    if event.url is not None and len(event.url) <= MAX_URL:
        pattern = PR_URL_RE if event.kind == "pr_opened" else GITHUB_URL_RE
        url = event.url if pattern.fullmatch(event.url) else None
    if event.kind == "pr_opened" and url is None:
        return None
    if event.kind == "failed":
        message: str | None = event.message if event.message in FAILURE_WORDS else "unknown"
    elif event.kind == "pr_opened":
        message = None
    else:
        message = clean_text(event.message, 500 if event.kind == "owner_note" else 200) or None
    level = event.level if event.kind == "owner_note" and event.level in EVENT_LEVELS else None
    tmux = event.tmux_session
    if tmux is not None and not any(fnmatch.fnmatchcase(tmux, glob) for glob in machine.sessions):
        # The hub shows only tmux sessions it is allowed to name for this machine.
        tmux = None
    # The machine's clock is used when it is plausible, else the hub's.
    moment = event.time if now - 24 * HOUR_MS <= event.time <= now + 5 * MINUTE_MS else now
    return CheckedEvent(
        id=event.id,
        view=EventView(
            kind=event.kind,
            session_id=session_id,
            session_name=clean_name(event.session_name, 64) or None,
            project=clean_name(event.project, 64) or None,
            tmux_session=clean_name(tmux, 64) or None,
            message=message,
            url=url,
            level=level,
        ),
        time_ms=min(moment, now),
    )


def log_detail(view: EventView) -> str:
    """The `why` of a log entry: where it happened, and the session's own words quoted."""
    parts = []
    if view.project:
        parts.append(f"project {view.project}")
    if view.session_name:
        parts.append(f'session "{view.session_name}"')
    if view.tmux_session:
        parts.append(f"tmux {view.tmux_session}")
    where = ", ".join(parts) or "a session"
    if view.kind == "failed":
        return f"{where}: {FAILURE_WORDS.get(view.message or '', FAILURE_WORDS['unknown'])}"
    if view.url:
        where += f": {view.url}"
    if view.message and view.kind != "pr_opened":
        where += f': the session says "{view.message}"'
    return where


@dataclass
class EventPollerOptions:
    remote: RemoteConfig
    events: EventsConfig
    transports: Mapping[str, Transport]
    tracker: IncidentTracker
    log: IncidentLog
    state_dir: str
    logger: Logger
    discord: DiscordNotifier | None = None
    now: Callable[[], int] | None = None


class EventPoller:
    """Polls every machine's `events` verb at its interval; `start` and `stop` with the hub."""

    def __init__(self, options: EventPollerOptions) -> None:
        self._opts = options
        self._now = options.now or now_ms
        self._logger = options.logger
        self._machines = {m.name: m for m in options.remote.machines if m.name in options.transports}
        self._path = os.path.join(options.state_dir, STATE_FILE)
        self._seen: deque[str] = deque(maxlen=MAX_SEEN)
        self._seen_set: set[str] = set()
        self._acks: dict[str, list[str]] = {}
        self._taken: dict[str, list[int]] = {}
        self._last_poll: dict[str, int] = {}
        self._unsupported: dict[str, int] = {}
        self._busy: set[str] = set()
        self._hooked: dict[str, int] = {}
        """Machine name: when the hub last took events from its hooks (ms)."""
        self._loop: asyncio.Task[None] | None = None
        self._polls: set[asyncio.Task[None]] = set()
        self.delivered = 0

    # --- state ------------------------------------------------------------------------------

    def load(self) -> None:
        """Reads `stateDir/events.json`; a broken file is ignored (events may then repeat once)."""
        try:
            with open(self._path, "rb") as handle:
                data = json.loads(handle.read(1024 * 1024).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        seen = data.get("seen")
        for key in seen if isinstance(seen, list) else []:
            if isinstance(key, str) and len(key) <= 128:
                self._remember(key)
        acks = data.get("acks")
        if isinstance(acks, dict):
            for name, ids in acks.items():
                if name in self._machines and isinstance(ids, list):
                    self._acks[name] = [i for i in ids if isinstance(i, str) and EVENT_ID_RE.fullmatch(i)][:16]
        hooked = data.get("hooked")
        if isinstance(hooked, dict):
            for name, at in hooked.items():
                if name in self._machines and isinstance(at, int) and not isinstance(at, bool):
                    self._hooked[name] = at

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            ensure_dir(self._opts.state_dir)
            write_json_atomic(
                self._path, {"version": 1, "seen": list(self._seen), "acks": self._acks, "hooked": self._hooked}
            )

    def _remember(self, key: str) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)

    def covers(self, machine: str, kind: str) -> bool:
        """True when the machine's session hooks report this kind of wait (`permission` or
        `question`) to the owner: events are on, the machine handed out events in the last
        HOOKS_COVER_MS, and Discord is on and takes that event kind. The watcher then does not
        push the same wait again; without Discord the events only reach the incident log, so the
        watcher keeps pushing."""
        event_kind = COVERED_KINDS.get(kind)
        discord = self._opts.discord
        if not self._opts.events.enabled or event_kind is None or discord is None or not discord.wants(event_kind):
            return False
        if machine in self._unsupported:
            return False
        at = self._hooked.get(machine)
        return at is not None and 0 <= self._now() - at < HOOKS_COVER_MS

    # --- loop -------------------------------------------------------------------------------

    def interval_ms(self, machine: MachineConfig) -> int:
        if machine.transport == "ssh":
            return self._opts.events.ssh_poll_seconds * SECOND_MS
        return self._opts.events.poll_seconds * SECOND_MS

    async def start(self) -> None:
        if not self._opts.events.enabled or self._loop is not None:
            return
        self.load()
        self._loop = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        tasks = [t for t in (loop, *self._polls) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as err:
                self._logger.error("events tick failed", {"error": type(err).__name__})
            await asyncio.sleep(TICK_SECONDS)

    def tick(self) -> None:
        """Starts a poll of every machine that is due and not being polled."""
        now = self._now()
        for name, machine in self._machines.items():
            interval = self.interval_ms(machine)
            if interval == 0 or name in self._busy:
                continue
            if now - self._last_poll.get(name, 0) < interval:
                continue
            since = self._unsupported.get(name)
            if since is not None and now - since < UNSUPPORTED_RETRY_MS:
                continue
            self._last_poll[name] = now
            self._busy.add(name)
            task = asyncio.get_running_loop().create_task(self._poll_guarded(name))
            self._polls.add(task)
            task.add_done_callback(self._polls.discard)

    async def _poll_guarded(self, name: str) -> None:
        try:
            await self.poll(name)
        except Exception as err:
            self._logger.error("events poll failed", {"machine": name, "error": type(err).__name__})
        finally:
            self._busy.discard(name)

    async def poll(self, name: str) -> None:
        """Takes the machine's events: acknowledges the ones taken last time and delivers the new
        ones, up to MAX_ROUNDS calls."""
        transport = self._opts.transports[name]
        machine = self._machines[name]
        timeout = float(command_timeout_seconds(self._opts.remote, machine))
        for _ in range(MAX_ROUNDS):
            acks = self._acks.get(name, [])
            result = await transport.call("events", acks, timeout_seconds=timeout)
            if not result.ok:
                if result.error == "refused_by_policy" and result.detail in UNSUPPORTED:
                    if name not in self._unsupported:
                        self._logger.info(
                            "machine hands out no session events", {"machine": name, "reason": result.detail}
                        )
                    self._unsupported[name] = self._now()
                return
            self._unsupported.pop(name, None)
            data = result.data
            if not isinstance(data, EventsResponse):
                return
            taken = [event.id for event in data.events]
            changed = bool(acks) or bool(taken)
            if taken:
                self._hooked[name] = self._now()
            self._acks[name] = taken
            for event in data.events:
                key = f"{name}|{event.id}"
                if key in self._seen_set:
                    continue
                self._remember(key)
                self.deliver(machine, event)
            if data.full:
                self._opts.tracker.fail(
                    name,
                    "session",
                    "events_spool_full",
                    what="session events were lost",
                    why="the machine's switchboard-hook spool was full (200 events): the hub did not take them for a "
                    "long time, so the hooks dropped newer ones",
                    effect="some approvals, questions or finished turns were not reported",
                    instant=True,
                )
            if data.bad:
                self._logger.warn("machine spool had invalid event files", {"machine": name, "count": data.bad})
            if changed:
                self._save()
            if not taken:
                return

    # --- delivery ---------------------------------------------------------------------------

    def _within_rate(self, name: str) -> bool:
        now = self._now()
        recent = [t for t in self._taken.get(name, []) if now - t < HOUR_MS]
        if len(recent) >= self._opts.events.max_per_hour:
            self._taken[name] = recent
            return False
        recent.append(now)
        self._taken[name] = recent
        return True

    def deliver(self, machine: MachineConfig, event: SessionEvent) -> None:
        name = machine.name
        checked = check_event(event, machine, self._now())
        if checked is None:
            self._logger.warn("session event refused", {"machine": name})
            return
        if not self._within_rate(name):
            self._opts.tracker.fail(
                name,
                "session",
                "events_flood",
                what="too many session events",
                why=f"more than {self._opts.events.max_per_hour} session events in an hour (events.maxPerHour); "
                "the rest are counted here and not posted",
                effect="some session events were not reported",
                instant=True,
            )
            return
        view = checked.view
        self.delivered += 1
        self._logger.info("session event", {"machine": name, "kind": view.kind})
        if view.kind == "failed":
            self._opts.tracker.fail(
                name,
                "session",
                "session_failed",
                what="a Claude Code session stopped on an API error",
                why=log_detail(view),
                effect="that session stopped until someone continues it",
                at_ms=checked.time_ms,
                instant=True,
            )
        elif self._opts.tracker.enabled:
            self._opts.log.write(
                event_entry(
                    machine=name, code=view.kind, what=WHAT[view.kind], why=log_detail(view), at_ms=checked.time_ms
                )
            )
        if self._opts.discord is not None:
            self._opts.discord.add(Item(kind=view.kind, machine=name, event=view))
