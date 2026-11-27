"""Session events end to end: the dispatcher's `events` verb on a spool written by the real
switchboard-hook, the hub's poller (acknowledgements, at-least-once with dedupe across restarts,
the machine named by the link and never by the event, per-machine rate limit, older dispatchers),
the incident log entries, and the whole chain over a real agent link (dispatcher in serve mode,
relay) to a Discord MockTransport."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.helpers import FakeClock, LogCapture
from tests.remote.agent_helpers import MachineLink, ServeSession, agent_machine, agents_section, socket_dir, wait_until
from tests.remote.helpers import RemoteWorld, pane
from tests.test_cli import run_cli, write_config
from voice_coding_mcp.config import parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import hook as h
from voice_coding_mcp.remote.config import DiscordConfig, EventsConfig, MachineConfig, RemoteConfig
from voice_coding_mcp.remote.discord import DiscordNotifier
from voice_coding_mcp.remote.events import EventPoller, EventPollerOptions, check_event
from voice_coding_mcp.remote.incidents import (
    IncidentLog,
    IncidentTracker,
    ObservedTransport,
    TrackerOptions,
    event_entry,
)
from voice_coding_mcp.remote.protocol import EventsResponse, SessionEvent, parse_response
from voice_coding_mcp.remote.tools import RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import TransportResult, dispatcher_error
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

T0 = 1_795_000_000_000
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
URL = "https://discord.com/api/webhooks/123456789012345678/" + "A" * 68
POLICY = ["--sessions", "fpga,notes", "--allow-events"]


def note(kind: str = "approval_needed", **keys: Any) -> dict[str, object]:
    return {
        "v": 1,
        "kind": kind,
        "time": T0,
        "sessionId": SESSION,
        "sessionName": "fix tests",
        "project": "fpgadc",
        "pane": "%1",
        "message": "Claude needs your permission to use Bash",
        "url": None,
        "level": None,
        **keys,
    }


def spool(world: RemoteWorld, *events: dict[str, object]) -> list[str]:
    env = {"HOME": world.home}
    path = h.ensure_spool(env)
    assert path is not None
    names = []
    for index, event in enumerate(events):
        name = h.write_event(path, event, T0 + index)
        assert name is not None
        names.append(name[: -len(".json")])
    return names


def spool_names(world: RemoteWorld) -> list[str]:
    return h.spooled(h.spool_dir({"HOME": world.home}))


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    world.set_panes([pane("fpga", "%1", 9_400_001, command="claude"), pane("private", "%2", 9_400_002)])
    return world


def run(world: RemoteWorld, args: Sequence[str] = (), policy: Sequence[str] = POLICY) -> dict[str, Any]:
    options, _, _ = d.parse_options(list(policy), {})
    docs = list(d.run_request(d.policy_from_options(options), ["events", *args], world.tmux_env()))
    assert len(docs) == 1
    return docs[0]


# --- the dispatcher's verb ---------------------------------------------------------------------


def test_events_need_the_opt_in(world: RemoteWorld) -> None:
    spool(world, note())
    assert run(world, policy=["--sessions", "fpga"]) == {"protocol": 1, "ok": False, "error": "not_allowed"}
    assert len(spool_names(world)) == 1


def test_events_oldest_first_then_deleted_when_named(world: RemoteWorld) -> None:
    ids = spool(world, note(), note("finished", message=None, pane="%2"), note("question", pane=None))
    first = run(world)
    assert first["ok"] is True
    assert [e["id"] for e in first["events"]] == ids
    assert first["events"][0] == {
        "id": ids[0],
        "kind": "approval_needed",
        "time": T0,
        "sessionId": SESSION,
        "pane": "%1",
        "sessionName": "fix tests",
        "project": "fpgadc",
        "message": "Claude needs your permission to use Bash",
        "url": None,
        "level": None,
        "tmuxSession": "fpga",
    }
    # A pane in a tmux session outside --sessions is not named.
    assert first["events"][1]["tmuxSession"] is None
    assert (first["more"], first["pending"], first["full"], first["bad"]) == (False, 3, False, 0)
    second = run(world, ids[:2])
    assert [e["id"] for e in second["events"]] == ids[2:]
    assert run(world, ids[2:])["events"] == []
    assert spool_names(world) == []


def test_at_most_16_per_answer(world: RemoteWorld) -> None:
    spool(world, *[note() for _ in range(20)])
    answer = run(world)
    assert len(answer["events"]) == d.MAX_EVENTS
    assert (answer["more"], answer["pending"]) == (True, 20)


@pytest.mark.parametrize(
    "broken",
    [
        b"{not json",
        json.dumps({**note(), "machine": "dev"}).encode(),
        json.dumps({**note(), "kind": "rm_rf"}).encode(),
        json.dumps({**note(), "url": "https://evil.example/pull/1"}).encode(),
        json.dumps({**note(), "sessionId": "../../x"}).encode(),
        json.dumps({**note(), "pane": "%1;ls"}).encode(),
        json.dumps({**note(), "time": "now"}).encode(),
        json.dumps({**note(), "message": "x" * 5000}).encode(),
    ],
)
def test_files_that_are_not_events_are_deleted_and_counted(world: RemoteWorld, broken: bytes) -> None:
    [good] = spool(world, note())
    directory = Path(h.spool_dir({"HOME": world.home}))
    (directory / "0000000000001-0000000001-deadbeef.json").write_bytes(broken)
    answer = run(world)
    assert [e["id"] for e in answer["events"]] == [good]
    assert answer["bad"] == 1
    assert spool_names(world) == [good + ".json"]


def test_a_symlink_in_the_spool_is_not_followed(world: RemoteWorld, tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(note(message="from outside")))
    spool(world, note())
    directory = Path(h.spool_dir({"HOME": world.home}))
    (directory / "0000000000001-0000000001-deadbeef.json").symlink_to(secret)
    answer = run(world)
    assert [e["message"] for e in answer["events"]] == ["Claude needs your permission to use Bash"]
    assert answer["bad"] == 1
    assert secret.exists()


def test_a_spool_others_can_write_is_refused(world: RemoteWorld) -> None:
    spool(world, note())
    Path(h.spool_dir({"HOME": world.home})).chmod(0o733)
    assert run(world)["error"] == "events_unsafe"


@pytest.mark.parametrize("arg", ["../etc/passwd", "1", "0000000000001-0000000001-DEADBEEF", "x" * 40])
def test_bad_ids_are_bad_requests(world: RemoteWorld, arg: str) -> None:
    assert run(world, [arg])["error"] == "bad_request"
    assert run(world, ["0000000000001-0000000001-deadbeef"] * 17)["error"] == "bad_request"


def test_no_spool_is_no_events(world: RemoteWorld) -> None:
    assert run(world)["events"] == []


def test_serve_mode_answers_events(world: RemoteWorld) -> None:
    ids = spool(world, note())
    session = ServeSession(["--serve", *POLICY], world.tmux_env())
    hello = session.read()
    assert hello is not None
    assert "events" in hello["verbs"]
    [doc] = session.request(1, "events", [])
    assert [e["id"] for e in doc["events"]] == ids
    [doc] = session.request(2, "events", ids)
    assert doc["events"] == []
    assert session.finish() == 0


# --- the hub's poller --------------------------------------------------------------------------


class InProcess:
    """A machine whose dispatcher runs in this process (the real verb), counting its calls."""

    def __init__(self, machine: MachineConfig, world: RemoteWorld, policy: Sequence[str] = POLICY) -> None:
        self.machine = machine
        self.world = world
        self.policy = list(policy)
        self.calls: list[list[str]] = []
        self.forge: dict[str, Any] | None = None

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        self.calls.append([verb, *args])
        options, _, _ = d.parse_options(self.policy, {})
        docs = list(d.run_request(d.policy_from_options(options), [verb, *args], self.world.tmux_env()))
        if self.forge is not None:
            found = docs[0].get("events", [])
            assert isinstance(found, list)
            for event in found:
                event.update(self.forge)
        parsed = parse_response(verb, "\n".join(json.dumps(doc) for doc in docs).encode())
        if isinstance(parsed, str):
            return TransportResult(ok=False, error=dispatcher_error(parsed), detail=parsed)
        return TransportResult(ok=True, data=parsed)


class Hub:
    def __init__(self, tmp_path: Path, world: RemoteWorld, policy: Sequence[str] = POLICY, **events: Any) -> None:
        self.clock = FakeClock(T0 + 1000)
        self.state = str(tmp_path / "state")
        self.log = IncidentLog(self.state)
        self.tracker = IncidentTracker(TrackerOptions(log=self.log, settle_seconds=0, now=self.clock))
        self.machine = MachineConfig(name="laptop", transport="agent", sessions=["fpga", "notes"])
        self.fake = InProcess(self.machine, world, policy)
        self.posts: list[dict[str, Any]] = []
        self.events = EventsConfig(enabled=True, **events)

        def webhook(request: httpx2.Request) -> httpx2.Response:
            self.posts.append(json.loads(request.content))
            return httpx2.Response(204)

        self.discord = DiscordNotifier(
            URL,
            DiscordConfig(coalesce_seconds=0),
            silent_logger,
            client=httpx2.AsyncClient(transport=httpx2.MockTransport(webhook)),
        )
        self.poller = self.new_poller()

    def new_poller(self) -> EventPoller:
        return EventPoller(
            EventPollerOptions(
                remote=RemoteConfig(machines=[self.machine]),
                events=self.events,
                transports={"laptop": ObservedTransport(self.fake, self.tracker)},
                tracker=self.tracker,
                log=self.log,
                state_dir=self.state,
                logger=silent_logger,
                discord=self.discord,
                now=self.clock,
            )
        )

    def entries(self) -> list[dict[str, Any]]:
        with open(self.log.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    async def settle(self) -> None:
        for _ in range(100):
            if not self.discord._buffer and (self.discord._worker is None or self.discord._worker.done()):
                return
            await asyncio.sleep(0.01)


async def test_the_poller_acknowledges_logs_and_posts(tmp_path: Path, world: RemoteWorld) -> None:
    ids = spool(world, note(), note("pr_opened", message="gh pr create", url="https://github.com/o/r/pull/7"))
    hub = Hub(tmp_path, world)
    await hub.poller.poll("laptop")
    assert hub.fake.calls == [["events"], ["events", *ids]]
    assert spool_names(world) == []
    entries = hub.entries()
    assert [(e["entry"], e["component"], e["code"], e["source"], e["machine"]) for e in entries] == [
        ("event", "session", "approval_needed", "session", "laptop"),
        ("event", "session", "pr_opened", "session", "laptop"),
    ]
    assert entries[0]["what"] == "a session waits for approval"
    assert 'session "fix tests"' in entries[0]["why"]
    assert "tmux fpga" in entries[0]["why"]
    assert entries[1]["why"].endswith("https://github.com/o/r/pull/7")
    assert entries[0]["start"] == entries[0]["end"]
    await hub.settle()
    [post] = hub.posts
    assert post["allowed_mentions"] == {"parse": []}
    assert "**Needs your approval** · on laptop" in post["content"]
    assert "<https://github.com/o/r/pull/7>" in post["content"]
    # Events are not incidents: the log's readers leave them out.
    incidents, _ = hub.log.read()
    assert all(i.is_event for i in incidents)
    await hub.discord.aclose()


async def test_a_lost_acknowledgement_delivers_once(tmp_path: Path, world: RemoteWorld) -> None:
    ids = spool(world, note())
    hub = Hub(tmp_path, world)
    original = hub.fake.call

    async def acks_lost(verb: str, args: Sequence[str] = (), **kw: Any) -> TransportResult:
        if args:
            return TransportResult(ok=False, error="disconnected")
        return await original(verb, args, **kw)

    hub.fake.call = acks_lost  # type: ignore[method-assign]
    await hub.poller.poll("laptop")
    assert len(spool_names(world)) == 1
    hub.fake.call = original  # type: ignore[method-assign]
    # A restart: the remembered acknowledgement goes out first.
    restarted = hub.new_poller()
    restarted.load()
    await restarted.poll("laptop")
    assert hub.fake.calls[-1] == ["events", *ids]
    assert spool_names(world) == []
    assert len(hub.entries()) == 1
    await hub.discord.aclose()


async def test_an_event_seen_before_is_not_delivered_again(tmp_path: Path, world: RemoteWorld) -> None:
    spool(world, note())
    hub = Hub(tmp_path, world)
    original = hub.fake.call

    async def acks_lost(verb: str, args: Sequence[str] = (), **kw: Any) -> TransportResult:
        if args:
            return TransportResult(ok=False, error="disconnected")
        return await original(verb, args, **kw)

    hub.fake.call = acks_lost  # type: ignore[method-assign]
    await hub.poller.poll("laptop")
    hub.fake.call = original  # type: ignore[method-assign]
    restarted = hub.new_poller()
    restarted.load()
    restarted._acks.clear()
    await restarted.poll("laptop")
    assert spool_names(world) == []
    assert len(hub.entries()) == 1
    await hub.settle()
    assert len(hub.posts) == 1
    await hub.discord.aclose()


async def test_the_machine_comes_from_the_link_never_the_event(tmp_path: Path, world: RemoteWorld) -> None:
    spool(world, note())
    hub = Hub(tmp_path, world)
    hub.fake.forge = {"machine": "dev", "tmuxSession": "private"}
    await hub.poller.poll("laptop")
    [entry] = hub.entries()
    assert entry["machine"] == "laptop"
    assert "private" not in entry["why"]
    assert "dev" not in entry["why"]
    await hub.discord.aclose()


async def test_a_failed_session_opens_an_incident(tmp_path: Path, world: RemoteWorld) -> None:
    spool(world, note("failed", message="rate_limit"))
    hub = Hub(tmp_path, world)
    await hub.poller.poll("laptop")
    incidents, _ = hub.log.read()
    [incident] = incidents
    assert not incident.is_event
    assert (incident.machine, incident.component, incident.code) == ("laptop", "session", "session_failed")
    assert "rate limit reached" in incident.why
    await hub.discord.aclose()


async def test_the_rate_limit_is_per_machine(tmp_path: Path, world: RemoteWorld) -> None:
    spool(world, *[note(project=f"p{i}") for i in range(5)])
    hub = Hub(tmp_path, world, max_per_hour=3)
    await hub.poller.poll("laptop")
    entries = hub.entries()
    assert [e["code"] for e in entries if e.get("entry") == "event"] == ["approval_needed"] * 3
    assert {e["code"] for e in entries if "entry" not in e} == {"events_flood"}
    assert spool_names(world) == []
    await hub.discord.aclose()


async def test_an_old_dispatcher_or_no_opt_in_is_asked_again_later(tmp_path: Path, world: RemoteWorld) -> None:
    hub = Hub(tmp_path, world, policy=["--sessions", "fpga"])
    hub.poller.tick()
    await asyncio.sleep(0.05)
    assert hub.fake.calls == [["events"]]
    hub.clock.t += 60_000
    hub.poller.tick()
    await asyncio.sleep(0.05)
    assert hub.fake.calls == [["events"]]
    assert hub.tracker.open_incidents == []
    hub.clock.t += 10 * 60_000
    hub.poller.tick()
    await asyncio.sleep(0.05)
    assert len(hub.fake.calls) == 2
    await hub.discord.aclose()


async def test_ssh_machines_are_not_polled_by_default(tmp_path: Path, world: RemoteWorld) -> None:
    hub = Hub(tmp_path, world)
    ssh = MachineConfig(name="box", transport="ssh", host="h", user="u", identity_file="/k", sessions=["x"])
    assert hub.poller.interval_ms(ssh) == 0
    assert hub.poller.interval_ms(hub.machine) == 5000
    await hub.discord.aclose()


def test_the_hub_checks_every_field_again() -> None:
    machine = MachineConfig(name="laptop", transport="agent", sessions=["fpga"])

    def event(**keys: Any) -> SessionEvent:
        return SessionEvent.model_validate({"id": "1795000000000-0000000001-deadbeef", **note(), **keys})

    assert check_event(event(kind="exec"), machine, T0) is None
    assert check_event(event(kind="pr_opened", url="https://github.com/o/r/issues/1"), machine, T0) is None
    checked = check_event(event(kind="owner_note", url="https://evil.example", level="urgent"), machine, T0)
    assert checked is not None
    assert checked.view.url is None
    assert checked.view.level == "urgent"
    checked = check_event(event(level="urgent", tmuxSession="private", sessionId="nope"), machine, T0)
    assert checked is not None
    assert (checked.view.level, checked.view.tmux_session, checked.view.session_id) == (None, None, None)
    checked = check_event(event(kind="failed", message="ghp_abcdefghijklmnopqrstuvwxyz0123456789"), machine, T0)
    assert checked is not None
    assert checked.view.message == "unknown"
    future = check_event(event(time=T0 + 10**9), machine, T0)
    assert future is not None
    assert future.time_ms == T0


def test_the_events_response_model_is_lenient_and_capped() -> None:
    doc = {"protocol": 1, "ok": True, "events": [], "more": False, "unknownField": 1}
    parsed = parse_response("events", json.dumps(doc).encode())
    assert isinstance(parsed, EventsResponse)
    too_many = {**doc, "events": [{"id": "1795000000000-0000000001-deadbeef", "kind": "x", "time": 1}] * 17}
    with pytest.raises(ValueError, match="bad_response"):
        parse_response("events", json.dumps(too_many).encode())


# --- the whole chain ---------------------------------------------------------------------------


async def test_hook_to_discord_over_the_agent_link(tmp_path: Path, world: RemoteWorld) -> None:
    with socket_dir() as directory:
        # The hook runs on the machine, as Claude Code would run it.
        env = {"HOME": world.home, "TMUX_PANE": "%1"}
        read, write = os.pipe()
        os.write(write, json.dumps({"hook_event_name": "Stop", "session_id": SESSION, "cwd": "/w/fpgadc"}).encode())
        os.close(write)
        assert h.run_hook(env, read) == 0
        os.close(read)
        assert len(spool_names(world)) == 1

        projects = tmp_path / "projects"
        projects.mkdir()
        data = {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(directory / "state"),
            "projectRoots": [str(projects)],
            "remote": {
                "machines": [agent_machine("laptop", sessions=["fpga"])],
                "agents": agents_section(directory),
                "summary": {"enabled": False},
            },
            "events": {"enabled": True, "pollSeconds": 2},
            "discord": {"enabled": True, "coalesceSeconds": 0, "kinds": ["finished", "incident"]},
        }
        config = parse_config(data, home_dir="/nonexistent-home-dir")
        manager = await TaskManager.create(
            TaskManagerOptions(
                state_dir=config.state_dir,
                project_roots=config.project_roots,
                claude=config.claude,
                tasks=config.tasks,
                logger=LogCapture().logger,
            )
        )
        posts: list[dict[str, Any]] = []

        def webhook(request: httpx2.Request) -> httpx2.Response:
            posts.append(json.loads(request.content))
            return httpx2.Response(204)

        discord = DiscordNotifier(
            URL, config.discord, silent_logger, client=httpx2.AsyncClient(transport=httpx2.MockTransport(webhook))
        )
        tools = RemoteTools(
            RemoteToolsOptions(
                config=config,
                manager=manager,
                logger=LogCapture().logger,
                base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                claude_home=str(tmp_path / "hub-claude"),
                discord=discord,
            )
        )
        await tools.start()
        link = MachineLink(world, str(directory / "hub.sock"), "laptop", ["--sessions", "fpga", "--allow-events"])
        try:
            await wait_until(lambda: bool(posts), 20)
            assert "**Finished its turn** · on laptop · project `fpgadc`" in posts[0]["content"]
            assert "tmux `fpga`" in posts[0]["content"]
            await wait_until(lambda: not spool_names(world), 20)
            incidents, _ = tools.incident_log.read()
            assert [(i.entry, i.code, i.machine) for i in incidents] == [("event", "finished", "laptop")]
            assert tools.incidents_report(None, None, False).text == "No incidents today."
        finally:
            await tools.stop()
            link.stop()
            await discord.aclose()
            await manager.shutdown("test finished")


async def test_the_cli_lists_events_only_when_asked(real_tmp: Path) -> None:

    config = write_config(real_tmp)
    log = IncidentLog(str(real_tmp / "state"))
    log.write(event_entry(machine="laptop", code="finished", what="a session finished its turn", why="x", at_ms=T0))
    code, io = await run_cli("incidents", "list", "--config", config)
    assert (code, io.stdout) == (0, "No incidents.\n")
    code, io = await run_cli("incidents", "list", "--events", "--config", config)
    assert code == 0
    assert "finished" in io.stdout
    assert "session" in io.stdout


async def test_a_missing_webhook_is_an_incident_at_start(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    config = parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(tmp_path / "state"),
            "projectRoots": [str(projects)],
            "discord": {"enabled": True},
        },
        home_dir="/nonexistent-home-dir",
    )
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=LogCapture().logger,
        )
    )
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=manager,
            logger=LogCapture().logger,
            discord_problem="SWITCHBOARD_DISCORD_WEBHOOK_URL is not set",
        )
    )
    await tools.start()
    try:
        [incident] = [i for i in tools.incidents.open_incidents if i.component == "notify"]
        assert incident.code == "discord_not_configured"
        assert "is not set" in incident.why
    finally:
        await tools.stop()
        await manager.shutdown("test finished")


def test_a_symlinked_spool_is_refused_before_anything_is_deleted(world: RemoteWorld, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    victim = elsewhere / "1795000000000-0000000001-deadbeef.json"
    victim.write_text("{}")
    directory = Path(h.spool_dir({"HOME": world.home}))
    directory.parent.mkdir(parents=True, mode=0o700)
    directory.symlink_to(elsewhere)
    assert run(world, ["1795000000000-0000000001-deadbeef"])["error"] == "events_unsafe"
    assert victim.exists()
