"""`remote_update` and its `remote_send_confirm` (docs/SWITCHBOARD.md, "Self-update"): the read-back
of versions and signing key, the confirmation bound to the published bundle's bytes and to an
explicit yes to the risky part, the update over the real ssh transport (fake ssh running an
installed dispatcher copy, real ssh-keygen with temporary keys), the incident events and audit
lines of every attempt, and the notes in list_remote_sessions."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FakeClock, LogCapture, claude_config
from tests.remote.helpers import RemoteWorld, machine
from tests.remote.release_helpers import bundle, fingerprint, install_machine, needs_openssh, new_key, programs
from tests.remote.test_send_tools import OWNER, T0
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import release as r
from voice_coding_mcp.remote.audit import read_audit
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.protocol import ListResponse, VersionResponse
from voice_coding_mcp.remote.tools import EARLY, NEEDS_RISKY_OK, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import TransportResult
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

pytestmark = needs_openssh

FORCED = ["--sessions", "fpga,notes,work-*", "--allow-self-update"]


@dataclass
class Stub:
    """A machine that answers `version` as told and records what `update` got."""

    machine: MachineConfig
    version: str = "0.13.0"
    verbs: Sequence[str] = tuple(d.VERBS)
    allow: bool = True
    update: TransportResult = field(
        default_factory=lambda: TransportResult(ok=False, error="refused_by_policy", detail="bad_signature")
    )
    received: list[bytes] = field(default_factory=list)

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        if verb == "version":
            data = {"version": self.version, "verbs": list(self.verbs), "allowSelfUpdate": self.allow}
            return TransportResult(ok=True, data=VersionResponse.model_validate(data))
        if verb == "list":
            return TransportResult(ok=True, data=ListResponse.model_validate({"panes": []}))
        if verb == "update":
            self.received.append(stdin or b"")
            return self.update
        return TransportResult(ok=False, error="other")


@dataclass
class Ctx:
    root: Path
    world: RemoteWorld
    tools: RemoteTools
    config: Config
    clock: FakeClock
    key: Path
    manager: TaskManager
    home: Path

    async def prepare(self, name: str = "dev") -> tuple[str, bool]:
        answer = await self.tools.remote_update(name, OWNER)
        return answer.text, answer.is_error

    async def confirm(self, cid: str, risky_ok: bool = True) -> tuple[str, bool]:
        answer = await self.tools.remote_send_confirm(cid, risky_ok, OWNER)
        return answer.text, answer.is_error

    def later(self, seconds: float = 5) -> None:
        self.clock.t += int(seconds * 1000)

    def publish(self, version: str = "0.13.1") -> r.Release:
        data = bundle(self.key, self.root / "work", version=version)
        path = self.root / f"out-{version}.bundle"
        path.write_bytes(data)
        return r.publish(str(path), self.config.state_dir)

    def events(self) -> list[dict[str, Any]]:
        path = Path(self.config.state_dir) / "incidents.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


async def build(root: Path, stubs: dict[str, Any] | None = None) -> Ctx:
    world = RemoteWorld.create(root, forced=FORCED)
    key = new_key(root, "release")
    home = Path(world.home)
    dispatcher = install_machine(home, key)
    world.configure(dispatcherPath=str(dispatcher))
    projects = root / "projects"
    projects.mkdir(exist_ok=True)
    claude = claude_config(str(root / "record.jsonl"))
    config = parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(root / "state"),
            "projectRoots": [str(projects)],
            "claude": {"bin": claude.bin, "env": dict(claude.env)},
            "tasks": {"syncWaitSeconds": 30, "maxConcurrent": 1},
            "remote": {
                "machines": [
                    machine("dev", world=world),
                    machine("buildbox", world=world, host="example-host", user="bot", sessions=["api"]),
                    {"name": "hubself", "transport": "local", "sessions": ["x"]},
                ],
                "ssh": {"bin": world.ssh, "commandTimeoutSeconds": 20},
                "summary": {"enabled": False},
            },
        },
        home_dir="/nonexistent-home-dir",
    )
    os.makedirs(config.state_dir, exist_ok=True)
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=silent_logger,
            kill_grace_ms=200,
        )
    )
    clock = FakeClock(T0)
    pool = iter(["4172", "5283", "6394", "7405"])
    transports = None
    if stubs is not None:
        transports = {m.name: stubs.get(m.name) or Stub(m) for m in config.remote.machines}
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=manager,
            logger=LogCapture().logger,
            base_env={"PATH": os.environ["PATH"], "HOME": str(root)},
            claude_home=str(root / "hub-claude"),
            now=clock,
            new_confirmation_id=lambda: next(pool),
            transports=transports,
        )
    )
    return Ctx(root, world, tools, config, clock, key, manager, home)


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    built = await build(tmp_path)
    yield built
    await built.manager.shutdown("test finished")


@pytest.fixture
async def stubbed(tmp_path: Path) -> AsyncIterator[Ctx]:
    built = await build(tmp_path, stubs={})
    yield built
    await built.manager.shutdown("test finished")


def tamper(path: str) -> None:
    with open(path, "ab") as handle:
        handle.write(b"\n")


def stub(ctx: Ctx, name: str = "dev") -> Stub:
    transport = ctx.tools.transports[name]
    inner = getattr(transport, "inner", transport)
    assert isinstance(inner, Stub)
    return inner


async def test_update_over_ssh_reads_back_confirms_installs_and_records(ctx: Ctx) -> None:
    release = ctx.publish()
    text, error = await ctx.prepare()
    assert not error
    assert text == (
        f"Ready to update switchboard-remote on dev from {d.VERSION} to 0.13.1, signed by the release key "
        f"{fingerprint(ctx.key)}. Careful, this replaces the programs that run on dev. dev checks the signature "
        "with its own pinned key before it installs anything. Say yes, update it. "
        "(confirmation 4172, do not read this number aloud)"
    )
    assert release.fingerprint == fingerprint(ctx.key)
    ctx.later()
    text, error = await ctx.confirm("4172")
    assert (text, error) == (
        "Updated switchboard-remote on dev to 0.13.1. The next call to it runs the new version.",
        False,
    )
    installed = Path(ctx.home) / ".local" / "bin"
    assert (installed / "switchboard-remote").read_bytes() == programs("0.13.1")[d.UPDATE_PRODUCT]
    assert (installed / "switchboard-remote.prev").read_bytes() == r.source_files()[d.UPDATE_PRODUCT]
    events = ctx.events()
    assert [(e["machine"], e["component"], e["code"], e["entry"]) for e in events] == [
        ("dev", "update", "update_installed", "event")
    ]
    assert fingerprint(ctx.key)[:19] in events[0]["why"]
    audit = read_audit(ctx.config.state_dir)[0]
    assert audit[-1]["event"] == "remote_update"
    assert audit[-1]["outcome"] == "sent"
    assert audit[-1]["riskyOk"] is True
    assert audit[-1]["release"] == {
        "from": d.VERSION,
        "to": "0.13.1",
        "signer": release.fingerprint,
        "bundleSha256": release.sha256,
    }
    assert "textHmac" not in audit[-1]
    # The machine now reports the new version, and the release is not newer any more.
    text, error = await ctx.prepare()
    assert error
    assert "already 0.13.1" in text


async def test_the_confirmation_needs_an_explicit_yes_to_the_risky_part_and_a_later_turn(stubbed: Ctx) -> None:
    stubbed.publish()
    text, _ = await stubbed.prepare()
    assert "confirmation 4172" in text
    assert (await stubbed.confirm("4172"))[0] == EARLY
    stubbed.later()
    assert (await stubbed.confirm("4172", risky_ok=False))[0] == NEEDS_RISKY_OK
    assert stub(stubbed).received == []
    text, error = await stubbed.confirm("4172")
    assert error
    assert text == "Not updated: the signature did not check out against the release key pinned on dev."
    assert len(stub(stubbed).received) == 1
    assert [e["code"] for e in stubbed.events()] == ["update_refused"]
    assert "bad_signature" in stubbed.events()[0]["why"]
    # Single use.
    assert (await stubbed.confirm("4172"))[1]


async def test_only_the_bundle_that_was_read_back_is_sent(stubbed: Ctx) -> None:
    release = stubbed.publish()
    await stubbed.prepare()
    stubbed.later()
    tamper(release.path)
    text, error = await stubbed.confirm("4172")
    assert error
    assert text == "Not updated: the published release changed since I read it to you. Ask me again."
    assert stub(stubbed).received == []
    assert [e["code"] for e in stubbed.events()] == ["update_refused"]


async def test_what_the_hub_refuses_before_a_read_back(stubbed: Ctx) -> None:
    text, error = await stubbed.prepare()
    assert error
    assert "No release is published" in text
    stubbed.publish()
    old = stub(stubbed)
    old.version, old.verbs = "0.6.0", ("version", "list", "capture")
    text, _ = await stubbed.prepare()
    assert text.startswith("switchboard-remote on dev is version 0.6.0, too old to update itself. Install 0.13.1")
    old.version, old.verbs, old.allow = d.VERSION, tuple(d.VERBS), False
    assert (await stubbed.prepare())[
        0
    ] == "Self-update is off on dev: its switchboard-remote runs without --allow-self-update."
    old.allow, old.version = True, "0.13.1"
    assert "already 0.13.1" in (await stubbed.prepare())[0]
    text, error = await stubbed.prepare("hubself")
    assert error
    assert "updates with the hub" in text
    assert old.received == []


async def test_list_remote_sessions_mentions_an_older_dispatcher(stubbed: Ctx) -> None:
    stubbed.publish()
    before = (await stubbed.tools.list_remote_sessions("dev")).text
    assert "is published" not in before
    await stubbed.prepare()
    after = (await stubbed.tools.list_remote_sessions("dev")).text
    assert f"Its switchboard-remote is {d.VERSION}; release 0.13.1 is published, ask me to update dev." in after


async def test_an_unknown_outcome_is_recorded_as_such(stubbed: Ctx) -> None:
    stubbed.publish()
    stub(stubbed).update = TransportResult(ok=False, error="timeout")
    await stubbed.prepare()
    stubbed.later()
    text, error = await stubbed.confirm("4172")
    assert error
    assert text.startswith("I do not know whether dev installed the update")
    assert [e["code"] for e in stubbed.events() if e["component"] == "update"] == ["update_unknown"]
    assert read_audit(stubbed.config.state_dir)[0][-1]["outcome"] == "unknown"
