"""Machine health (docs/SWITCHBOARD.md, "Incident log"): the hub polls each machine's dispatcher
`health` verb (0.9.0) every `incidents.health.intervalSeconds` and when an agent machine connects,
checks its own state directory, and opens incidents on the thresholds: a file system with less free
space than `minFreeGigabytes` or `minFreePercent`, any read-only file system, less available memory
than `minMemoryMegabytes`. On WSL the Windows drive (`/mnt/c`) counts too: WSL's own disk image
lives there, and inside WSL the root's free space does not show it filling. An incident closes when
the value is back past the limit with a margin (10 % more space, 1 point more percent, 20 % more
memory), so a value that hovers at the limit does not flap.

The same loop closes settled incidents (`IncidentTracker.tick`) and refreshes the hub's alive marker.
A dispatcher older than 0.9.0 answers `bad_request`: it is then polled with `version` instead (no
health incidents, but its link and dispatcher incidents still open and close).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo

from ..clock import SECOND_MS, now_ms
from ..log import Logger
from .config import HealthConfig, RemoteConfig, command_timeout_seconds
from .incidents import HUB, MARKER_SECONDS, HubMarker, IncidentTracker, note_unclean_stop
from .protocol import FilesystemInfo, HealthResponse
from .transport import Transport

GIB = 1024**3
MIB = 1024**2
TICK_SECONDS = 30
UNSUPPORTED_RETRY_MS = 6 * 3600 * SECOND_MS
"""How long a dispatcher without `health` is polled with `version` before `health` is tried again."""
LABELS = {
    "root": "the root file system",
    "home": "the home file system",
    "claudeHome": "the file system of the Claude Code home",
    "windows": "the Windows disk C:",
    "stateDir": "the file system of the hub's state directory",
}
CODES = {"root": "root", "home": "home", "claudeHome": "claude_home", "windows": "windows", "stateDir": "state_dir"}


def size_words(value: int) -> str:
    """Binary units as Windows and `df -h` show them, labelled GB and TB: "57 GB", "1.9 TB", "512 MB"."""
    if value >= 1024 * GIB:
        return f"{value / (1024 * GIB):.1f} TB"
    if value >= 10 * GIB:
        return f"{round(value / GIB)} GB"
    if value >= GIB:
        return f"{value / GIB:.1f} GB"
    return f"{round(value / MIB)} MB"


@dataclass(frozen=True)
class Finding:
    """One threshold check: `bad` opens (or repeats) its incident, `good` ends it, neither leaves it."""

    code: str
    bad: bool
    good: bool
    what: str = ""
    why: str = ""
    effect: str = ""


def _disk(label: str, fs: FilesystemInfo, config: HealthConfig, *, wsl: bool) -> Finding | None:
    total, free = fs.total_bytes, fs.free_bytes
    if total is None or free is None or total <= 0:
        return None
    percent = 100 * free / total
    low_bytes = config.min_free_gigabytes * GIB
    bad = free < low_bytes or percent < config.min_free_percent
    good = free >= low_bytes * 1.1 and (config.min_free_percent == 0 or percent >= config.min_free_percent + 1)
    name = LABELS[label]
    used = round(100 - percent)
    why = f"{name} has {size_words(free)} free of {size_words(total)} ({used} % used)"
    if label == "windows":
        why += (
            ": WSL's disk image usually lives there, and when that disk fills, WSL's root file system "
            "goes read-only and programs crash (inside WSL the root still shows free space)"
        )
    elif wsl and label == "root":
        why += ": on WSL also check the Windows disk, which this number does not show"
    return Finding(
        code=f"low_disk_{CODES[label]}",
        bad=bad,
        good=good,
        what=f"{name} is nearly full ({size_words(free)} free)",
        why=why,
        effect="programs there may fail to write files soon",
    )


def _read_only(label: str, fs: FilesystemInfo, *, wsl: bool) -> Finding:
    name = LABELS[label]
    why = f"{name} is mounted read-only"
    if wsl and label in ("root", "home", "claudeHome"):
        why += (
            "; on WSL this usually means the Windows disk filled up and ext4 remounted the root read-only "
            "(errors=remount-ro); free space on the Windows disk, then wsl --shutdown"
        )
    return Finding(
        code=f"read_only_{CODES[label]}",
        bad=fs.read_only,
        good=not fs.read_only,
        what=f"{name} is read-only",
        why=why,
        effect="Claude Code, tmux and git there cannot write; sessions may crash",
    )


def evaluate(health: HealthResponse, config: HealthConfig) -> list[Finding]:
    """The threshold checks of one `health` answer. A file system already reported under an earlier
    label (`sameAs`) or that could not be read is skipped."""
    findings: list[Finding] = []
    seen: set[str] = set()
    for fs in health.filesystems:
        label = fs.name
        if label not in LABELS or label == "stateDir" or label in seen:
            continue
        seen.add(label)
        if fs.error is not None or fs.same_as is not None:
            continue
        disk = _disk(label, fs, config, wsl=health.wsl)
        if disk is not None:
            findings.append(disk)
        findings.append(_read_only(label, fs, wsl=health.wsl))
    available, total = health.memory_available_bytes, health.memory_total_bytes
    if available is not None and config.min_memory_megabytes > 0:
        low = config.min_memory_megabytes * MIB
        of = f" of {size_words(total)}" if total else ""
        findings.append(
            Finding(
                code="low_memory",
                bad=available < low,
                good=available >= low * 1.2,
                what=f"memory is low ({size_words(available)} available)",
                why=f"only {size_words(available)}{of} memory is available; the kernel may kill programs",
                effect="Claude Code and builds there may be killed",
            )
        )
    return findings


def apply(tracker: IncidentTracker, machine: str, findings: Sequence[Finding]) -> None:
    for finding in findings:
        if finding.bad:
            tracker.fail(
                machine, "machine_health", finding.code, what=finding.what, why=finding.why, effect=finding.effect
            )
        elif finding.good:
            tracker.recovered(machine, codes=[finding.code], recovery="back within the limit")


def hub_findings(state_dir: str, config: HealthConfig) -> list[Finding]:
    """The hub's own state directory (where this log lives): free space and the read-only flag."""
    try:
        info = os.statvfs(state_dir)
    except OSError:
        return []
    fs = FilesystemInfo.model_validate(
        {
            "name": "stateDir",
            "totalBytes": info.f_blocks * info.f_frsize,
            "freeBytes": info.f_bavail * info.f_frsize,
            "readOnly": bool(info.f_flag & os.ST_RDONLY),
        }
    )
    findings = [_read_only("stateDir", fs, wsl=False)]
    disk = _disk("stateDir", fs, config, wsl=False)
    return [disk, *findings] if disk is not None else findings


