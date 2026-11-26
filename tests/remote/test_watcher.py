"""The notification watcher: transitions, debounce, restart dedupe, long turns, unreachable pushes,
and push texts without pane content (unit tests on built reports, and one run on the fake world)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.helpers import config_data
from tests.remote.helpers import SESSION_A, SESSION_B, RemoteWorld, pane
from tests.remote.test_tools import build
from voice_coding_mcp import http as http_module
from voice_coding_mcp.auth import OAuthStore
from voice_coding_mcp.clock import MINUTE_MS
from voice_coding_mcp.config import parse_config
from voice_coding_mcp.http import CreateAppOptions, create_app
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote.config import MachineConfig, WatchConfig
from voice_coding_mcp.remote.inventory import MachineView
from voice_coding_mcp.remote.pending import PendingEntry, PendingKind, PendingReport, PendingSource, TrackedSession
from voice_coding_mcp.remote.watcher import Watcher, WatcherOptions
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

T0 = 1_764_160_000_000


def tracked(
    key: str = f"dev|%1|{SESSION_A}",
    place: str = "fpga on dev",
    status: str | None = "waiting",
    at: int | None = None,
    machine: str | None = "dev",
) -> TrackedSession:
    return TrackedSession(key=key, machine=machine, place=place, status=status, status_updated_at=at)


def entry(
    session: TrackedSession | None = None,
    kind: PendingKind = "permission",
    source: PendingSource = "registry",
    question: str | None = "Bash command git push origin main",
) -> PendingEntry:
    s = session or tracked()
    return PendingEntry(
        key=f"{s.key}|{kind}",
        session=s,
        label=f"On {s.machine}, fpga",
        kind=kind,
        question=question,
        source=source,
        permission_answers=False,
    )


def view(name: str, ok: bool = True) -> MachineView:
    machine = MachineConfig(name=name, transport="local", sessions=["fpga"])
    return MachineView(machine=machine) if ok else MachineView(machine=machine, error="timeout")


def report(
    entries: Sequence[PendingEntry] = (),
    sessions: Sequence[TrackedSession] | None = None,
    *,
    down: Sequence[str] = (),
    unknown: Sequence[str] = (),
) -> PendingReport:
    views = [view("dev", ok="dev" not in down), view("buildbox", ok="buildbox" not in down)]
    return PendingReport(
        entries=tuple(entries),
        sessions=tuple(sessions if sessions is not None else [e.session for e in entries]),
        views=tuple(views),
        partial=(),
        unknown=frozenset(unknown),
    )


@dataclass
class Rig:
    tmp: Path
    watch: WatchConfig = field(default_factory=lambda: WatchConfig(interval_seconds=60))
    reports: list[PendingReport] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    now: list[int] = field(default_factory=lambda: [T0])

    def watcher(self) -> Watcher:
        async def check() -> PendingReport:
            return self.reports.pop(0)

        async def send(text: str) -> None:
            self.sent.append(text)

        return Watcher(
            WatcherOptions(
                watch=self.watch,
                check=check,
                send=send,
                state_dir=str(self.tmp / "state"),
                logger=silent_logger,
                now=lambda: self.now[0],
            )
        )

    async def ticks(self, watcher: Watcher, *reports: PendingReport) -> list[list[str]]:
        out = []
        for r in reports:
            self.reports.append(r)
            out.append(await watcher.tick())
            self.now[0] += MINUTE_MS
        return out


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp=tmp_path)


async def test_a_registry_wait_pushes_once_then_again_after_it_was_gone(rig: Rig) -> None:
    w = rig.watcher()
    wait = entry()
    pushes = await rig.ticks(w, report([wait]), report([wait]), report([]), report([wait]))
    assert pushes == [["fpga on dev needs a decision."], [], [], ["fpga on dev needs a decision."]]
    assert rig.sent == ["fpga on dev needs a decision."] * 2


async def test_a_question_reads_differently(rig: Rig) -> None:
    w = rig.watcher()
    pushes = await rig.ticks(w, report([entry(kind="question", question="keep the old endpoint?")]))
    assert pushes == [["fpga on dev asks you something."]]


async def test_a_screen_only_wait_needs_two_ticks_in_a_row(rig: Rig) -> None:
    w = rig.watcher()
    seen = entry(source="screen")
    pushes = await rig.ticks(w, report([seen]), report([]), report([seen]), report([seen]), report([seen]))
    assert pushes == [[], [], [], ["fpga on dev needs a decision."], []]


async def test_a_wait_that_could_not_be_checked_counts_as_still_there(rig: Rig) -> None:
    w = rig.watcher()
    wait = entry()
    screen = entry(tracked(key=f"dev|%2|{SESSION_B}", place="notes on dev"), kind="question", source="screen")
    pushes = await rig.ticks(
        w,
        report([wait, screen]),
        report([wait, screen]),
        # dev does not answer, then the pane of the screen wait cannot be captured: nothing is lost.
        report([], [], down=["dev"]),
        report([wait], [wait.session, screen.session], unknown=[screen.session.key]),
        report([wait, screen]),
    )
    assert pushes == [["fpga on dev needs a decision."], ["notes on dev asks you something."], [], [], []]


async def test_a_restart_does_not_repeat_announced_waits(rig: Rig) -> None:
    wait = entry()
    first = rig.watcher()
    assert await rig.ticks(first, report([wait])) == [["fpga on dev needs a decision."]]
    second = rig.watcher()
    assert await rig.ticks(second, report([wait]), report([]), report([wait])) == [
        [],
        [],
        ["fpga on dev needs a decision."],
    ]


async def test_the_state_file_holds_ids_only(rig: Rig) -> None:
    w = rig.watcher()
    await rig.ticks(w, report([entry(question="secret question text")]))
    path = rig.tmp / "state" / "watch.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "announced": [f"dev|%1|{SESSION_A}|permission"], "unreachable": []}


async def test_a_broken_state_file_is_ignored(rig: Rig) -> None:
    (rig.tmp / "state").mkdir()
    (rig.tmp / "state" / "watch.json").write_text("{not json", encoding="utf-8")
    w = rig.watcher()
    assert await rig.ticks(w, report([entry()])) == [["fpga on dev needs a decision."]]


async def test_include_question_appends_the_quoted_question(rig: Rig) -> None:
    rig.watch = WatchConfig(interval_seconds=60, include_question=True)
    w = rig.watcher()
    long = 'run "make" ' + "x" * 200
    pushes = await rig.ticks(w, report([entry(question=long)]))
    text = pushes[0][0]
    assert text.startswith("fpga on dev needs a decision: \"run 'make' xxx")
    assert len(text) == len("fpga on dev needs a decision: ") + 122
    plain = await rig.ticks(w, report([]), report([entry(question=None)]))
    assert plain == [[], ["fpga on dev needs a decision."]]


async def _tick(rig: Rig, w: Watcher, r: PendingReport, at: int) -> list[str]:
    rig.now[0] = at
    rig.reports.append(r)
    return await w.tick()


async def test_a_long_turn_pushes_once_when_it_ends(rig: Rig) -> None:
    w = rig.watcher()
    busy = tracked(status="busy", at=T0 - 20 * MINUTE_MS)
    assert await _tick(rig, w, report([], [busy]), T0) == []
    assert await _tick(rig, w, report([], [busy]), T0 + 3 * MINUTE_MS) == []
    idle = tracked(status="idle", at=T0 + 5 * MINUTE_MS)
    assert await _tick(rig, w, report([], [idle]), T0 + 6 * MINUTE_MS) == [
        "fpga on dev finished its turn after 25 minutes."
    ]
    assert await _tick(rig, w, report([], [idle]), T0 + 7 * MINUTE_MS) == []


async def test_short_turns_and_turns_off_push_nothing(rig: Rig) -> None:
    w = rig.watcher()
    assert await _tick(rig, w, report([], [tracked(status="busy", at=T0)]), T0) == []
    assert await _tick(rig, w, report([], [tracked(status="idle", at=T0 + 9 * MINUTE_MS)]), T0 + 10 * MINUTE_MS) == []
    rig.watch = WatchConfig(interval_seconds=60, long_turn_minutes=0)
    off = rig.watcher()
    assert await _tick(rig, off, report([], [tracked(status="busy", at=T0)]), T0) == []
    assert await _tick(rig, off, report([], [tracked(status="idle")]), T0 + 90 * MINUTE_MS) == []


async def test_a_turn_without_a_status_time_is_timed_from_the_first_sighting(rig: Rig) -> None:
    w = rig.watcher()
    assert await _tick(rig, w, report([], [tracked(status="busy")]), T0) == []
    assert await _tick(rig, w, report([], [tracked(status="idle")]), T0 + 12 * MINUTE_MS) == [
        "fpga on dev finished its turn after 12 minutes."
    ]


async def test_a_long_turn_that_ends_in_a_new_wait_pushes_only_the_wait(rig: Rig) -> None:
    w = rig.watcher()
    assert await _tick(rig, w, report([], [tracked(status="busy", at=T0)]), T0) == []
    waiting = entry(tracked(status="waiting"))
    assert await _tick(rig, w, report([waiting]), T0 + 30 * MINUTE_MS) == ["fpga on dev needs a decision."]


async def test_a_session_that_ended_is_forgotten(rig: Rig) -> None:
    w = rig.watcher()
    assert await _tick(rig, w, report([], [tracked(status="busy", at=T0)]), T0) == []
    assert await _tick(rig, w, report([], []), T0 + 20 * MINUTE_MS) == []
    assert await _tick(rig, w, report([], [tracked(status="idle")]), T0 + 40 * MINUTE_MS) == []


async def test_a_busy_session_on_a_machine_that_did_not_answer_is_kept(rig: Rig) -> None:
    w = rig.watcher()
    assert await _tick(rig, w, report([], [tracked(status="busy", at=T0)]), T0) == []
    assert await _tick(rig, w, report([], [], down=["dev"]), T0 + 10 * MINUTE_MS) == []
    assert await _tick(rig, w, report([], [tracked(status="idle", at=T0 + 15 * MINUTE_MS)]), T0 + 16 * MINUTE_MS) == [
        "fpga on dev finished its turn after 15 minutes."
    ]


async def test_unreachable_pushes_are_off_by_default(rig: Rig) -> None:
    w = rig.watcher()
    pushes = await rig.ticks(w, *[report(down=["buildbox"]) for _ in range(5)], report())
    assert pushes == [[]] * 6


async def test_unreachable_after_three_failed_ticks_and_once_back(rig: Rig) -> None:
    rig.watch = WatchConfig(interval_seconds=60, notify_unreachable=True)
    w = rig.watcher()
    pushes = await rig.ticks(
        w,
        report(down=["buildbox"]),
        report(down=["buildbox"]),
        report(down=["buildbox"]),
        report(down=["buildbox"]),
        report(),
        report(),
        report(down=["buildbox"]),
    )
    assert pushes == [[], [], ["Machine buildbox cannot be reached."], [], ["Machine buildbox answers again."], [], []]
    # A restart while it is down does not repeat the push.
    again = rig.watcher()
    await rig.ticks(again, report(down=["buildbox"]), report(down=["buildbox"]))
    assert await rig.ticks(again, report(down=["buildbox"])) == [["Machine buildbox cannot be reached."]]
    restarted = rig.watcher()
    assert await rig.ticks(restarted, *[report(down=["buildbox"]) for _ in range(3)], report()) == [
        [],
        [],
        [],
        ["Machine buildbox answers again."],
    ]


async def test_local_sessions_are_named_by_their_project(rig: Rig) -> None:
    w = rig.watcher()
    local = tracked(key=f"local|{SESSION_A}", place="A session in alpha on this machine", machine=None)
    assert await rig.ticks(w, report([entry(local)])) == [["A session in alpha on this machine needs a decision."]]


async def test_start_and_stop(rig: Rig) -> None:
    w = rig.watcher()
    w.start()
    await w.stop()
    rig.watch = WatchConfig()
    off = rig.watcher()
    assert not off.enabled
    off.start()
    await off.stop()


# --- on the fake world ----------------------------------------------------------------------------

DIALOG_WITH_SECRET = "\n".join(
    [
        "⏺ PANE-ONLY-TEXT the build log says token=s3cr3tvalue",
        "╭───────────────────────────────╮",
        "│ Bash command                  │",
        "│   git push origin main        │",
        "│ Do you want to proceed?       │",
        "│ ❯ 1. Yes                      │",
        "│   2. No                       │",
        "╰───────────────────────────────╯",
    ]
)


@pytest.mark.parametrize("include_question", [False, True])
async def test_pushes_from_real_checks_carry_no_pane_text(tmp_path: Path, include_question: bool) -> None:
    world = RemoteWorld.create(tmp_path)
    world.set_panes(
        [pane("fpga", "%1", 9_999_801, command="claude"), pane("notes", "%2", 9_999_802, command="claude")],
        {"%1": DIALOG_WITH_SECRET},
    )
    world.configure(modes={"example-host": "connect_timeout"})
    world.register("2001", os.getpid(), sessionId=SESSION_A, name="evil name", status="idle", tmux="0:@0.%1")
    world.register(
        "2002",
        os.getpid(),
        sessionId=SESSION_B,
        name="other evil name",
        status="waiting",
        waitingFor="answer token=abcdefsecret",
        tmux="1:@1.%2",
    )
    c = await build(tmp_path, world, summary=False)
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    w = Watcher(
        WatcherOptions(
            watch=WatchConfig(interval_seconds=60, include_question=include_question),
            check=c.tools.collect_pending,
            send=send,
            state_dir=c.config.state_dir,
            logger=silent_logger,
        )
    )
    try:
        await w.tick()
        await w.tick()
    finally:
        await c.manager.shutdown("test finished")
    if include_question:
        assert sent == [
            'notes on dev asks you something: "answer token=[secret]"',
            'fpga on dev needs a decision: "Bash command git push origin main"',
        ]
    else:
        assert sent == ["notes on dev asks you something.", "fpga on dev needs a decision."]
    joined = "\n".join(sent)
    for leak in ["PANE-ONLY-TEXT", "s3cr3tvalue", "abcdefsecret", "evil name"]:
        assert leak not in joined


# --- wiring -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("with_sender", [True, False])
async def test_the_app_starts_the_watcher_with_the_server_and_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_sender: bool
) -> None:
    events: list[str] = []

    class FakeWatcher:
        def __init__(self, options: WatcherOptions) -> None:
            events.append(f"created {options.watch.interval_seconds}")

        def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(http_module, "Watcher", FakeWatcher)
    projects = tmp_path / "projects"
    projects.mkdir()
    config = parse_config(
        config_data(
            "https://vcm.example.com",
            str(tmp_path / "state"),
            str(projects),
            watch={"intervalSeconds": 30},
            notify={"url": "https://ntfy.example.com/topic"},
        ),
        home_dir="/nonexistent-home-dir",
    )
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=silent_logger,
        )
    )

    async def send(text: str) -> None:
        raise AssertionError("no push expected")

    app = create_app(
        CreateAppOptions(
            config=config,
            logger=silent_logger,
            store=OAuthStore(config.state_dir, access_token_hours=24, refresh_token_days=90),
            manager=manager,
            send_text=send if with_sender else None,
        )
    )
    try:
        async with app.router.lifespan_context(app):
            events.append("serving")
    finally:
        await manager.shutdown("test finished")
    assert events == (["created 30", "start", "serving", "stop"] if with_sender else ["serving"])
