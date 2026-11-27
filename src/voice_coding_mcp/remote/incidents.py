"""The incident log (docs/SWITCHBOARD.md, "Incident log"): what went wrong, on which machine, when
and why, so the owner is not blind to failures that happened while nobody was listening.

`stateDir/incidents.jsonl` (0600) is append-only: an incident is written when it opens and again,
with the same id, when it closes; a reader keeps the last line of each id. Repeats of an open
incident only bump a counter in memory, written with the close, so a failure that repeats every
call does not grow the file. Past `incidents.maxKilobytes` the file is rotated (`.1` ... `.N`, the
incidents still open are carried into the new file); appends and rotation hold `incidents.lock`, so
the CLI (another process) can add and close incidents next to the running hub.

Automatic incidents (`source: auto`) come from `IncidentTracker`: a failure opens one per (machine,
component, code); a success marks it recovered, and it closes only after `incidents.settleSeconds`
without the failure, so a flapping link or a machine that fails every other call stays one
incident. At most MAX_NEW_PER_HOUR automatic incidents open per hour; beyond that a single
`incident_flood` incident counts the rest.

What is written: fixed codes and texts chosen by the hub, numbers, machine names from the hub's
config and, for incidents the owner records (`source: owner` or `cli`), the owner's own words,
cleaned, redacted and clipped. Never pane text, prompts, tokens, paths or a machine's error text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import unicodedata
import zoneinfo
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Literal

from ..clock import HOUR_MS, MINUTE_MS, SECOND_MS, now_ms, parse_iso, to_iso
from ..json_file import ensure_dir, file_identity, file_lock, write_json_atomic
from ..log import Logger
from ..notify import TextSender
from ..text import clip, plural
from .config import MACHINE_NAME_RE, IncidentsConfig, MachineConfig
from .redact import redact, strip_terminal
from .transport import REMOTE_FAILURES, Transport, TransportResult

INCIDENTS_FILE = "incidents.jsonl"
LOCK_FILE = "incidents.lock"
MARKER_FILE = "hub-alive.json"
HUB = "hub"
"""The machine name of incidents of the hub itself (never a configured machine: `local` is reserved
and `hub` is checked in `check_remote`)."""
Component = Literal[
    "agent_link",
    "dispatcher",
    "tmux",
    "transport",
    "relay",
    "summariser",
    "tool",
    "hub",
    "machine_health",
    "other",
    "session",
    "notify",
    "update",
]
COMPONENTS: tuple[str, ...] = (
    "agent_link",
    "dispatcher",
    "tmux",
    "transport",
    "relay",
    "summariser",
    "tool",
    "hub",
    "machine_health",
    "other",
    "session",
    "notify",
    "update",
)
Source = Literal["auto", "owner", "cli", "session"]
SOURCES: tuple[str, ...] = ("auto", "owner", "cli", "session")
"""`session`: an event a machine's session hooks reported (`remote/events.py`), its texts the
session's own words, cleaned and redacted."""
ENTRIES = ("incident", "event")
"""An `event` line records something that happened (a session finished, asked, opened a pull
request) and is not an incident: the incidents tool and the CLI's default list leave it out."""
CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
ID_RE = re.compile(r"[0-9a-f]{8}")
MAX_WHAT = 200
MAX_WHY = 500
MAX_EFFECT = 200
MAX_RECOVERY = 300
MAX_LINE_BYTES = 8 * 1024
MAX_OPEN = 256
MAX_NEW_PER_HOUR = 30
FILE_VERSION = 1
MARKER_SECONDS = 60
"""How often the running hub refreshes its alive marker (the start of an unclean-stop incident)."""


def clean_text(value: str | None, limit: int) -> str:
    """Text for the log: one line, without control and format characters, secrets redacted
    (`redact.redact`), at most `limit` characters."""
    if not value:
        return ""
    text = " ".join(strip_terminal(value).split())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return clip(redact(text), limit)


def zone_of(config: IncidentsConfig) -> tzinfo | None:
    """The configured zone (`incidents.timeZone`), or None for the hub's local zone."""
    if config.time_zone is None:
        return None
    return zoneinfo.ZoneInfo(config.time_zone)


def local_time(ms: int, tz: tzinfo | None) -> datetime:
    moment = datetime.fromtimestamp(ms / 1000, tz) if tz is not None else datetime.fromtimestamp(ms / 1000)
    return moment if moment.tzinfo is not None else moment.astimezone()


