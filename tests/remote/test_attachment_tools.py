"""`remote_attachments` and `remote_attachment` end to end: RemoteTools over the real ssh transport
(fake ssh, real dispatcher), over the agent transport (real relay and serve mode), with a lying
dispatcher behind a fake transport, and through the MCP server as image, audio and embedded
resource content."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import AudioContent, BlobResourceContents, EmbeddedResource, ImageContent, TextContent

from tests.helpers import FakeClock, claude_config
from tests.remote.agent_helpers import MachineLink, agent_machine, agents_section, socket_dir, wait_until
from tests.remote.helpers import RemoteWorld, machine
from tests.remote.media_fixtures import GPS, SVG, exif_segment, jpeg, mp3, mp4, png, write_sparse_mkv
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.mcp import McpServerOptions, create_mcp_server
from voice_coding_mcp.remote import attachments as att
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import tools as tools_module
from voice_coding_mcp.remote.audit import read_audit
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.protocol import ATTACHMENT_CAPS as HUB_CAPS
from voice_coding_mcp.remote.protocol import AttachmentResponse, AttachmentsResponse
from voice_coding_mcp.remote.tools import Caller, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import Transport, TransportResult
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions

T0 = 1_764_160_000_000
OWNER = Caller(fingerprint="len=36 sha256=0a0b0c0d", name="Claude")
FORCED = ["--sessions", "fpga", "--allow-attachments"]


def put(home: str, name: str, data: bytes, age_seconds: int) -> None:
    box = Path(home) / d.OUTBOX_DIR
    box.mkdir(parents=True, exist_ok=True)
    path = box / name
    path.write_bytes(data)
    when = T0 // 1000 - age_seconds
    os.utime(path, (when, when))


def make_config(tmp: Path, machines: list[dict[str, Any]], **remote: Any) -> Config:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    claude = claude_config(str(tmp / "record.jsonl"))
    return parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(tmp / "state"),
            "projectRoots": [str(projects)],
            "claude": {"bin": claude.bin, "env": dict(claude.env)},
            "tasks": {"syncWaitSeconds": 20, "maxConcurrent": 1},
            "remote": {"machines": machines, "summary": {"enabled": False}, **remote},
        },
        home_dir="/nonexistent-home-dir",
    )


async def make_manager(config: Config) -> TaskManager:
    return await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=silent_logger,
            kill_grace_ms=200,
        )
    )


@dataclass
class Ctx:
    world: RemoteWorld
    tools: RemoteTools
    config: Config
    manager: TaskManager


@pytest.fixture
async def ssh(tmp_path: Path) -> AsyncIterator[Ctx]:
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    config = make_config(
        tmp_path,
        [
            machine("dev", world=world, allowAttachments=True, sessions=["fpga"]),
            machine("other", world=world, sessions=["fpga"]),
        ],
        ssh={"bin": world.ssh, "commandTimeoutSeconds": 8},
    )
    manager = await make_manager(config)
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=manager,
            logger=silent_logger,
            base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            now=FakeClock(T0),
        )
    )
    try:
        yield Ctx(world=world, tools=tools, config=config, manager=manager)
    finally:
        await manager.shutdown("test over")


async def test_list_says_how_many_and_the_newest(ssh: Ctx) -> None:
    put(ssh.world.home, "shot.png", png(), 120)
    put(ssh.world.home, "take.mp3", mp3(), 3600)
    put(ssh.world.home, "page.html", b"<html></html>", 60)
    answer = await ssh.tools.remote_attachments("dev")
    assert not answer.is_error
    assert answer.text == (
        "dev has 2 media files in its outbox; the newest is an image from 2 minutes ago. Newest first, with the "
        'file names the machine gives: an image "shot.png", 2 minutes ago, 1 KB; audio "take.mp3", 1 hour ago, '
        "1 KB. 1 other file there is not shown: not an image, audio or video file I can send, or too large."
    )
    only_audio = await ssh.tools.remote_attachments("dev", "audio")
    assert only_audio.text.startswith("dev has 1 audio file in its outbox; the newest is audio from 1 hour ago.")
    assert (await ssh.tools.remote_attachments("dev", "video")).text.startswith("The outbox on dev has no videos.")


async def test_no_outbox_and_empty_outbox(ssh: Ctx) -> None:
    assert (await ssh.tools.remote_attachments("dev")).text == "dev has no outbox folder, so there is nothing to show."
    (Path(ssh.world.home) / d.OUTBOX_DIR).mkdir(parents=True)
    assert (await ssh.tools.remote_attachments("dev")).text == "The outbox on dev has no images, audio or video."
    answer = await ssh.tools.remote_attachment("dev", None, None, OWNER)
    assert answer.is_error
    assert answer.text == "The outbox on dev has no media file."


async def test_hub_flag_is_needed(ssh: Ctx) -> None:
    put(ssh.world.home, "shot.png", png(), 120)
    for answer in (
        await ssh.tools.remote_attachments("other"),
        await ssh.tools.remote_attachment("other", "shot.png", None, OWNER),
    ):
        assert answer.is_error
        assert answer.text == "Attachments are off for other in the hub's config (allowAttachments)."
    # Nothing went to the machine.
    assert ssh.world.ssh_calls() == []


async def test_dispatcher_flag_is_needed(ssh: Ctx) -> None:
    ssh.world.configure(forced=["--sessions", "fpga"])
    answer = await ssh.tools.remote_attachments("dev")
    assert answer.is_error
    assert answer.text == "switchboard-remote on dev does not allow attachments; it needs --allow-attachments."


async def test_fetch_returns_the_cleaned_image_and_audits_it(ssh: Ctx) -> None:
    put(ssh.world.home, "shot.jpg", jpeg(exif_segment()), 300)
    put(ssh.world.home, "older.png", png(), 900)
    answer = await ssh.tools.remote_attachment("dev", "the shot", None, OWNER)
    assert not answer.is_error, answer.text
    assert answer.media is not None
    assert (answer.media.kind, answer.media.mime, answer.media.name) == ("image", "image/jpeg", "shot.jpg")
    assert answer.media.data == jpeg()
    assert GPS not in answer.media.data
    assert answer.text == 'Image "shot.jpg" from dev, 1 KB, from 5 minutes ago. The file name comes from the machine.'
    newest = await ssh.tools.remote_attachment("dev", None, None, OWNER)
    assert newest.media is not None
    assert newest.media.name == "shot.jpg"
    png_only = await ssh.tools.remote_attachment("dev", None, "image", OWNER)
    assert png_only.media is not None
    exact = await ssh.tools.remote_attachment("dev", "older.png", None, OWNER)
    assert exact.media is not None
    assert exact.media.name == "older.png"
    rows, bad = read_audit(ssh.config.state_dir)
    assert bad == 0
    assert [(r["event"], r["machine"], r["kind"], r["outcome"]) for r in rows] == [
        ("remote_attachment", "dev", "image", "sent"),
    ] * 4
    assert rows[0]["bytes"] == len(jpeg())
    assert "shot.jpg" not in str(rows)
    assert len(rows[0]["nameHmac"]) == 64
    assert rows[0]["client"] == OWNER.fingerprint


async def test_refusals_are_spoken(ssh: Ctx) -> None:
    put(ssh.world.home, "drawing.png", SVG, 60)
    answer = await ssh.tools.remote_attachment("dev", "drawing", None, OWNER)
    assert answer.is_error
    # Not listed at all (not media), so nothing of that name is found.
    assert answer.text == 'I found no media file called "drawing" in the outbox on dev.'
    put(ssh.world.home, "cut.png", png()[:-4], 30)
    answer = await ssh.tools.remote_attachment("dev", "cut.png", None, OWNER)
    assert answer.text == '"cut.png" on dev is not a whole image, audio or video file, so I did not send it.'
    rows, _ = read_audit(ssh.config.state_dir)
    assert rows[-1]["outcome"] == "refused:not_media"


async def test_rate_limit(ssh: Ctx) -> None:
    put(ssh.world.home, "shot.png", png(), 60)
    for _ in range(att.MAX_FETCHES_PER_WINDOW):
        assert (await ssh.tools.remote_attachment("dev", "shot.png", None, OWNER)).media is not None
    answer = await ssh.tools.remote_attachment("dev", "shot.png", None, OWNER)
    assert answer.is_error
    assert answer.text == "That is a lot of attachments in a short time. Try again in a few minutes."


# --- a lying dispatcher ------------------------------------------------------------------------


class LyingTransport:
    """Lists one file and answers `attachment` with whatever the test says."""

    def __init__(self, machine: MachineConfig, answer: AttachmentResponse, listed: dict[str, Any]) -> None:
        self.machine = machine
        self.answer = answer
        self.listed = listed

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        if verb == "attachments":
            return TransportResult(ok=True, data=AttachmentsResponse.model_validate({"attachments": [self.listed]}))
        return TransportResult(ok=True, data=self.answer)


@pytest.mark.parametrize(
    ("listed", "answer"),
    [
        # SVG claimed to be a PNG.
        (("shot.png", "image", "image/png"), ("shot.png", "image", "image/png", SVG)),
        # A PNG with a ZIP behind it.
        (("shot.png", "image", "image/png"), ("shot.png", "image", "image/png", png() + b"PK\x03\x04")),
        # A real PNG, but labelled as audio.
        (("shot.png", "image", "image/png"), ("shot.png", "audio", "audio/mpeg", png())),
        # A real PNG under another name than the one asked for.
        (("shot.png", "image", "image/png"), ("other.png", "image", "image/png", png())),
        # Over the hub's cap for images.
        (("shot.png", "image", "image/png"), ("shot.png", "image", "image/png", png() + bytes(4_000_000))),
    ],
)
async def test_the_hub_checks_everything_again(
    tmp_path: Path, listed: tuple[str, ...], answer: tuple[Any, ...]
) -> None:
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    try:
        info = {"name": listed[0], "kind": listed[1], "mime": listed[2], "size": 10, "mtime": T0}
        reply = AttachmentResponse(name=answer[0], kind=answer[1], mime=answer[2], mtime=T0, data=answer[3])
        transports: dict[str, Transport] = {"dev": LyingTransport(config.remote.machines[0], reply, info)}
        tools = RemoteTools(
            RemoteToolsOptions(config=config, manager=manager, logger=silent_logger, transports=transports)
        )
        result = await tools.remote_attachment("dev", "shot.png", None, OWNER)
        assert result.is_error
        assert result.media is None
        assert result.text == (
            'dev sent something for "shot.png" that is not the image, audio or video file it listed, so I did not '
            "pass it on."
        )
        rows, _ = read_audit(config.state_dir)
        assert rows[-1]["outcome"] == "rejected"
    finally:
        await manager.shutdown("test over")


class SlowTransport(LyingTransport):
    """Like LyingTransport, but `attachment` waits until the test releases it."""

    def __init__(self, machine: MachineConfig, answer: AttachmentResponse, listed: dict[str, Any]) -> None:
        super().__init__(machine, answer, listed)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        if verb == "attachment":
            self.started.set()
            await self.release.wait()
        return await super().call(verb, args, timeout_seconds=timeout_seconds, stdin=stdin)


async def lying_tools(tmp_path: Path, transport_type: type[LyingTransport], data: bytes) -> tuple[RemoteTools, Any]:
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    info = {"name": "shot.png", "kind": "image", "mime": "image/png", "size": 10, "mtime": T0}
    reply = AttachmentResponse(name="shot.png", kind="image", mime="image/png", mtime=T0, data=data)
    transport = transport_type(config.remote.machines[0], reply, info)
    transports: dict[str, Transport] = {"dev": transport}
    tools = RemoteTools(RemoteToolsOptions(config=config, manager=manager, logger=silent_logger, transports=transports))
    return tools, (manager, transport)


async def test_rejected_fetches_count_against_the_limits(tmp_path: Path) -> None:
    # A lying machine that answers every fetch with junk does not get unlimited tries.
    tools, (manager, _) = await lying_tools(tmp_path, LyingTransport, SVG)
    try:
        for _ in range(att.MAX_FETCHES_PER_WINDOW):
            answer = await tools.remote_attachment("dev", "shot.png", None, OWNER)
            assert answer.is_error
            assert answer.media is None
        answer = await tools.remote_attachment("dev", "shot.png", None, OWNER)
        assert answer.text == "That is a lot of attachments in a short time. Try again in a few minutes."
        # The bytes the machine sent count, not the size it listed.
        limiter = tools.fetches
        assert not limiter.fits(att.MAX_BYTES_PER_WINDOW - att.MAX_FETCHES_PER_WINDOW * len(SVG) + 1)
    finally:
        await manager.shutdown("test over")


async def test_one_fetch_at_a_time(tmp_path: Path) -> None:
    tools, (manager, transport) = await lying_tools(tmp_path, SlowTransport, png())
    try:
        first = asyncio.create_task(tools.remote_attachment("dev", "shot.png", None, OWNER))
        await asyncio.wait_for(transport.started.wait(), 5)
        busy = await tools.remote_attachment("dev", "shot.png", None, OWNER)
        assert busy.is_error
        assert busy.text == att.BUSY_WORDS
        transport.release.set()
        assert (await first).media is not None
        # The slot is free again.
        assert (await tools.remote_attachment("dev", "shot.png", None, OWNER)).media is not None
    finally:
        await manager.shutdown("test over")


async def test_the_hub_check_runs_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A slow check (a crafted file) must not stop the event loop: other coroutines keep running.
    def slow_check(name: str, data: bytes) -> tuple[str, str, bytes | bytearray]:
        time.sleep(0.5)
        return d.media_check(name, data)

    monkeypatch.setattr(tools_module, "media_check", slow_check)
    tools, (manager, _) = await lying_tools(tmp_path, LyingTransport, png())
    try:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        running = asyncio.create_task(ticker())
        answer = await tools.remote_attachment("dev", "shot.png", None, OWNER)
        running.cancel()
        assert answer.media is not None
        assert ticks >= 10
    finally:
        await manager.shutdown("test over")


# --- MCP content --------------------------------------------------------------------------------


async def test_mcp_returns_image_audio_and_video_content(ssh: Ctx) -> None:
    put(ssh.world.home, "shot.png", png(), 60)
    put(ssh.world.home, "take.mp3", mp3(), 120)
    put(ssh.world.home, "clip.mp4", mp4(), 180)
    server = create_mcp_server(
        McpServerOptions(manager=ssh.manager, config=ssh.config, logger=silent_logger, remote_tools=ssh.tools)
    )
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("remote_attachments", "remote_attachment"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
        assert sorted(tools["remote_attachment"].input_schema["properties"]) == ["kind", "machine", "name"]
        listed = await client.call_tool("remote_attachments", {"machine": "dev"})
        assert isinstance(listed.content[0], TextContent)
        assert listed.content[0].text.startswith("dev has 3 media files")

        image = await client.call_tool("remote_attachment", {"machine": "dev", "name": "shot.png"})
        assert image.is_error is not True
        assert isinstance(image.content[0], TextContent)
        assert isinstance(image.content[1], ImageContent)
        assert image.content[1].mime_type == "image/png"
        assert base64.b64decode(image.content[1].data) == png()

        audio = await client.call_tool("remote_attachment", {"machine": "dev", "kind": "audio"})
        assert isinstance(audio.content[1], AudioContent)
        assert audio.content[1].mime_type == "audio/mpeg"
        assert base64.b64decode(audio.content[1].data) == mp3()

        video = await client.call_tool("remote_attachment", {"machine": "dev", "name": "clip"})
        assert isinstance(video.content[1], EmbeddedResource)
        resource = video.content[1].resource
        assert isinstance(resource, BlobResourceContents)
        assert resource.mime_type == "video/mp4"
        assert str(resource.uri) == "attachment://dev/clip.mp4"
        assert len(base64.b64decode(resource.blob)) == len(mp4())

        refused = await client.call_tool("remote_attachment", {"machine": "other"})
        assert refused.is_error is True
        assert len(refused.content) == 1


async def test_tools_are_not_offered_without_any_machine_that_allows_them(tmp_path: Path) -> None:
    config = make_config(tmp_path, [machine("dev")])
    manager = await make_manager(config)
    try:
        server = create_mcp_server(McpServerOptions(manager=manager, config=config, logger=silent_logger))
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
        assert "remote_attachments" not in names
        assert "remote_attachment" not in names
    finally:
        await manager.shutdown("test over")


# --- the agent transport ------------------------------------------------------------------------


async def test_a_large_video_over_the_agent_transport(tmp_path: Path) -> None:
    with socket_dir() as directory:
        world = RemoteWorld.create(tmp_path)
        video = mp4(mdat=os.urandom(6 * 1024 * 1024))
        put(world.home, "clip.mp4", video, 60)
        put(world.home, "shot.png", png(), 30)
        config = make_config(
            tmp_path,
            [agent_machine("laptop", allowAttachments=True, allowUsage=False)],
            agents=agents_section(directory),
        )
        manager = await make_manager(config)
        tools = RemoteTools(
            RemoteToolsOptions(
                config=config,
                manager=manager,
                logger=silent_logger,
                base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                now=FakeClock(T0),
            )
        )
        await tools.start()
        link = MachineLink(world, str(directory / "hub.sock"), "laptop", FORCED)
        try:
            assert tools.agents is not None
            await wait_until(lambda: tools.agents is not None and tools.agents.live("laptop") is not None)
            conn = tools.agents.live("laptop")
            assert conn is not None
            assert conn.hello.allow_attachments is True
            assert tools.agents.disagreements("laptop", conn.hello) == []
            listed = await tools.remote_attachments("laptop")
            assert listed.text.startswith("laptop has 2 media files in its outbox; the newest is an image")
            answer = await tools.remote_attachment("laptop", "clip.mp4", None, OWNER)
            assert not answer.is_error, answer.text
            assert answer.media is not None
            assert answer.media.data == d.media_check("clip.mp4", video)[2]
            assert answer.text.startswith('Video "clip.mp4" from laptop, 6.3 MB')
        finally:
            await tools.stop()
            link.stop()
            await manager.shutdown("test over")


async def test_a_250_mb_video_over_the_agent_transport_gets_a_link(tmp_path: Path) -> None:
    """The largest video there may be, through the real relay and serve-mode dispatcher: about 5 s
    on a desktop machine (the answer may first say it is still coming, then the next call has the
    link). Slow by this suite's standards, but it is the only test of 250 chunks end to end."""
    with socket_dir() as directory:
        world = RemoteWorld.create(tmp_path)
        cap = d.ATTACHMENT_CAPS["video"]
        box = Path(world.home) / d.OUTBOX_DIR
        box.mkdir(parents=True)
        write_sparse_mkv(str(box / "take.mkv"), cap)
        config = make_config(
            tmp_path,
            [agent_machine("laptop", allowAttachments=True, allowUsage=False)],
            agents=agents_section(directory),
        )
        manager = await make_manager(config)
        tools = RemoteTools(
            RemoteToolsOptions(
                config=config,
                manager=manager,
                logger=silent_logger,
                base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                now=FakeClock(T0),
            )
        )
        await tools.start()
        link = MachineLink(world, str(directory / "hub.sock"), "laptop", FORCED)
        try:
            await wait_until(lambda: tools.agents is not None and tools.agents.live("laptop") is not None)
            answer = await tools.remote_attachment("laptop", "take.mkv", None, OWNER)
            for _ in range(10):
                if "still" not in answer.text and "a while to arrive" not in answer.text:
                    break
                answer = await tools.remote_attachment("laptop", "take.mkv", None, OWNER)
            assert not answer.is_error, answer.text
            assert "Link (do not read it aloud): https://vcm.example.com/attachments/" in answer.text
            token = answer.text.rsplit("/", 1)[1]
            held = tools.links.get(token)
            assert held is not None
            assert len(held.data) == cap
            assert b"OBS Studio" not in held.data[:4096]
            rows, _ = read_audit(config.state_dir)
            assert [r["outcome"] for r in rows if r["event"] == "remote_attachment"] == ["link"]
        finally:
            await tools.stop()
            link.stop()
            await manager.shutdown("test over")


