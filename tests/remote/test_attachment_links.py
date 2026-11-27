"""Large videos: the one-time link (`remote/links.py`), the threshold between an embedded video and
a link, the fetch that continues after its tool call, and `GET /attachments/<token>` on the real
HTTP app (headers, ranges, expiry, misses, rate limit, the request log and the audit lines)."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.app_helpers import AppCtx, start_app
from tests.helpers import FakeClock, LogCapture
from tests.remote.helpers import machine
from tests.remote.media_fixtures import mkv_sized
from tests.remote.test_attachment_tools import OWNER, T0, make_config, make_manager
from voice_coding_mcp.auth.router import RateLimitSettings
from voice_coding_mcp.config import Config
from voice_coding_mcp.log import fingerprint, silent_logger
from voice_coding_mcp.remote import attachments as att
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import links
from voice_coding_mcp.remote import tools as tools_module
from voice_coding_mcp.remote.audit import read_audit
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.protocol import AttachmentResponse, AttachmentsResponse
from voice_coding_mcp.remote.tools import RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import Transport, TransportResult
from voice_coding_mcp.tasks.manager import TaskManager

LINK_RE = re.compile(r"Link \(do not read it aloud\): (https?://[^/ ]+)/attachments/([A-Za-z0-9_-]+)$")
BIG = att.EMBED_MAX_BYTES + 1


# --- the store ----------------------------------------------------------------------------------


def add(store: links.LinkStore, data: bytes = b"x" * 10, name: str = "big.mkv", size: int = 10) -> links.Link:
    return store.add(machine="dev", name=name, kind="video", mime="video/x-matroska", data=data, size=size, mtime=T0)


def test_tokens_are_256_random_bits_in_base64url() -> None:
    store = links.LinkStore(now=FakeClock(T0))
    tokens = [add(store).token for _ in range(200)]
    assert len(set(tokens)) == 200
    for token in tokens:
        assert links.TOKEN_RE.fullmatch(token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32
    # Every bit position takes both values across 200 tokens (a constant byte would show here).
    bits = [int.from_bytes(base64.urlsafe_b64decode(t + "="), "big") for t in tokens]
    assert all(any(b >> i & 1 for b in bits) and not all(b >> i & 1 for b in bits) for i in range(256))


def test_wrong_malformed_and_expired_tokens_find_nothing() -> None:
    clock = FakeClock(T0)
    ended: list[tuple[str, str]] = []
    store = links.LinkStore(now=clock, on_end=lambda link, reason: ended.append((link.name, reason)))
    link = add(store)
    assert store.get(link.token) is link
    other = "A" * 43 if link.token != "A" * 43 else "B" * 43
    for wrong in ("", other, link.token[:-1], link.token + "A", link.token.upper(), "../" + link.token[3:]):
        if wrong != link.token:
            assert store.get(wrong) is None
    clock.t += links.LINK_TTL_MS - 1
    assert store.get(link.token) is link
    clock.t += 1
    assert store.get(link.token) is None
    assert ended == [("big.mkv", "expired")]
    assert link.data == b""
    assert len(store) == 0


def test_the_memory_cap_drops_the_oldest_links() -> None:
    ended: list[tuple[str, str]] = []
    store = links.LinkStore(now=FakeClock(T0), max_bytes=100, on_end=lambda link, why: ended.append((link.name, why)))
    first = add(store, b"a" * 60, "one.mkv")
    second = add(store, b"b" * 40, "two.mkv")
    assert store.held_bytes == 100
    third = add(store, b"c" * 30, "three.mkv")
    assert ended == [("one.mkv", "evicted")]
    assert store.get(first.token) is None
    assert store.get(second.token) is second
    assert store.get(third.token) is third
    assert 2 * d.ATTACHMENT_CAPS["video"] <= links.MAX_LINK_BYTES


def test_a_link_serves_at_most_eight_times_its_size() -> None:
    ended: list[str] = []
    store = links.LinkStore(now=FakeClock(T0), on_end=lambda _link, why: ended.append(why))
    link = add(store, b"x" * 10)
    assert store.served(link, 80)
    assert not store.served(link, 1)
    assert ended == ["served_limit"]
    assert store.get(link.token) is None


def test_the_same_file_finds_its_link() -> None:
    store = links.LinkStore(now=FakeClock(T0))
    link = add(store, size=10)
    assert store.find("dev", "big.mkv", 10, T0) is link
    assert store.find("dev", "big.mkv", 11, T0) is None
    assert store.find("dev", "big.mkv", 10, T0 + 1) is None
    assert store.find("other", "big.mkv", 10, T0) is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-10", (990, 999)),
        ("bytes=-5000", (0, 999)),
        ("bytes=990-5000", (990, 999)),
        ("BYTES = 5-5", (5, 5)),
        ("bytes=1000-", "unsatisfiable"),
        ("bytes=-0", "unsatisfiable"),
        ("bytes=0-1,5-6", None),
        ("bytes=5-1", None),
        ("bytes=a-b", None),
        ("items=0-1", None),
        ("bytes=-", None),
        ("bytes=" + "9" * 30 + "-", None),
    ],
)
def test_range_headers(header: str | None, expected: object) -> None:
    assert links.parse_range(header, 1000) == expected


# --- the tools ----------------------------------------------------------------------------------


class VideoTransport:
    """Lists one video and answers `attachment` with it; `hold` keeps the fetch waiting."""

    def __init__(self, machine: MachineConfig, name: str, data: bytes, *, listed: int | None = None) -> None:
        self.machine = machine
        self.name = name
        self.data = data
        self.listed = len(data) if listed is None else listed
        self.fetches = 0
        self.started = asyncio.Event()
        self.hold: asyncio.Event | None = None

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        if verb == "attachments":
            info = {"name": self.name, "kind": "video", "mime": "video/x-matroska", "size": self.listed, "mtime": T0}
            return TransportResult(ok=True, data=AttachmentsResponse.model_validate({"attachments": [info]}))
        self.fetches += 1
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        reply = AttachmentResponse(name=self.name, kind="video", mime="video/x-matroska", mtime=T0, data=self.data)
        return TransportResult(ok=True, data=reply)


def video_tools(
    config: Config, manager: TaskManager, data: bytes, *, listed: int | None = None, clock: FakeClock | None = None
) -> tuple[RemoteTools, VideoTransport]:
    transport = VideoTransport(config.remote.machines[0], "big.mkv", data, listed=listed)
    transports: dict[str, Transport] = {"dev": transport}
    tools = RemoteTools(
        RemoteToolsOptions(
            config=config, manager=manager, logger=silent_logger, transports=transports, now=clock or FakeClock(T0)
        )
    )
    return tools, transport


@dataclass
class Tools:
    tools: RemoteTools
    transport: VideoTransport
    config: Config


@pytest.fixture
async def big(tmp_path: Path) -> AsyncIterator[Tools]:
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    tools, transport = video_tools(config, manager, mkv_sized(BIG))
    try:
        yield Tools(tools, transport, config)
    finally:
        await tools.stop()
        await manager.shutdown("test over")


async def test_a_video_over_16_mib_gets_a_link(big: Tools) -> None:
    answer = await big.tools.remote_attachment("dev", None, "video", OWNER)
    assert not answer.is_error, answer.text
    assert answer.media is None
    assert answer.text.startswith(
        'Video "big.mkv" from dev, 16.8 MB, from just now. It is too large to play inside this answer, so I put a '
        "link in the chat, valid for 10 minutes."
    )
    found = LINK_RE.search(answer.text)
    assert found is not None
    assert found.group(1) == "https://vcm.example.com"
    token = found.group(2)
    link = big.tools.links.get(token)
    assert link is not None
    assert link.data == d.media_check("big.mkv", big.transport.data)[2]
    rows, _ = read_audit(big.config.state_dir)
    assert [(r["event"], r["outcome"]) for r in rows] == [("attachment_link", "created"), ("remote_attachment", "link")]
    assert rows[0]["link"] == fingerprint(token)
    assert token not in str(rows)
    assert rows[1]["bytes"] == BIG
    # The link counted as the fetch.
    assert not big.tools.fetches.fits(att.MAX_BYTES_PER_WINDOW - BIG + 1)


async def test_asking_again_hands_out_the_same_link(big: Tools) -> None:
    first = await big.tools.remote_attachment("dev", None, "video", OWNER)
    again = await big.tools.remote_attachment("dev", "big", None, OWNER)
    assert LINK_RE.search(first.text) is not None
    assert first.text.rsplit(" ", 1)[1] == again.text.rsplit(" ", 1)[1]
    assert big.transport.fetches == 1
    rows, _ = read_audit(big.config.state_dir)
    assert rows[-1]["outcome"] == "link_again"
    assert rows[-1]["bytes"] == 0


@pytest.mark.parametrize(("size", "embedded"), [(att.EMBED_MAX_BYTES, True), (BIG, False)])
async def test_the_threshold_between_embedded_and_a_link(tmp_path: Path, size: int, embedded: bool) -> None:
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    tools, _ = video_tools(config, manager, mkv_sized(size))
    try:
        answer = await tools.remote_attachment("dev", None, None, OWNER)
        assert (answer.media is not None) is embedded
        assert (LINK_RE.search(answer.text) is not None) is not embedded
    finally:
        await tools.stop()
        await manager.shutdown("test over")


async def test_a_video_listed_small_that_arrives_large_gets_a_link(tmp_path: Path) -> None:
    # The file grew between the listing and the fetch: never more than 16 MiB inside a tool result.
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    tools, _ = video_tools(config, manager, mkv_sized(BIG), listed=1000)
    try:
        answer = await tools.remote_attachment("dev", None, None, OWNER)
        assert answer.media is None
        assert LINK_RE.search(answer.text) is not None
    finally:
        await tools.stop()
        await manager.shutdown("test over")


async def test_a_slow_large_video_arrives_after_its_call(big: Tools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_module, "MIN_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(tools_module, "DEADLINE_MARGIN_SECONDS", 19.8)
    big.transport.hold = asyncio.Event()
    first = await big.tools.remote_attachment("dev", None, "video", OWNER)
    assert not first.is_error
    assert first.text == (
        'The video "big.mkv" from dev is 16.8 MB, so it takes a while to arrive and be checked. Ask me for it '
        "again in a moment and I will put a link in the chat."
    )
    # Still on its way: the same request waits for it again; another file is busy.
    assert (await big.tools.remote_attachment("dev", "big.mkv", None, OWNER)).text == first.text
    busy = await big.tools.remote_attachment("dev", None, "image", OWNER)
    assert busy.text == att.BUSY_WORDS
    big.transport.hold.set()
    ready = await big.tools.remote_attachment("dev", None, None, OWNER)
    assert LINK_RE.search(ready.text) is not None
    assert big.transport.fetches == 1
    # The slot is free again.
    assert big.tools._fetching == 0


async def test_a_large_video_that_fails_the_check_gets_no_link(tmp_path: Path) -> None:
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    tools, _ = video_tools(config, manager, mkv_sized(BIG) + b"PK\x03\x04")
    try:
        answer = await tools.remote_attachment("dev", None, None, OWNER)
        assert answer.is_error
        assert "not the image, audio or video file it listed" in answer.text
        assert len(tools.links) == 0
        rows, _ = read_audit(config.state_dir)
        assert [r["outcome"] for r in rows] == ["rejected"]
    finally:
        await tools.stop()
        await manager.shutdown("test over")


# --- GET /attachments/<token> -----------------------------------------------------------------


@dataclass
class Served:
    app: AppCtx
    tools: RemoteTools
    clock: FakeClock
    path: str
    token: str
    clean: bytes


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[Served]:
    clock = FakeClock(T0)
    made: list[RemoteTools] = []

    def build(config: Config, manager: TaskManager, _log: LogCapture) -> RemoteTools:
        tools, _ = video_tools(config, manager, mkv_sized(BIG), clock=clock)
        made.append(tools)
        return tools

    remote = {"machines": [machine("dev", allowAttachments=True)], "summary": {"enabled": False}}
    async with start_app(
        tmp_path, remote=remote, remote_tools=build, rate_limit=RateLimitSettings(max=3, window_ms=60_000)
    ) as app:
        tools = made[0]
        answer = await tools.remote_attachment("dev", None, None, OWNER)
        found = LINK_RE.search(answer.text)
        assert found is not None, answer.text
        assert found.group(1) == app.base
        clean = bytes(d.media_check("big.mkv", mkv_sized(BIG))[2])
        yield Served(app, tools, clock, f"/attachments/{found.group(2)}", found.group(2), clean)


async def test_the_link_serves_the_video_with_safe_headers(served: Served) -> None:
    response = await served.app.http.get(served.path)
    assert response.status_code == 200
    assert response.content == served.clean
    headers = response.headers
    assert headers["content-type"] == "video/x-matroska"
    assert headers["content-length"] == str(len(served.clean))
    assert headers["content-disposition"] == 'inline; filename="big.mkv"'
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert headers["accept-ranges"] == "bytes"
    head = await served.app.http.head(served.path)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(served.clean))


async def test_range_requests(served: Served) -> None:
    size = len(served.clean)
    part = await served.app.http.get(served.path, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 100-199/{size}"
    assert part.content == served.clean[100:200]
    tail = await served.app.http.get(served.path, headers={"Range": "bytes=-16"})
    assert tail.status_code == 206
    assert tail.content == served.clean[-16:]
    rest = await served.app.http.get(served.path, headers={"Range": f"bytes={size - 3 * 1024 * 1024}-"})
    assert rest.content == served.clean[-3 * 1024 * 1024 :]
    past = await served.app.http.get(served.path, headers={"Range": f"bytes={size}-"})
    assert past.status_code == 416
    assert past.headers["content-range"] == f"bytes */{size}"
    several = await served.app.http.get(served.path, headers={"Range": "bytes=0-1,5-6"})
    assert several.status_code == 200
    assert len(several.content) == size
    link = served.tools.links.get(served.token)
    assert link is not None
    assert (link.requests, link.ranges) == (5, 3)
    assert link.served == 100 + 16 + 3 * 1024 * 1024 + size


async def test_wrong_tokens_and_methods_are_404_and_rate_limited(served: Served) -> None:
    wrong = "A" * 43
    assert (await served.app.http.post(served.path)).status_code == 404
    assert (await served.app.http.get(f"/attachments/{wrong}")).status_code == 404
    # The token is case-sensitive, and the route takes nothing but the token.
    assert (await served.app.http.get(f"/attachments/{served.token.swapcase()}")).status_code == 404
    limited = await served.app.http.get("/attachments/")
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
    # Misses are limited, the real link is not.
    assert (await served.app.http.get(served.path)).status_code == 200


async def test_expired_links_are_404_and_audited(served: Served) -> None:
    await served.app.http.get(served.path, headers={"Range": "bytes=0-9"})
    served.clock.t += links.LINK_TTL_MS
    assert (await served.app.http.get(served.path)).status_code == 404
    rows, _ = read_audit(served.app.config.state_dir)
    ended = [r for r in rows if r["event"] == "attachment_link" and r["outcome"] == "expired"]
    assert len(ended) == 1
    assert (ended[0]["requests"], ended[0]["ranges"], ended[0]["bytesServed"]) == (1, 1, 10)
    assert ended[0]["link"] == fingerprint(served.token)


async def test_the_request_log_never_shows_the_token(served: Served) -> None:
    await served.app.http.get(served.path)
    await served.app.http.get("/attachments/" + "B" * 43)
    text = served.app.log.text()
    assert served.token not in text
    assert "B" * 43 not in text
    paths = [r.get("path") for r in served.app.log.records if r.get("msg") == "request"]
    assert f"/attachments/({fingerprint(served.token)})" in paths


async def test_the_host_check_covers_the_link(served: Served) -> None:
    response = await served.app.http.get(served.path, headers={"Host": "evil.example"})
    assert response.status_code == 403


# --- regressions (review): the fetch slot, a finished fetch, aborted and orphaned responses ----


async def test_a_cancelled_call_leaves_the_slot_with_its_fetch(big: Tools) -> None:
    # The MCP client cancels the call while it waits for a large video: the background fetch keeps
    # the one slot until it ends, and the slot is released once, not twice.
    big.transport.hold = asyncio.Event()
    call = asyncio.create_task(big.tools.remote_attachment("dev", None, "video", OWNER))
    await big.transport.started.wait()
    fetch = big.tools._link_fetch
    assert fetch is not None
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert big.tools._fetching == 1
    assert (await big.tools.remote_attachment("dev", None, "image", OWNER)).text == att.BUSY_WORDS
    big.transport.hold.set()
    await fetch.task
    await asyncio.sleep(0)
    assert big.tools._fetching == 0
    assert big.tools._link_fetch is None
    again = await big.tools.remote_attachment("dev", None, "video", OWNER)
    assert LINK_RE.search(again.text) is not None
    assert big.transport.fetches == 1
    assert big.tools._fetching == 0


async def test_a_finished_fetch_is_not_replayed_after_its_link_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools_module, "MIN_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(tools_module, "DEADLINE_MARGIN_SECONDS", 19.8)
    clock = FakeClock(T0)
    config = make_config(tmp_path, [machine("dev", allowAttachments=True)])
    manager = await make_manager(config)
    tools, transport = video_tools(config, manager, mkv_sized(BIG), clock=clock)
    try:
        transport.hold = asyncio.Event()
        first = await tools.remote_attachment("dev", None, "video", OWNER)
        assert "Ask me for it again in a moment" in first.text
        fetch = tools._link_fetch
        assert fetch is not None
        transport.hold.set()
        await fetch.task
        await asyncio.sleep(0)
        clock.t += 20 * 60 * 1000
        # The old link expired: a new request fetches again and answers with a live link.
        answer = await tools.remote_attachment("dev", None, "video", OWNER)
        found = LINK_RE.search(answer.text)
        assert found is not None, answer.text
        assert tools.links.get(found.group(2)) is not None
        assert transport.fetches == 2
    finally:
        await tools.stop()
        await manager.shutdown("test over")


class Client:
    """An ASGI client that behaves like uvicorn: after `leave()` its send returns at once and its
    receive reports the disconnect; `pause` (when set) holds the next body message."""

    def __init__(self, *, leave_after: int | None = None) -> None:
        self.status: int | None = None
        self.bodies: list[bytes] = []
        self.gone = asyncio.Event()
        self.leave_after = leave_after
        self.pause: asyncio.Event | None = None
        self.paused = asyncio.Event()
        self._asked = False

    async def receive(self) -> dict[str, object]:
        if not self._asked:
            self._asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, object]) -> None:
        if self.gone.is_set():
            return
        if message["type"] == "http.response.start":
            status = message["status"]
            assert isinstance(status, int)
            self.status = status
            return
        if self.pause is not None:
            self.paused.set()
            await self.pause.wait()
        body = message["body"]
        assert isinstance(body, bytes)
        self.bodies.append(body)
        if self.leave_after is not None and len(self.bodies) >= self.leave_after:
            self.gone.set()


def scope_for(range_header: str | None = None) -> dict[str, object]:
    headers = [] if range_header is None else [(b"range", range_header.encode())]
    return {"type": "http", "method": "GET", "headers": headers}


async def test_aborted_open_ended_ranges_cost_only_what_was_sent() -> None:
    # A player asks for `bytes=N-` on every seek and drops the connection after a few KB: the link
    # must survive many seeks, and an aborted range stops being copied.
    store = links.LinkStore(now=FakeClock(T0))
    size = 10 * 1024 * 1024
    link = add(store, bytes(size), size=size)
    for i in range(30):
        client = Client(leave_after=1)
        await links.serve_link(store, link, scope_for(f"bytes={i * 100_000}-"), client.receive, client.send)  # type: ignore[arg-type]
        assert client.status == 206
        assert len(client.bodies) == 1
    assert store.get(link.token) is link
    assert link.served <= 30 * 2 * links.SEND_BYTES
    assert link.requests == 30


async def test_the_budget_is_charged_per_slice_and_cuts_the_response() -> None:
    store = links.LinkStore(now=FakeClock(T0))
    size = 3 * links.SEND_BYTES
    link = add(store, bytes(size), size=size)
    link.served = links.MAX_SERVED_FACTOR * size - links.SEND_BYTES - 1
    client = Client()
    await links.serve_link(store, link, scope_for(), client.receive, client.send)  # type: ignore[arg-type]
    # The first slice still fit, the second did not: the response stopped and the link ended.
    assert client.status == 200
    assert len(client.bodies) == 1
    assert store.get(link.token) is None


@pytest.mark.parametrize("end", ["evicted", "expired"])
async def test_a_stalled_reader_does_not_keep_an_ended_link_alive(end: str) -> None:
    clock = FakeClock(T0)
    store = links.LinkStore(now=clock, max_bytes=4 * links.SEND_BYTES)
    size = 3 * links.SEND_BYTES
    link = add(store, bytes(size), size=size)
    client = Client()
    client.pause = asyncio.Event()
    serving = asyncio.create_task(links.serve_link(store, link, scope_for(), client.receive, client.send))  # type: ignore[arg-type]
    await client.paused.wait()
    if end == "evicted":
        add(store, bytes(2 * links.SEND_BYTES), name="newer.mkv")
    else:
        clock.t += links.LINK_TTL_MS
        store.sweep()
    assert link.data == b""
    client.pause.set()
    await asyncio.wait_for(serving, 5)
    # The slice in flight went out; nothing after it.
    assert len(client.bodies) == 1