def zone_name(tz: tzinfo | None, ms: int) -> str:
    """The IANA name when configured, else the hub's local abbreviation (`EET`, `UTC`)."""
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    return local_time(ms, tz).tzname() or "local"


def duration_words(ms: int) -> str:
    """ "10 seconds", "5 minutes", "1 hour 6 minutes", "2 days 3 hours"."""
    seconds = max(0, round(ms / SECOND_MS))
    if seconds < 60:
        return plural(seconds, "second")
    minutes = round(seconds / 60)
    if minutes < 60:
        return plural(minutes, "minute")
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return plural(hours, "hour") + (f" {plural(rest, 'minute')}" if rest else "")
    days, hours = divmod(hours, 24)
    return plural(days, "day") + (f" {plural(hours, 'hour')}" if hours else "")


@dataclass
class Incident:
    id: str
    machine: str
    component: str
    code: str
    what: str
    why: str
    source: str
    start_ms: int
    end_ms: int | None = None
    effect: str = ""
    recovery: str = ""
    count: int = 1
    entry: str = "incident"
    """`incident` or `event` (ENTRIES); written only for events, so older lines read as incidents."""
    # In memory only:
    last_ms: int = 0
    """When the failure was last seen."""
    recovered_ms: int | None = None
    """When it stopped (a success); it closes at this time after the settle time."""
    instant: bool = False
    """An event without a success that ends it (a crash, a refused relay connection): closes after
    the settle time without a repeat."""

    @property
    def open(self) -> bool:
        return self.end_ms is None

    def to_json(self, tz: tzinfo | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "v": FILE_VERSION,
            "id": self.id,
            "start": to_iso(self.start_ms),
            "end": to_iso(self.end_ms) if self.end_ms is not None else None,
            "zone": zone_name(tz, self.start_ms),
            "startLocal": local_time(self.start_ms, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "endLocal": local_time(self.end_ms, tz).strftime("%Y-%m-%d %H:%M:%S") if self.end_ms is not None else None,
            "durationSeconds": round((self.end_ms - self.start_ms) / SECOND_MS) if self.end_ms is not None else None,
            "machine": self.machine,
            "component": self.component,
            "code": self.code,
            "what": self.what,
            "why": self.why,
            "effect": self.effect,
            "recovery": self.recovery,
            "count": self.count,
            "source": self.source,
        }
        if self.entry != "incident":
            out["entry"] = self.entry
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Incident | None:
        """A line of the log, checked field by field (the file is the hub user's, but a line may be
        cut or written by an older version); None when it is not an incident."""
        ident, machine, component, code, source = (
            data.get("id"),
            data.get("machine"),
            data.get("component"),
            data.get("code"),
            data.get("source"),
        )
        if not (isinstance(ident, str) and ID_RE.fullmatch(ident)):
            return None
        if not (isinstance(machine, str) and (machine == HUB or MACHINE_NAME_RE.fullmatch(machine))):
            return None
        if component not in COMPONENTS or source not in SOURCES:
            return None
        entry = data.get("entry", "incident")
        if entry not in ENTRIES:
            return None
        if not (isinstance(code, str) and CODE_RE.fullmatch(code)):
            return None
        start = parse_iso(data.get("start")) if isinstance(data.get("start"), str) else None
        if start is None:
            return None
        end_value = data.get("end")
        end = parse_iso(end_value) if isinstance(end_value, str) else None
        if end_value is not None and end is None:
            return None
        count = data.get("count")

        def text(key: str, limit: int) -> str:
            value = data.get(key)
            return clean_text(value, limit) if isinstance(value, str) else ""

        return cls(
            id=ident,
            machine=machine,
            component=str(component),
            code=code,
            what=text("what", MAX_WHAT),
            why=text("why", MAX_WHY),
            effect=text("effect", MAX_EFFECT),
            recovery=text("recovery", MAX_RECOVERY),
            source=str(source),
            start_ms=start,
            end_ms=end,
            count=count if isinstance(count, int) and not isinstance(count, bool) and count >= 1 else 1,
            entry=str(entry),
            last_ms=end if end is not None else start,
        )

    @property
    def is_event(self) -> bool:
        return self.entry == "event"


def new_id() -> str:
    return secrets.token_hex(4)


class IncidentLog:
    """`stateDir/incidents.jsonl`: appends under a file lock, rotation by size, folded reads."""

    def __init__(
        self,
        state_dir: str,
        *,
        max_bytes: int = 1024 * 1024,
        keep_files: int = 3,
        tz: tzinfo | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, INCIDENTS_FILE)
        self.lock_path = os.path.join(state_dir, LOCK_FILE)
        self.max_bytes = max_bytes
        self.keep_files = keep_files
        self.tz = tz
        self.logger = logger
        self._cache: tuple[tuple[object, ...], list[Incident], int] | None = None

    def encode(self, incident: Incident) -> bytes:
        line = json.dumps(incident.to_json(self.tz), ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        if len(data) > MAX_LINE_BYTES:
            # Not reachable with the field limits; kept so a line is never cut by a reader's cap.
            incident.why = clip(incident.why, 100)
            incident.effect = clip(incident.effect, 100)
            incident.recovery = clip(incident.recovery, 100)
            data = (json.dumps(incident.to_json(self.tz), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        return data

    def write(self, incident: Incident) -> bool:
        """Appends the incident's current state (fsynced). False, logged, when the disk refused it:
        the hub's own disk may be the one that is full."""
        data = self.encode(incident)
        try:
            ensure_dir(self.state_dir)
            with file_lock(self.lock_path, ensure_parent=False):
                self._rotate_if_needed(len(data), incident.id)
                self._append(self.path, data)
        except OSError as err:
            if self.logger is not None:
                self.logger.warn(
                    "incident not written",
                    {"machine": incident.machine, "code": incident.code, "error": type(err).__name__},
                )
            return False
        return True

    @staticmethod
    def _append(path: str, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rotate_if_needed(self, adding: int, writing: str) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size + adding <= self.max_bytes:
            return
        still_open = [i for i in _fold([self.path])[0] if i.open and i.id != writing]
        for index in range(self.keep_files - 1, 0, -1):
            older = f"{self.path}.{index}"
            if os.path.exists(older):
                os.replace(older, f"{self.path}.{index + 1}")
        os.replace(self.path, f"{self.path}.1")
        if still_open:
            self._append(self.path, b"".join(self.encode(i) for i in still_open))

    def files(self) -> list[str]:
        """Oldest first: the rotated files, then the current one."""
        return [f"{self.path}.{index}" for index in range(self.keep_files, 0, -1)] + [self.path]

    def read(self) -> tuple[list[Incident], int]:
        """Every incident (the last line of each id), ordered by start time, and how many lines could
        not be read. Cached until a file changes."""
        files = self.files()
        key = tuple(file_identity(path) for path in files)
        if self._cache is not None and self._cache[0] == key:
            return list(self._cache[1]), self._cache[2]
        incidents, bad = _fold(files)
        self._cache = (key, incidents, bad)
        return list(incidents), bad


def _fold(paths: Sequence[str]) -> tuple[list[Incident], int]:
    found: dict[str, Incident] = {}
    bad = 0
    for path in paths:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        for chunk in raw.split(b"\n"):
            if not chunk.strip():
                continue
            value: object = None
            if len(chunk) <= MAX_LINE_BYTES:
                with contextlib.suppress(UnicodeDecodeError, ValueError, RecursionError):
                    value = json.loads(chunk.decode("utf-8"))
            incident = Incident.from_json(value) if isinstance(value, dict) else None
            if incident is None:
                bad += 1
                continue
            found.pop(incident.id, None)
            found[incident.id] = incident
    return sorted(found.values(), key=lambda i: i.start_ms), bad


def owner_incident(
    *,
    machine: str,
    what: str,
    why: str,
    source: Source,
    start_ms: int,
    end_ms: int | None,
    effect: str = "",
    recovery: str = "",
    component: str = "other",
) -> Incident:
    """An incident the owner reports (voice `incident_note`, or the CLI)."""
    return Incident(
        id=new_id(),
        machine=machine,
        component=component,
        code="owner_report",
        what=clean_text(what, MAX_WHAT),
        why=clean_text(why, MAX_WHY),
        effect=clean_text(effect, MAX_EFFECT),
        recovery=clean_text(recovery, MAX_RECOVERY),
        source=source,
        start_ms=start_ms,
        end_ms=end_ms,
        last_ms=end_ms if end_ms is not None else start_ms,
    )


def event_entry(*, machine: str, code: str, what: str, why: str, at_ms: int) -> Incident:
    """A session event for the log (`entry: event`, closed at once): `what` is the hub's own words,
    `why` may quote the session's words (cleaned and redacted here)."""
    return Incident(
        id=new_id(),
        machine=machine,
        component="session",
        code=code,
        what=clean_text(what, MAX_WHAT),
        why=clean_text(why, MAX_WHY),
        source="session",
        start_ms=at_ms,
        end_ms=at_ms,
        last_ms=at_ms,
        entry="event",
    )


def update_event(*, machine: str, code: str, what: str, why: str, at_ms: int) -> Incident:
    """One self-update attempt for the log (`entry: event`, component `update`, closed at once):
    `update_installed`, `update_refused` (the machine or the hub said no; `why` names the fixed
    refusal code) or `update_unknown` (the machine stopped answering). Only the hub's own words,
    versions, the signer's fingerprint and fixed codes."""
    return Incident(
        id=new_id(),
        machine=machine,
        component="update",
        code=code,
        what=clean_text(what, MAX_WHAT),
        why=clean_text(why, MAX_WHY),
        source="auto",
        start_ms=at_ms,
        end_ms=at_ms,
        last_ms=at_ms,
        entry="event",
    )


Key = tuple[str, str, str]


@dataclass
class TrackerOptions:
    log: IncidentLog
    settle_seconds: int = 120
    enabled: bool = True
    logger: Logger | None = None
    now: Callable[[], int] | None = None
    notify: TextSender | None = None
    """Pushes "Incident on wsl: ..." and "Resolved on wsl: ..." (`incidents.notify`)."""


class IncidentTracker:
    """The automatic incidents of the running hub. Never raises."""

    def __init__(self, options: TrackerOptions) -> None:
        self.log = options.log
        self._settle_ms = options.settle_seconds * SECOND_MS
        self.enabled = options.enabled
        self.logger = options.logger
        self._now = options.now or now_ms
        self._notify = options.notify
        self._open: dict[Key, Incident] = {}
        self._created: list[int] = []
        self._background: set[asyncio.Task[None]] = set()

    @property
    def open_incidents(self) -> list[Incident]:
        return list(self._open.values())

    def adopt(self) -> int:
        """Takes over the automatic incidents an earlier run left open (it stopped while they were
        open), so a success now closes them. Their repeats count from here."""
        if not self.enabled:
            return 0
        incidents, _ = self.log.read()
        now = self._now()
        taken = 0
        for incident in incidents:
            key = (incident.machine, incident.component, incident.code)
            if incident.open and incident.source == "auto" and key not in self._open:
                incident.last_ms = now
                # Unknown whether it was an event or a state: an event closes after the settle time.
                incident.instant = incident.component in ("relay", "tool", "hub")
                self._open[key] = incident
                taken += 1
        return taken

    def fail(
        self,
        machine: str,
        component: Component,
        code: str,
        *,
        what: str,
        why: str,
        effect: str = "",
        at_ms: int | None = None,
        instant: bool = False,
    ) -> Incident | None:
        """Opens the incident (machine, component, code), or counts a repeat of the open one. The
        texts are the hub's own (never a machine's output)."""
        if not self.enabled:
            return None
        now = self._now()
        key = (machine, component, code)
        current = self._open.get(key)
        if current is not None:
            current.count += 1
            current.last_ms = now
            current.recovered_ms = None
            current.recovery = ""
            return current
        self._created = [t for t in self._created if now - t < HOUR_MS]
        if len(self._created) >= MAX_NEW_PER_HOUR or len(self._open) >= MAX_OPEN:
            self._flood(now)
            return None
        self._created.append(now)
        return self._start(key, what=what, why=why, effect=effect, at_ms=at_ms, instant=instant)

    def _start(self, key: Key, *, what: str, why: str, effect: str, at_ms: int | None, instant: bool) -> Incident:
        now = self._now()
        incident = Incident(
            id=new_id(),
            machine=key[0],
            component=key[1],
            code=key[2],
            what=clean_text(what, MAX_WHAT),
            why=clean_text(why, MAX_WHY),
            effect=clean_text(effect, MAX_EFFECT),
            source="auto",
            start_ms=min(at_ms, now) if at_ms is not None else now,
            last_ms=now,
            instant=instant,
        )
        self._open[key] = incident
        self.log.write(incident)
        if self.logger is not None:
            self.logger.warn(
                "incident opened",
                {
                    "id": incident.id,
                    "machine": incident.machine,
                    "component": incident.component,
                    "code": incident.code,
                },
            )
        self._push(f"Incident on {incident.machine}: {incident.what}.")
        return incident

    def _flood(self, now: int) -> None:
        key: Key = (HUB, "hub", "incident_flood")
        flood = self._open.get(key)
        if flood is not None:
            flood.count += 1
            flood.last_ms = now
            return
        self._start(
            key,
            what="too many incidents",
            why=f"more than {MAX_NEW_PER_HOUR} automatic incidents opened within an hour; further ones are only "
            "counted here until it calms down (the hub's journal has every failed call)",
            effect="some failures are not in the incident log one by one",
            at_ms=now,
            instant=True,
        )

    def recovered(
        self,
        machine: str,
        *,
        components: Iterable[str] = (),
        codes: Iterable[str] = (),
        recovery: str,
        at_ms: int | None = None,
    ) -> None:
        """A success that ends the open incidents of `machine` in `components` or with one of `codes`:
        they close after the settle time unless the failure comes back."""
        if not self.enabled:
            return
        wanted_components, wanted_codes = set(components), set(codes)
        now = self._now()
        for incident in list(self._open.values()):
            if incident.machine != machine or incident.instant or incident.recovered_ms is not None:
                continue
            if incident.component not in wanted_components and incident.code not in wanted_codes:
                continue
            incident.recovered_ms = min(at_ms, now) if at_ms is not None else now
            incident.recovery = clean_text(recovery, MAX_RECOVERY)
        if self._settle_ms == 0:
            self.tick()

    def tick(self) -> None:
        """Closes what has settled: recovered incidents after the settle time, and events that did not
        happen again for that long."""
        now = self._now()
        for key, incident in list(self._open.items()):
            if incident.recovered_ms is not None and now - incident.recovered_ms >= self._settle_ms:
                self._close(key, incident.recovered_ms)
            elif incident.instant and now - incident.last_ms >= self._settle_ms:
                if not incident.recovery:
                    minutes = max(1, round(self._settle_ms / MINUTE_MS))
                    incident.recovery = f"it did not happen again within {plural(minutes, 'minute')}"
                self._close(key, incident.last_ms)

    def close_now(self, machine: str, component: Component, code: str, *, recovery: str) -> None:
        """Closes one open incident at once (an event whose end is known, such as the hub starting
        again after an unclean stop)."""
        key = (machine, component, code)
        incident = self._open.get(key)
        if incident is not None:
            incident.recovery = clean_text(recovery, MAX_RECOVERY)
            self._close(key, self._now())

    def _close(self, key: Key, end_ms: int) -> None:
        incident = self._open.pop(key)
        incident.end_ms = max(end_ms, incident.start_ms)
        self.log.write(incident)
        if self.logger is not None:
            self.logger.info(
                "incident closed",
                {"id": incident.id, "machine": incident.machine, "code": incident.code, "count": incident.count},
            )
        self._push(f"Resolved on {incident.machine}: {incident.what}.")

    def shutdown(self) -> None:
        """At a clean stop: what already recovered closes at its recovery time; the rest stays open
        in the log and is adopted by the next start."""
        for key, incident in list(self._open.items()):
            if incident.recovered_ms is not None:
                self._close(key, incident.recovered_ms)

    def recovering(self, incident_id: str) -> bool:
        return any(i.id == incident_id and i.recovered_ms is not None for i in self._open.values())

    def _push(self, text: str) -> None:
        if self._notify is None:
            return
        notify = self._notify

        async def send() -> None:
            await notify(text)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


# --- dispatcher calls -----------------------------------------------------------------------------

TMUX_VERBS = ("list", "capture", "send", "keys", "resize")

_EFFECT_OF_VERB = {
    "list": "its sessions could not be listed",
    "capture": "a screen could not be read",
    "send": "a message could not be delivered",
    "keys": "key presses could not be delivered",
    "resize": "a window could not be resized",
    "usage": "its usage could not be read",
    "health": "its health could not be checked",
    "version": "it could not be checked",
    "update": "an update could not be installed",
}

_TRANSPORT_WHY = {
    "timeout": ("no answer in time", "the machine did not answer within the command timeout"),
    "host_key": (
        "its host key is not trusted",
        "ssh refused the machine's host key (changed, or not in the known hosts file)",
    ),
    "auth": ("its key was refused", "ssh was refused: the hub's key is not accepted there"),
    "refused": (
        "it could not be reached",
        "the connection was refused or the host could not be found (machine off, network down or sshd stopped)",
    ),
    "other": ("the connection failed", "the call failed without a clearer reason (see the hub's journal)"),
    "not_connected": ("it is not connected", "the machine has no connection to the hub"),
    "disconnected": ("the connection was lost during a call", "the connection closed while a request was out"),
}


def observe_call(tracker: IncidentTracker, machine: MachineConfig, verb: str, result: TransportResult) -> None:
    """Opens or ends incidents from one dispatcher call. Only the error code and the dispatcher's own
    code (a fixed set) are used, never text from the machine."""
    name = machine.name
    if result.ok:
        components = ["transport", "dispatcher", *(["tmux"] if verb in TMUX_VERBS else [])]
        tracker.recovered(name, components=components, recovery=f"a {verb} call worked again")
        return
    error, detail = result.error or "other", result.detail
    effect = _EFFECT_OF_VERB.get(verb, "a request failed")
    if error in ("not_connected", "disconnected") and machine.transport == "agent":
        # The agent link incident (`AgentHub`) is the record of that.
        return
    if error == "remote_failed":
        code = detail if detail in REMOTE_FAILURES else "remote_failed"
        tracker.fail(
            name,
            "tmux",
            code or "remote_failed",
            what="tmux failed",
            why="the dispatcher answered, but tmux failed there: the tmux server may have stopped, or the "
            "machine's disk may be full or read-only",
            effect=effect,
        )
        return
    if error == "refused_by_policy":
        if detail in ("no_policy", "bad_policy"):
            tracker.fail(
                name,
                "dispatcher",
                "bad_policy",
                what="the dispatcher has no valid policy",
                why="switchboard-remote refuses everything: its command line or policy file is broken",
                effect=effect,
            )
        elif detail == "busy":
            tracker.fail(
                name,
                "dispatcher",
                "busy",
                what="the dispatcher was busy",
                why="every worker of switchboard-remote was still running earlier requests (a slow tmux or disk)",
                effect=effect,
            )
        # Any other refusal is the dispatcher doing its job (gone, changed, not_allowed, ...).
        return
    if error == "dispatcher_missing":
        tracker.fail(
            name,
            "dispatcher",
            "dispatcher_missing",
            what="switchboard-remote is missing",
            why="the dispatcher could not be started there (not installed, or its path or Python is wrong)",
            effect=effect,
        )
        return
    if error == "bad_response":
        tracker.fail(
            name,
            "dispatcher",
            "bad_response",
            what="the dispatcher answered something unexpected",
            why="the answer was not valid (too large, cut, or another program printed to the connection)",
            effect=effect,
        )
        return
    if error == "other" and detail == "crashed":
        tracker.fail(
            name,
            "dispatcher",
            "crashed",
            what="the dispatcher crashed",
            why="switchboard-remote raised an error for a request (its traceback is on the machine's stderr)",
            effect=effect,
        )
        return
    what, why = _TRANSPORT_WHY.get(error, _TRANSPORT_WHY["other"])
    tracker.fail(name, "transport", error if CODE_RE.fullmatch(error) else "other", what=what, why=why, effect=effect)


class ObservedTransport:
    """A transport whose every call result goes through `observe_call`."""

    def __init__(self, inner: Transport, tracker: IncidentTracker) -> None:
        self.machine = inner.machine
        self.inner = inner
        self._tracker = tracker

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        result = await self.inner.call(verb, args, timeout_seconds=timeout_seconds, stdin=stdin)
        try:
            observe_call(self._tracker, self.machine, verb, result)
        except Exception as err:
            # The incident log must never break a tool.
            if self._tracker.logger is not None:
                self._tracker.logger.error("incident observation failed", {"error": type(err).__name__})
        return result


# --- agent links -------------------------------------------------------------------------------

LINK_REASONS = {
    "eof": "the connection closed: the machine, its connector or the network went away",
    "heartbeat": "the machine stopped answering pings",
    "write_stalled": "the machine stopped reading its connection",
    "write_failed": "writing to the connection failed",
    "protocol": "the machine sent something that is not the protocol",
    "frame_too_large": "the machine sent a frame over the size limit",
}


def link_down(tracker: IncidentTracker, machine: str, reason: str | None, at_ms: int) -> None:
    """An agent machine's connection closed (not replaced by a newer one, not the hub stopping)."""
    why = LINK_REASONS.get(reason or "eof", LINK_REASONS["eof"])
    tracker.fail(
        machine,
        "agent_link",
        "link_down",
        what="its connection to the hub was lost",
        why=why,
        effect="it could not be listed, read or sent to until it connected again",
        at_ms=at_ms,
    )


def link_up(tracker: IncidentTracker, machine: str, at_ms: int) -> None:
    for incident in tracker.open_incidents:
        if incident.machine == machine and incident.code == "link_down" and incident.recovered_ms is None:
            gap = duration_words(at_ms - incident.start_ms)
            tracker.recovered(machine, codes=["link_down"], recovery=f"it connected again after {gap}", at_ms=at_ms)


RELAY_PROBLEMS = {
    "socket_not_served": (
        "the relay socket is not served",
        "the hub could not serve remote.agents.socketPath (see the startup warning: its directory, owner, "
        "group or mode, or the relay user)",
        "no agent machine can connect",
    ),
    "relay_wrong_user": (
        "a connection to the relay socket came from another user",
        "only the relay account may connect; another local user tried",
        "that connection was refused",
    ),
    "relay_too_many": (
        "too many relay connections",
        "more connections than machines were open at once (a connector loop, or a stolen key)",
        "new connections were refused",
    ),
    "relay_protocol": (
        "a relay connection broke the protocol",
        "a connection sent no valid preamble or hello (an old or broken relay, connector or dispatcher)",
        "that connection was refused",
    ),
    "relay_unknown_machine": (
        "the relay named a machine the hub does not know",
        "a key in the relay account's authorized_keys names a machine that is not in remote.machines",
        "that machine cannot connect",
    ),
}


def relay_problem(tracker: IncidentTracker, code: str, machine: str | None) -> None:
    what, why, effect = RELAY_PROBLEMS.get(code, RELAY_PROBLEMS["relay_protocol"])
    try:
        tracker.fail(machine or HUB, "relay", code, what=what, why=why, effect=effect, instant=True)
    except Exception as err:
        if tracker.logger is not None:
            tracker.logger.error("incident observation failed", {"error": type(err).__name__})


# --- the hub's own run ------------------------------------------------------------------------------


@dataclass
class HubMarker:
    """`stateDir/hub-alive.json` while the hub runs: a marker left behind means it did not stop
    cleanly, and its `seenMs` is about when it stopped."""

    state_dir: str
    now: Callable[[], int] = now_ms
    started_ms: int = field(default=0)

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, MARKER_FILE)

    def previous(self) -> int | None:
        """`seenMs` of a marker an earlier run left behind, else None."""
        try:
            with open(self.path, "rb") as handle:
                data = json.loads(handle.read(4096).decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError):
            return self.now()
        seen = data.get("seenMs") if isinstance(data, dict) else None
        return seen if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0 else self.now()

    def beat(self) -> None:
        now = self.now()
        if not self.started_ms:
            self.started_ms = now
        with contextlib.suppress(OSError):
            ensure_dir(self.state_dir)
            write_json_atomic(
                self.path, {"version": 1, "pid": os.getpid(), "startedMs": self.started_ms, "seenMs": now}
            )

    def remove(self) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self.path)


def note_unclean_stop(tracker: IncidentTracker, seen_ms: int, tz: tzinfo | None) -> None:
    seen = local_time(seen_ms, tz).strftime("%H:%M")
    tracker.fail(
        HUB,
        "hub",
        "unclean_stop",
        what="the hub stopped without a clean shutdown",
        why=f"it was last seen running at {seen} ({zone_name(tz, seen_ms)}); a crash, a kill, running out of "
        "memory or the machine losing power stops it like this",
        effect="the voice connector did not answer until the hub started again",
        at_ms=seen_ms,
        instant=True,
    )
    tracker.close_now(HUB, "hub", "unclean_stop", recovery="the hub started again")


# --- spoken and printed -------------------------------------------------------------------------

SPOKEN_MAX = 6


def _clock(ms: int, now: int, tz: tzinfo | None) -> str:
    moment, today = local_time(ms, tz), local_time(now, tz)
    if moment.date() == today.date():
        return moment.strftime("%H:%M")
    if (today.date() - moment.date()).days == 1:
        return "yesterday " + moment.strftime("%H:%M")
    return f"{moment.strftime('%B')} {moment.day} {moment.strftime('%H:%M')}"


def _subject(incident: Incident) -> str:
    return "the hub" if incident.machine == HUB else incident.machine


def spoken_incident(incident: Incident, now: int, tz: tzinfo | None, *, recovering: bool = False) -> str:
    """ "wsl: tmux failed, from 12:57 to 14:03 (1 hour 6 minutes)." Owner notes are quoted."""
    if incident.source == "auto":
        head = f"{_subject(incident)}: {incident.what}"
    else:
        head = f'{_subject(incident)}: you noted "{incident.what}"'
    start = _clock(incident.start_ms, now, tz)
    if incident.end_ms is None:
        when = "it has just recovered" if recovering else "still open"
        return f"{head}, since {start}, {when}."
    if incident.end_ms - incident.start_ms < MINUTE_MS:
        return f"{head}, at {start}."
    end = _clock(incident.end_ms, now, tz)
    return f"{head}, from {start} to {end} ({duration_words(incident.end_ms - incident.start_ms)})."


RELATIVE_PERIODS = ("Today", "Yesterday", "This week", "Last week", "This month", "Last month")


def period_words(label: str) -> str:
    """A `usage.compute.Period` label inside a sentence: "today", "this week", "on November 3, 2026",
    "in November 2026", "from November 1, 2026 to ..."; "open now" stays."""
    if label in RELATIVE_PERIODS:
        return label.lower()
    if label.startswith("From "):
        return "f" + label[1:]
    if label == "open now":
        return label
    return f"on {label}" if "," in label else f"in {label}"


def format_spoken(
    incidents: Sequence[Incident],
    *,
    label: str,
    now: int,
    tz: tzinfo | None,
    recovering: Callable[[str], bool] = lambda _id: False,
    with_why: bool = False,
) -> str:
    """The answer of the `incidents` tool: a count for the period, the newest first, open ones first."""
    zone = zone_name(tz, now)
    label = period_words(label)
    if not incidents:
        return f"No incidents {label}."
    ordered = sorted(incidents, key=lambda i: (not i.open, -i.start_ms))
    still = sum(1 for i in incidents if i.open and not recovering(i.id))
    head = f"{plural(len(incidents), 'incident')} {label}"
    head += f", {still} still open" if still and still < len(incidents) else ""
    lines = [f"{head} (times in {zone})."]
    for incident in ordered[:SPOKEN_MAX]:
        line = spoken_incident(incident, now, tz, recovering=recovering(incident.id))
        if with_why and incident.why:
            reason = incident.why if incident.source == "auto" else f'"{incident.why}"'
            line = line[:-1] + f". Why: {reason}."
        lines.append(line)
    if len(ordered) > SPOKEN_MAX:
        lines.append(f"And {len(ordered) - SPOKEN_MAX} more.")
    return "\n".join(lines)


def open_clause(incidents: Sequence[Incident]) -> str:
    """One short clause for `list_remote_sessions` and `remote_status` about a machine's open
    incidents, or empty."""
    if not incidents:
        return ""
    first = max(incidents, key=lambda i: i.start_ms)
    what = first.what if first.source == "auto" else f'you noted "{first.what}"'
    more = f" (and {plural(len(incidents) - 1, 'more')})" if len(incidents) > 1 else ""
    return f" Open incident: {clip(what, 120)}{more}."


TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("startLocal", "start"),
    ("endLocal", "end"),
    ("machine", "machine"),
    ("component", "component"),
    ("code", "code"),
    ("count", "count"),
    ("source", "source"),
    ("what", "what"),
)


def _cell(value: object, limit: int = 60) -> str:
    if value is None or value == "":
        return "-"
    text = str(value)
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:limit]


def format_table(incidents: Sequence[Incident], tz: tzinfo | None, *, details: bool = False) -> str:
    """A plain text table, oldest first; with `details` also why, effect and recovery under each row."""
    rows = [incident.to_json(tz) for incident in incidents]
    for row in rows:
        if row["endLocal"] is None:
            row["endLocal"] = "open"
    table = [[title for _, title in TABLE_COLUMNS]]
    table += [[_cell(row.get(key)) for key, _ in TABLE_COLUMNS] for row in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(TABLE_COLUMNS))]
    out: list[str] = []
    for index, line in enumerate(table):
        out.append("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())
        if details and index > 0:
            row = rows[index - 1]
            for key in ("why", "effect", "recovery"):
                if row.get(key):
                    out.append(f"    {key}: {_cell(row[key], 600)}")
    zone = zone_name(tz, incidents[-1].start_ms) if incidents else zone_name(tz, now_ms())
    return "\n".join(out) + f"\n(times in {zone})\n"