@dataclass
class HealthPollerOptions:
    remote: RemoteConfig
    health: HealthConfig
    transports: Mapping[str, Transport]
    """The observed transports: a failed poll opens its transport or dispatcher incident."""
    tracker: IncidentTracker
    state_dir: str
    logger: Logger
    tz: tzinfo | None = None
    now: Callable[[], int] | None = None
    marker: bool = True
    """Keep `stateDir/hub-alive.json` (off in tests that do not start a hub)."""


class HealthPoller:
    """The incident loop: `start` at the hub's start, `stop` at its clean stop."""

    def __init__(self, options: HealthPollerOptions) -> None:
        self._opts = options
        self._now = options.now or now_ms
        self._tracker = options.tracker
        self._unsupported: dict[str, int] = {}
        self._loop: asyncio.Task[None] | None = None
        self._polls: set[asyncio.Task[None]] = set()
        self._marker = HubMarker(options.state_dir, now=self._now)
        self._last_poll = 0
        self._last_beat = 0

    async def start(self) -> None:
        if not self._tracker.enabled:
            return
        self._tracker.adopt()
        if self._opts.marker:
            seen = self._marker.previous()
            if seen is not None:
                note_unclean_stop(self._tracker, seen, self._opts.tz)
            self._marker.beat()
        self._last_beat = self._now()
        if self._loop is None:
            self._loop = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        tasks = [t for t in (loop, *self._polls) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._tracker.enabled:
            self._tracker.shutdown()
            if self._opts.marker:
                self._marker.remove()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:
                self._opts.logger.error("incident tick failed", {"error": type(err).__name__})
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        """Closes settled incidents, refreshes the marker, and polls every machine when it is time."""
        now = self._now()
        self._tracker.tick()
        if self._opts.marker and now - self._last_beat >= MARKER_SECONDS * SECOND_MS:
            self._marker.beat()
            self._last_beat = now
        interval = self._opts.health.interval_seconds
        if interval > 0 and (not self._last_poll or now - self._last_poll >= interval * SECOND_MS):
            self._last_poll = now
            await self.poll_all()

    async def poll_all(self) -> None:
        apply(self._tracker, HUB, hub_findings(self._opts.state_dir, self._opts.health))
        await asyncio.gather(*(self.poll(name) for name in self._opts.transports))

    def poll_soon(self, name: str) -> None:
        """A poll of one machine in the background (an agent machine just connected)."""
        if self._loop is None or self._opts.health.interval_seconds == 0 or name not in self._opts.transports:
            return
        task = asyncio.get_running_loop().create_task(self.poll(name))
        self._polls.add(task)
        task.add_done_callback(self._polls.discard)

    async def poll(self, name: str) -> None:
        transport = self._opts.transports[name]
        timeout = float(command_timeout_seconds(self._opts.remote, transport.machine))
        since = self._unsupported.get(name)
        if since is not None and self._now() - since < UNSUPPORTED_RETRY_MS:
            await transport.call("version", timeout_seconds=timeout)
            return
        result = await transport.call("health", timeout_seconds=timeout)
        if not result.ok and result.error == "refused_by_policy" and result.detail == "bad_request":
            self._unsupported[name] = self._now()
            self._opts.logger.info("machine has no health verb (dispatcher before 0.9.0)", {"machine": name})
            return
        self._unsupported.pop(name, None)
        if result.ok and isinstance(result.data, HealthResponse):
            apply(self._tracker, name, evaluate(result.data, self._opts.health))
