"""The incident log: file format, bounds, rotation and privacy; the automatic triggers (dispatcher
calls, agent links, relay problems, the summariser, tool exceptions, unclean stops) with their
debounce; machine health thresholds and the poller; the fixed error mapping; the tools and the CLI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.tools import Tool

from tests.helpers import FakeClock
from tests.remote.helpers import RemoteWorld
from tests.remote.test_tools import build, populate
from tests.test_cli import run_cli, write_config
from voice_coding_mcp.clock import MINUTE_MS, SECOND_MS, parse_iso, to_iso
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.mcp import _guarded
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.config import HealthConfig, IncidentsConfig, MachineConfig, RemoteConfig
from voice_coding_mcp.remote.health import HealthPoller, HealthPollerOptions, evaluate, size_words
from voice_coding_mcp.remote.incidents import (
    HUB,
    MAX_NEW_PER_HOUR,
    MAX_WHY,
    Incident,
    IncidentLog,
    IncidentTracker,
    ObservedTransport,
    TrackerOptions,
    clean_text,
    format_spoken,
    link_down,
    link_up,
    note_unclean_stop,
    observe_call,
    open_clause,
    owner_incident,
    period_words,
    relay_problem,
)
from voice_coding_mcp.remote.inventory import MachineView
from voice_coding_mcp.remote.protocol import HealthResponse, ListResponse, VersionResponse, parse_response
from voice_coding_mcp.remote.speech import machine_line, unreachable_sentence
from voice_coding_mcp.remote.summarize import failure_code
from voice_coding_mcp.remote.transport import ProcessResult, TransportResult, interpret
from voice_coding_mcp.tasks.runner import RunResult

T0 = 1_795_000_000_000
GIB = 1024**3
SCREEN_SECRET = "export TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def lines(log: IncidentLog) -> list[dict[str, Any]]:
    with open(log.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@dataclass
class World:
    log: IncidentLog
    tracker: IncidentTracker
    clock: FakeClock
    pushes: list[str] = field(default_factory=list)


@pytest.fixture
def world(tmp_path: Path) -> World:
    clock = FakeClock(T0)
    log = IncidentLog(str(tmp_path / "state"))
    pushes: list[str] = []

    async def push(text: str) -> None:
        pushes.append(text)

    tracker = IncidentTracker(TrackerOptions(log=log, settle_seconds=120, now=clock, notify=push))
    return World(log=log, tracker=tracker, clock=clock, pushes=pushes)


def agent(name: str = "wsl") -> MachineConfig:
    return MachineConfig(name=name, transport="agent", sessions=["dev"])


def failed(error: str, detail: str | None = None) -> TransportResult:
    return TransportResult(ok=False, error=error, detail=detail)  # type: ignore[arg-type]


OK = TransportResult(ok=True, data=ListResponse(panes=[]))


# --- the file -------------------------------------------------------------------------------------


def test_a_line_per_state_with_utc_and_local_times(world: World) -> None:
    world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="because", effect="no list")
    [first] = lines(world.log)
    assert first["v"] == 1
    assert first["start"] == to_iso(T0)
    assert first["end"] is None
    assert first["durationSeconds"] is None
    assert first["zone"]
    assert first["startLocal"].startswith("2026-11-")
    assert (first["machine"], first["component"], first["code"], first["source"]) == (
        "wsl",
        "tmux",
        "tmux_failed",
        "auto",
    )
    assert stat.S_IMODE(os.stat(world.log.path).st_mode) == 0o600
    world.clock.t += 10 * SECOND_MS
    world.tracker.recovered("wsl", components=["tmux"], recovery="a list call worked again")
    world.clock.t += 120 * SECOND_MS
    world.tracker.tick()
    first_line, second = lines(world.log)
    assert second["id"] == first_line["id"]
    assert second["durationSeconds"] == 10
    assert second["end"] == to_iso(T0 + 10 * SECOND_MS)
    [incident], bad = world.log.read()
    assert bad == 0
    assert not incident.open
    assert incident.recovery == "a list call worked again"


def test_a_configured_zone_names_its_local_times(tmp_path: Path) -> None:
    log = IncidentLog(str(tmp_path), tz=zoneinfo.ZoneInfo("Europe/Athens"))
    incident = owner_incident(
        machine="wsl", what="x", why="", source="owner", start_ms=parse_iso("2026-11-27T10:57:15Z") or 0, end_ms=None
    )
    log.write(incident)
    [row] = lines(log)
    assert row["zone"] == "Europe/Athens"
    assert row["startLocal"] == "2026-11-27 12:57:15"


def test_texts_are_cleaned_redacted_and_bounded() -> None:
    assert clean_text("a\x1b[31m red\x07\n line\u202e", 100) == "a red line"
    assert "ghp_" not in clean_text(SCREEN_SECRET, 200)
    assert len(clean_text("x" * 5000, MAX_WHY)) == MAX_WHY


def test_unreadable_lines_are_counted_and_skipped(world: World) -> None:
    world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="w")
    with open(world.log.path, "ab") as handle:
        handle.write(b"not json\n" + b'{"id": "zz"}\n' + b"x" * 20000 + b"\n")
    incidents, bad = world.log.read()
    assert len(incidents) == 1
    assert bad == 3


def test_rotation_keeps_open_incidents_and_bounds_the_files(tmp_path: Path) -> None:
    log = IncidentLog(str(tmp_path), max_bytes=4096, keep_files=2)
    still = owner_incident(machine="wsl", what="still open", why="", source="cli", start_ms=T0, end_ms=None)
    log.write(still)
    for index in range(100):
        log.write(
            owner_incident(machine="dev", what=f"note {index}", why="w" * 100, source="cli", start_ms=T0, end_ms=T0)
        )
    names = sorted(os.listdir(tmp_path))
    assert "incidents.jsonl.3" not in names
    assert "incidents.jsonl.2" in names
    for name in names:
        if name.startswith("incidents.jsonl"):
            assert os.path.getsize(tmp_path / name) <= 4096
    incidents, _ = log.read()
    assert any(i.id == still.id and i.open for i in incidents)
    with open(log.path, encoding="utf-8") as handle:
        assert still.id in handle.read()


def test_a_refused_write_is_logged_not_raised(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    log = IncidentLog(str(blocked / "state"))
    assert not log.write(owner_incident(machine="wsl", what="x", why="", source="cli", start_ms=T0, end_ms=T0))


# --- the tracker ----------------------------------------------------------------------------------


def test_repeats_only_count_and_success_closes_after_the_settle_time(world: World) -> None:
    for _ in range(50):
        world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="w")
        world.clock.t += MINUTE_MS
    assert len(lines(world.log)) == 1
    world.tracker.recovered("wsl", components=["tmux"], recovery="worked")
    world.clock.t += 60 * SECOND_MS
    world.tracker.tick()
    assert world.tracker.open_incidents
    world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="w")
    world.clock.t += 120 * SECOND_MS
    world.tracker.tick()
    assert world.tracker.open_incidents, "a failure during the settle time keeps it open"
    world.tracker.recovered("wsl", components=["tmux"], recovery="worked")
    recovered_at = world.clock.t
    world.clock.t += 121 * SECOND_MS
    world.tracker.tick()
    assert not world.tracker.open_incidents
    closed = lines(world.log)[-1]
    assert closed["count"] == 51
    assert parse_iso(closed["end"]) == recovered_at
    assert len(lines(world.log)) == 2


async def test_pushes_name_the_machine_and_the_fixed_text(world: World) -> None:
    world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="w")
    world.tracker.recovered("wsl", components=["tmux"], recovery="r")
    world.clock.t += 200 * SECOND_MS
    world.tracker.tick()
    await asyncio.sleep(0)
    assert world.pushes == ["Incident on wsl: tmux failed.", "Resolved on wsl: tmux failed."]


def test_a_flood_becomes_one_counted_incident(world: World) -> None:
    for index in range(MAX_NEW_PER_HOUR + 20):
        world.tracker.fail("wsl", "transport", f"code_{index}", what="x", why="y")
    open_codes = {i.code for i in world.tracker.open_incidents}
    assert len(open_codes) == MAX_NEW_PER_HOUR + 1
    assert "incident_flood" in open_codes
    flood = next(i for i in world.tracker.open_incidents if i.code == "incident_flood")
    assert flood.machine == HUB
    assert flood.count == 20
    assert len(lines(world.log)) == MAX_NEW_PER_HOUR + 1


def test_disabled_writes_nothing(tmp_path: Path) -> None:
    log = IncidentLog(str(tmp_path))
    tracker = IncidentTracker(TrackerOptions(log=log, enabled=False))
    tracker.fail("wsl", "tmux", "tmux_failed", what="x", why="y")
    assert not os.path.exists(log.path)


def test_open_incidents_of_an_earlier_run_are_adopted(world: World) -> None:
    world.tracker.fail("wsl", "tmux", "tmux_failed", what="tmux failed", why="w")
    later = IncidentTracker(TrackerOptions(log=world.log, settle_seconds=0, now=world.clock))
    assert later.adopt() == 1
    observe_call(later, agent(), "list", OK)
    assert not later.open_incidents
    assert lines(world.log)[-1]["end"] is not None


# --- dispatcher calls -------------------------------------------------------------------------------


def test_a_tmux_failure_opens_a_tmux_incident_and_a_list_closes_it(world: World) -> None:
    observe_call(world.tracker, agent(), "list", failed("remote_failed", "tmux_failed"))
    [incident] = world.tracker.open_incidents
    assert (incident.component, incident.code, incident.what) == ("tmux", "tmux_failed", "tmux failed")
    assert "read-only" in incident.why
    observe_call(world.tracker, agent(), "list", OK)
    world.clock.t += 121 * SECOND_MS
    world.tracker.tick()
    assert not world.tracker.open_incidents


def test_ordinary_refusals_are_not_incidents(world: World) -> None:
    for detail in ("gone", "changed", "not_allowed", "draft_present", "bad_request"):
        observe_call(world.tracker, agent(), "send", failed("refused_by_policy", detail))
    observe_call(world.tracker, agent(), "list", failed("not_connected"))
    assert not world.tracker.open_incidents


def test_policy_busy_transport_and_dispatcher_failures(world: World) -> None:
    ssh = MachineConfig(name="box", host="example-host", user="bot", identity_file="/k", sessions=["a"])
    observe_call(world.tracker, ssh, "list", failed("refused_by_policy", "bad_policy"))
    observe_call(world.tracker, ssh, "list", failed("refused_by_policy", "busy"))
    observe_call(world.tracker, ssh, "list", failed("timeout"))
    observe_call(world.tracker, ssh, "list", failed("host_key"))
    observe_call(world.tracker, ssh, "list", failed("dispatcher_missing"))
    observe_call(world.tracker, ssh, "list", failed("other", "crashed"))
    observe_call(world.tracker, ssh, "list", failed("not_connected"))
    found = {(i.component, i.code) for i in world.tracker.open_incidents}
    assert found == {
        ("dispatcher", "bad_policy"),
        ("dispatcher", "busy"),
        ("transport", "timeout"),
        ("transport", "host_key"),
        ("dispatcher", "dispatcher_missing"),
        ("dispatcher", "crashed"),
        ("transport", "not_connected"),
    }


def test_no_machine_text_reaches_the_log(world: World) -> None:
    observe_call(world.tracker, agent(), "list", failed("refused_by_policy", SCREEN_SECRET))
    observe_call(world.tracker, agent(), "list", failed("remote_failed", SCREEN_SECRET))
    observe_call(world.tracker, agent(), "list", failed("weird code; rm -rf", SCREEN_SECRET))
    with open(world.log.path, encoding="utf-8") as handle:
        text = handle.read()
    assert "TOKEN" not in text
    assert "ghp_" not in text
    assert "rm -rf" not in text


async def test_observed_transport_passes_the_result_through(world: World) -> None:
    class Fake:
        machine = agent()

        async def call(
            self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
        ) -> TransportResult:
            return failed("remote_failed", "tmux_failed")

    observed = ObservedTransport(Fake(), world.tracker)
    result = await observed.call("list", timeout_seconds=1)
    assert result.detail == "tmux_failed"
    assert world.tracker.open_incidents


# --- the error mapping ---------------------------------------------------------------------------


def test_a_tmux_failure_is_remote_failed_not_refused_by_policy() -> None:
    stdout = json.dumps({"protocol": 1, "ok": False, "error": "tmux_failed"}).encode()
    result = interpret("list", ProcessResult(returncode=0, stdout=stdout, stderr_tail=""), via_ssh=True, started=0)
    assert (result.error, result.detail) == ("remote_failed", "tmux_failed")
    gone = json.dumps({"protocol": 1, "ok": False, "error": "gone"}).encode()
    refused = interpret("capture", ProcessResult(returncode=0, stdout=gone, stderr_tail=""), via_ssh=True, started=0)
    assert refused.error == "refused_by_policy"
    assert unreachable_sentence("wsl", "remote_failed", "tmux_failed") == "wsl answered, but tmux failed there."
    view = MachineView(machine=agent(), error="remote_failed", detail="tmux_failed")
    assert machine_line(view) == "wsl: answered, but tmux failed there."


# --- agent links and the relay -----------------------------------------------------------------------


def test_a_flapping_link_stays_one_incident(world: World) -> None:
    for _ in range(20):
        link_down(world.tracker, "wsl", "eof", world.clock.t)
        world.clock.t += 10 * SECOND_MS
        link_up(world.tracker, "wsl", world.clock.t)
        world.clock.t += 20 * SECOND_MS
        world.tracker.tick()
    world.clock.t += 121 * SECOND_MS
    world.tracker.tick()
    rows = lines(world.log)
    assert len(rows) == 2
    assert rows[-1]["count"] == 20
    assert rows[-1]["code"] == "link_down"
    assert rows[-1]["recovery"].startswith("it connected again after")


def test_todays_wsl_restart_closes_with_the_gap(world: World) -> None:
    down = parse_iso("2026-11-27T12:03:48Z") or 0
    up = parse_iso("2026-11-27T12:03:58Z") or 0
    world.clock.t = up
    link_down(world.tracker, "wsl", "eof", down)
    link_up(world.tracker, "wsl", up)
    world.clock.t += 121 * SECOND_MS
    world.tracker.tick()
    row = lines(world.log)[-1]
    assert (row["start"], row["end"], row["durationSeconds"]) == (
        "2026-11-27T12:03:48.000Z",
        "2026-11-27T12:03:58.000Z",
        10,
    )
    assert row["recovery"] == "it connected again after 10 seconds"
    assert "went away" in row["why"]


def test_relay_problems_are_events(world: World) -> None:
    relay_problem(world.tracker, "relay_wrong_user", None)
    relay_problem(world.tracker, "relay_wrong_user", None)
    [incident] = world.tracker.open_incidents
    assert (incident.machine, incident.component, incident.count, incident.instant) == (HUB, "relay", 2, True)
    world.clock.t += 121 * SECOND_MS
    world.tracker.tick()
    assert not world.tracker.open_incidents
    assert "did not happen again" in lines(world.log)[-1]["recovery"]


def test_an_unclean_stop_is_one_closed_incident(world: World) -> None:
    note_unclean_stop(world.tracker, T0 - 5 * MINUTE_MS, None)
    [row] = [r for r in lines(world.log) if r["end"] is not None]
    assert row["code"] == "unclean_stop"
    assert row["durationSeconds"] == 300
    assert not world.tracker.open_incidents


# --- the summariser and tool exceptions ------------------------------------------------------------------


def test_summariser_failure_codes() -> None:
    assert failure_code(RunResult(outcome="timeout")) == "timeout"
    assert failure_code(RunResult(result="API Error: 401 authentication_error")) == "auth_failed"
    assert failure_code(RunResult(stderr_tail="Invalid API key · Please run /login")) == "auth_failed"
    assert failure_code(RunResult(result="boom")) == "error"


async def test_a_tool_exception_is_reported_and_raised_again() -> None:
    seen: list[tuple[str, str]] = []

    async def broken(machine: str | None = None) -> str:
        raise RuntimeError("secret text")

    tool = _guarded(
        Tool.from_function(broken, name="incidents"), lambda name, err: seen.append((name, type(err).__name__))
    )
    with pytest.raises(RuntimeError):
        await tool.fn(machine="wsl")
    assert seen == [("incidents", "RuntimeError")]


# --- machine health ---------------------------------------------------------------------------------


def wsl_health(*, windows_free: int, root_ro: bool = False, available: int = 8 * GIB) -> HealthResponse:
    data = {
        "protocol": 1,
        "ok": True,
        "wsl": True,
        "filesystems": [
            {"name": "root", "totalBytes": 1000 * GIB, "freeBytes": 440 * GIB, "readOnly": root_ro},
            {"name": "home", "totalBytes": 1000 * GIB, "freeBytes": 440 * GIB, "readOnly": root_ro, "sameAs": "root"},
            {"name": "claudeHome", "totalBytes": 1, "freeBytes": 0, "readOnly": root_ro, "sameAs": "root"},
            {"name": "windows", "totalBytes": 1900 * GIB, "freeBytes": windows_free},
            {"name": "future-disk", "totalBytes": 10, "freeBytes": 0},
        ],
        "load": [1.0, 0.5, 0.2],
        "memoryTotalBytes": 16 * GIB,
        "memoryAvailableBytes": available,
        "uptimeSeconds": 12.5,
    }
    parsed = parse_response("health", json.dumps(data).encode())
    assert isinstance(parsed, HealthResponse)
    return parsed


def test_the_windows_disk_near_full_and_a_read_only_root() -> None:
    config = HealthConfig()
    findings = {f.code: f for f in evaluate(wsl_health(windows_free=57 * GIB, root_ro=True), config)}
    assert findings["low_disk_windows"].bad
    assert findings["low_disk_windows"].why.startswith(
        "the Windows disk C: has 57 GB free of 1.9 TB (97 % used): WSL's disk image usually lives there"
    )
    assert findings["read_only_root"].bad
    assert "errors=remount-ro" in findings["read_only_root"].why
    assert not findings["low_disk_root"].bad
    assert "read_only_home" not in findings, "sameAs is skipped"
    assert "read_only_claude_home" not in findings
    assert not findings["low_memory"].bad


def test_thresholds_have_a_margin() -> None:
    config = HealthConfig(min_free_gigabytes=5, min_free_percent=5)
    edge = {f.code: f for f in evaluate(wsl_health(windows_free=int(1900 * GIB * 0.055)), config)}
    assert not edge["low_disk_windows"].bad
    assert not edge["low_disk_windows"].good
    fine = {f.code: f for f in evaluate(wsl_health(windows_free=500 * GIB), config)}
    assert fine["low_disk_windows"].good
    low = {f.code: f for f in evaluate(wsl_health(windows_free=500 * GIB, available=100 * 1024**2), config)}
    assert low["low_memory"].bad


def test_size_words() -> None:
    assert [size_words(v) for v in (57 * GIB, int(1.9 * 1024 * GIB), int(2.5 * GIB), 300 * 1024**2)] == [
        "57 GB",
        "1.9 TB",
        "2.5 GB",
        "300 MB",
    ]


class HealthMachine:
    def __init__(self, machine: MachineConfig, answers: list[TransportResult]) -> None:
        self.machine = machine
        self.answers = answers
        self.calls: list[str] = []

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        self.calls.append(verb)
        return (
            self.answers.pop(0)
            if self.answers
            else TransportResult(ok=True, data=VersionResponse(version="0.6.0", verbs=[]))
        )


async def test_the_poller_opens_and_closes_health_incidents(world: World, tmp_path: Path) -> None:
    machine = agent()
    fake = HealthMachine(
        machine,
        [
            TransportResult(ok=True, data=wsl_health(windows_free=52 * GIB)),
            TransportResult(ok=True, data=wsl_health(windows_free=800 * GIB)),
        ],
    )
    tracker = IncidentTracker(TrackerOptions(log=world.log, settle_seconds=0, now=world.clock))
    poller = HealthPoller(
        HealthPollerOptions(
            remote=RemoteConfig(machines=[machine]),
            health=HealthConfig(),
            transports={"wsl": ObservedTransport(fake, tracker)},
            tracker=tracker,
            state_dir=str(tmp_path / "state"),
            logger=silent_logger,
            now=world.clock,
            marker=False,
        )
    )
    await poller.poll("wsl")
    [incident] = [i for i in tracker.open_incidents if i.machine == "wsl"]
    assert incident.code == "low_disk_windows"
    assert "52 GB free" in incident.what
    await poller.poll("wsl")
    assert not [i for i in tracker.open_incidents if i.machine == "wsl"]


async def test_an_old_dispatcher_is_polled_with_version(world: World, tmp_path: Path) -> None:
    machine = agent()
    fake = HealthMachine(machine, [failed("refused_by_policy", "bad_request")])
    poller = HealthPoller(
        HealthPollerOptions(
            remote=RemoteConfig(machines=[machine]),
            health=HealthConfig(),
            transports={"wsl": ObservedTransport(fake, world.tracker)},
            tracker=world.tracker,
            state_dir=str(tmp_path),
            logger=silent_logger,
            now=world.clock,
            marker=False,
        )
    )
    await poller.poll("wsl")
    await poller.poll("wsl")
    assert fake.calls == ["health", "version"]
    assert not world.tracker.open_incidents


async def test_a_marker_left_behind_is_an_unclean_stop(world: World, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "hub-alive.json").write_text(json.dumps({"version": 1, "seenMs": T0 - MINUTE_MS}))

    def poller() -> HealthPoller:
        return HealthPoller(
            HealthPollerOptions(
                remote=RemoteConfig(),
                health=HealthConfig(interval_seconds=0),
                transports={},
                tracker=world.tracker,
                state_dir=str(state),
                logger=silent_logger,
                now=world.clock,
            )
        )

    first = poller()
    await first.start()
    await first.stop()
    assert not (state / "hub-alive.json").exists()
    codes = [row["code"] for row in lines(world.log)]
    assert codes.count("unclean_stop") == 2  # opened and closed at once
    second = poller()
    await second.start()
    await second.stop()
    assert [row["code"] for row in lines(world.log)].count("unclean_stop") == 2, "a clean stop leaves no marker"


# --- the dispatcher's health verb ----------------------------------------------------------------------


def fake_proc(root: Path, *, wsl: bool, drive: Path | None) -> Path:
    proc = root / "proc"
    (proc / "sys" / "kernel").mkdir(parents=True)
    (proc / "self").mkdir()
    (proc / "sys" / "kernel" / "osrelease").write_text(
        "6.6.87.2-microsoft-standard-WSL2\n" if wsl else "6.8.0-generic\n"
    )
    mounts = "/dev/sdc / ext4 rw 0 0\n" + (f"C:\\134 {drive} 9p rw 0 0\n" if drive else "")
    (proc / "self" / "mounts").write_text(mounts)
    (proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n")
    (proc / "uptime").write_text("1234.56 99.0\n")
    return proc


def systems(body: dict[str, object]) -> list[dict[str, Any]]:
    value = body["filesystems"]
    assert isinstance(value, list)
    return value


def test_health_verb_on_a_wsl_like_machine(tmp_path: Path) -> None:
    drive = tmp_path / "c"
    drive.mkdir()
    proc = fake_proc(tmp_path, wsl=True, drive=drive)
    env = {"HOME": str(tmp_path)}
    body = d.verb_health(d.Policy(), env, proc=str(proc), host_drive=str(drive))
    names = [fs["name"] for fs in systems(body)]
    assert body["wsl"] is True
    assert names == ["root", "home", "claudeHome", "windows"]
    by_name = {fs["name"]: fs for fs in systems(body)}
    assert by_name["windows"]["sameAs"] in ("root", "home") or "freeBytes" in by_name["windows"]
    assert by_name["home"].get("sameAs") in (None, "root")
    assert body["memoryAvailableBytes"] == 8_000_000 * 1024
    assert body["uptimeSeconds"] == 1234.6
    assert not any("_device" in fs for fs in systems(body))
    text = json.dumps(body)
    assert str(tmp_path) not in text, "labels only, never paths"
    parsed = parse_response("health", json.dumps({"protocol": 1, "ok": True, **body}).encode())
    assert isinstance(parsed, HealthResponse)


def test_health_verb_flags_read_only_and_skips_an_unmounted_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = fake_proc(tmp_path, wsl=True, drive=None)
    real = os.statvfs

    def read_only(path: str) -> os.statvfs_result:
        got = real(path)
        return os.statvfs_result((*tuple(got)[:8], got.f_flag | os.ST_RDONLY, got.f_namemax))

    monkeypatch.setattr(os, "statvfs", read_only)
    body = d.verb_health(d.Policy(), {"HOME": str(tmp_path)}, proc=str(proc), host_drive="/nonexistent-drive")
    by_name = {fs["name"]: fs for fs in systems(body)}
    assert by_name["root"]["readOnly"] is True
    assert by_name["windows"] == {"name": "windows", "error": "not_mounted"}


def test_a_hanging_windows_drive_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    real = d._filesystem

    def slow(name: str, path: str) -> dict[str, object]:
        if name == "windows":
            release.wait(5)
        return real(name, path)

    monkeypatch.setattr(d, "_filesystem", slow)
    monkeypatch.setattr(d, "HOST_PROBE_SECONDS", 0.2)
    drive = tmp_path / "c"
    drive.mkdir()
    proc = fake_proc(tmp_path, wsl=True, drive=drive)
    first = d.verb_health(d.Policy(), {"HOME": str(tmp_path)}, proc=str(proc), host_drive=str(drive))
    second = d.verb_health(d.Policy(), {"HOME": str(tmp_path)}, proc=str(proc), host_drive=str(drive))
    release.set()
    for body in (first, second):
        windows = systems(body)[-1]
        assert windows == {"name": "windows", "error": "timeout"}


def test_health_is_a_plain_verb_of_the_dispatcher(tmp_path: Path) -> None:
    assert tuple(int(part) for part in d.VERSION.split(".")) >= (0, 9, 0)
    assert "health" in d.VERBS
    proc = fake_proc(tmp_path, wsl=False, drive=None)
    body = d.verb_health(d.Policy(), {"HOME": str(tmp_path)}, proc=str(proc))
    assert body["wsl"] is False
    assert [fs["name"] for fs in systems(body)] == ["root", "home", "claudeHome"]
    policy = d.Policy()
    policy.sessions = ["dev"]
    [answer] = list(d.run_request(policy, ["health"], {"HOME": str(tmp_path)}))
    assert answer["ok"] is True
    assert "filesystems" in answer
    [refused] = list(d.run_request(policy, ["health", "extra"], {"HOME": str(tmp_path)}))
    assert refused == {"protocol": 1, "ok": False, "error": "bad_request"}


# --- spoken -------------------------------------------------------------------------------------------


def test_spoken_summary_and_clause(world: World) -> None:
    now = parse_iso("2026-11-27T14:00:00Z") or 0
    start = parse_iso("2026-11-27T10:57:15Z") or 0
    end = parse_iso("2026-11-27T12:03:58Z") or 0
    note = owner_incident(
        machine="wsl", what="machine down", why="disk full", source="owner", start_ms=start, end_ms=end
    )
    disk = Incident(
        id="0000abcd",
        machine="wsl",
        component="machine_health",
        code="low_disk_windows",
        what="the Windows disk C: is nearly full (57 GB free)",
        why="w",
        source="auto",
        start_ms=end,
    )
    text = format_spoken([note, disk], label="Today", now=now, tz=zoneinfo.ZoneInfo("UTC"), with_why=True)
    assert text.splitlines() == [
        "2 incidents today, 1 still open (times in UTC).",
        "wsl: the Windows disk C: is nearly full (57 GB free), since 12:03, still open. Why: w.",
        'wsl: you noted "machine down", from 10:57 to 12:03 (1 hour 7 minutes). Why: "disk full".',
    ]
    assert open_clause([disk]) == " Open incident: the Windows disk C: is nearly full (57 GB free)."
    assert open_clause([]) == ""


# --- config ----------------------------------------------------------------------------------------------


def test_incidents_config_defaults_and_checks() -> None:
    config = IncidentsConfig()
    assert (config.enabled, config.settle_seconds, config.health.interval_seconds) == (True, 120, 300)
    assert IncidentsConfig.model_validate({"timeZone": "Europe/Athens"}).time_zone == "Europe/Athens"
    with pytest.raises(ValueError, match="IANA"):
        IncidentsConfig.model_validate({"timeZone": "Mars/Base"})
    with pytest.raises(ValueError, match="60 to 3600"):
        HealthConfig.model_validate({"intervalSeconds": 10})


# --- the CLI ---------------------------------------------------------------------------------------------


async def test_cli_add_list_and_close(real_tmp: Path) -> None:
    config = write_config(real_tmp, incidents={"timeZone": "Europe/Athens"})
    code, io = await run_cli(
        "incidents",
        "add",
        "--config",
        config,
        "--machine",
        "wsl",
        "--component",
        "machine_health",
        "--what",
        "machine down: WSL root read-only, Claude Code Bus error (core dumped)",
        "--why",
        "the Windows host disk C: filled up; WSL's ext4 root remounted read-only (errors=remount-ro)",
        "--recovery",
        "WSL restarted 14:03 Athens time",
        "--start",
        "2026-11-27T10:57:15Z",
        "--end",
        "2026-11-27T12:03:58Z",
        "--source",
        "owner",
    )
    assert code == 0, io.stderr
    assert io.stdout.startswith("added ")
    assert "(closed): wsl, machine down" in io.stdout
    assert "wsl is not in remote.machines" in io.stderr
    code, io = await run_cli("incidents", "--config", config)
    assert code == 0
    assert "2026-11-27 12:57:15" in io.stdout
    assert "(times in Europe/Athens)" in io.stdout
    code, io = await run_cli("incidents", "list", "--config", config, "--json")
    [row] = [json.loads(line) for line in io.stdout.splitlines()]
    assert (row["durationSeconds"], row["source"], row["component"]) == (4003, "owner", "machine_health")
    code, io = await run_cli(
        "incidents",
        "add",
        "--config",
        config,
        "--machine",
        "hub",
        "--what",
        "funnel off",
        "--start",
        "2026-11-27 13:00",
    )
    assert code == 0
    assert "(open)" in io.stdout
    ident = io.stdout.split()[1]
    code, io = await run_cli("incidents", "list", "--config", config, "--open", "--details")
    assert code == 0
    assert ident in io.stdout
    assert "machine down" not in io.stdout
    code, io = await run_cli("incidents", "close", ident, "--config", config, "--recovery", "turned on again")
    assert code == 0
    assert io.stdout.startswith(f"closed {ident}")
    code, io = await run_cli("incidents", "close", ident, "--config", config)
    assert code == 1
    assert "already closed" in io.stderr
    code, io = await run_cli("incidents", "add", "--config", config, "--machine", "Bad Name", "--what", "x")
    assert code == 2


async def test_cli_rejects_a_bad_time(real_tmp: Path) -> None:
    config = write_config(real_tmp)
    code, io = await run_cli(
        "incidents", "add", "--config", config, "--machine", "wsl", "--what", "x", "--start", "noon"
    )
    assert code == 2
    assert "ISO 8601" in io.stderr


# --- the tools ---------------------------------------------------------------------------------------------


async def test_tools_report_note_and_mention_open_incidents(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world)
    world.configure(modes={"example-host": "connect_timeout"})
    ctx = await build(tmp_path, world, summary=False)
    try:
        tools = ctx.tools
        tools.incidents.fail(
            "dev", "machine_health", "low_disk_windows", what="the Windows disk C: is nearly full (52 GB free)", why="w"
        )
        listed = await tools.list_remote_sessions(None)
        lines_of = listed.text.splitlines()
        assert lines_of[1].endswith(" Open incident: the Windows disk C: is nearly full (52 GB free).")
        # buildbox's own timeout incident is not repeated: its line already says it timed out.
        assert lines_of[2] == "buildbox: not reachable, the connection timed out."
        status = await tools.remote_status("dev", "fpga", None)
        assert status.text.endswith("Open incident: the Windows disk C: is nearly full (52 GB free).")

        noted = tools.incident_note("dev", "the build \x1b[31mbroke " + SCREEN_SECRET, None)
        assert noted.text.startswith("Noted for dev: the build broke export TOKEN=")
        assert "ghp_" not in noted.text
        assert tools.incident_note("nowhere", "x", None).is_error

        report = tools.incidents_report(None, None, False)
        assert report.text.startswith("3 incidents today, 2 still open")
        assert 'dev: you noted "the build broke' in report.text
        assert "buildbox: no answer in time" in report.text
        only = tools.incidents_report("buildbox", None, True)
        assert only.text.splitlines()[1].startswith("buildbox: no answer in time, since ")
        assert tools.incidents_report(None, "someday", False).is_error
        hub = tools.incidents_report("hub", None, False)
        assert hub.text == "No incidents today."
    finally:
        await ctx.manager.shutdown("test finished")


def test_period_words() -> None:
    assert [period_words(x) for x in ("Today", "Last week", "November 2026", "November 3, 2026", "open now")] == [
        "today",
        "last week",
        "in November 2026",
        "on November 3, 2026",
        "open now",
    ]
    assert period_words("From November 1, 2026 to November 3, 2026").startswith("from November 1")