async def test_agent_hello_disagreement_is_reported(tmp_path: Path) -> None:
    with socket_dir() as directory:
        world = RemoteWorld.create(tmp_path)
        config = make_config(
            tmp_path,
            [agent_machine("laptop", allowAttachments=True, allowUsage=False)],
            agents=agents_section(directory),
        )
        manager = await make_manager(config)
        tools = RemoteTools(RemoteToolsOptions(config=config, manager=manager, logger=silent_logger))
        await tools.start()
        link = MachineLink(world, str(directory / "hub.sock"), "laptop", ["--sessions", "fpga"])
        try:
            assert tools.agents is not None
            await wait_until(lambda: tools.agents is not None and tools.agents.live("laptop") is not None)
            conn = tools.agents.live("laptop")
            assert conn is not None
            assert conn.hello.allow_attachments is False
            assert tools.agents.disagreements("laptop", conn.hello) == [
                "allowAttachments is on on the hub, --allow-attachments is not the same"
            ]
            answer = await tools.remote_attachments("laptop")
            assert answer.text == (
                "switchboard-remote on laptop does not allow attachments; it needs --allow-attachments."
            )
        finally:
            await tools.stop()
            link.stop()
            await manager.shutdown("test over")
        await asyncio.sleep(0)


async def test_the_local_transport_passes_the_opt_in(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    put(world.home, "shot.png", png(), 60)
    config = make_config(
        tmp_path, [{"name": "dev", "transport": "local", "sessions": ["fpga"], "allowAttachments": True}]
    )
    manager = await make_manager(config)
    try:
        tools = RemoteTools(
            RemoteToolsOptions(
                config=config,
                manager=manager,
                logger=silent_logger,
                base_env={"PATH": os.environ["PATH"], **world.tmux_env()},
                now=FakeClock(T0),
            )
        )
        answer = await tools.remote_attachment("dev", None, None, OWNER)
        assert answer.media is not None
        assert answer.media.data == png()
    finally:
        await manager.shutdown("test over")


def test_the_docs_state_the_real_outbox_and_caps() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract = (repo / "docs" / "SWITCHBOARD.md").read_text(encoding="utf-8")
    assert f"`~/{d.OUTBOX_DIR}`" in contract
    assert f"images {d.ATTACHMENT_CAPS['image']:,} bytes" in contract
    assert f"audio {d.ATTACHMENT_CAPS['audio'] // (1024 * 1024)} MiB" in contract
    assert f"video {d.ATTACHMENT_CAPS['video']:,} bytes" in contract
    for _, mime in d.MEDIA_FORMATS.values():
        assert f"`{mime}`" in contract, mime
    reference = (repo / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    assert "`allowAttachments` (`false`" in reference
    assert "--allow-attachments" in reference
    for guide in ("SETUP-WSL.md", "SETUP-AGENTS.md"):
        text = (repo / "docs" / guide).read_text(encoding="utf-8")
        assert "--allow-attachments" in text, guide
        assert d.OUTBOX_DIR in text, guide
    assert HUB_CAPS == d.ATTACHMENT_CAPS
