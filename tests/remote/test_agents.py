"""The hub side of outbound agents (`remote/agents.py`): the relay socket and its checks, the
handshake, one live connection per machine, heartbeats, AgentTransport, the control socket and the
authorized_keys lint. The machine is a FakeMachine on the hub's socket (every frame under the
test's control); the full chain with the real relay and dispatcher is in test_agent_tools.py."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FakeClock, LogCapture
from tests.remote.agent_helpers import (
    OWN_USER,
    FakeMachine,
    agent_machine,
    agents_section,
    frame,
    hello_frame,
    ok_doc,
    socket_dir,
    wait_until,
)
from voice_coding_mcp.remote import agents as agents_module
from voice_coding_mcp.remote.agents import AgentHub, control_request, lint_authorized_keys
from voice_coding_mcp.remote.config import RemoteConfig
from voice_coding_mcp.remote.protocol import CaptureResponse, ListResponse, VersionResponse
from voice_coding_mcp.remote.speech import unreachable_reason, unreachable_sentence

T0 = 1_764_160_000_000


class Env:
    def __init__(self, hub: AgentHub, directory: Path, log: LogCapture, clock: FakeClock) -> None:
        self.hub = hub
        self.directory = directory
        self.log = log
        self.clock = clock
        self.socket = str(directory / "hub.sock")
        self.machines: list[FakeMachine] = []
        self.sockets: list[socket.socket] = []

    async def connect(self, machine: str = "laptop", **kwargs: Any) -> FakeMachine:
        fake = await FakeMachine.connect(self.socket, machine, **kwargs)
        self.machines.append(fake)
        return fake

    async def connected(self, machine: str = "laptop", **kwargs: Any) -> FakeMachine:
        """A machine that completed the handshake (welcome read)."""
        before = self.hub.live(machine)
        fake = await self.connect(machine, **kwargs)
        assert await fake.read() == {"t": "welcome", "protocol": 1}
        await wait_until(lambda: self.hub.live(machine) not in (None, before))
        return fake

    def transport(self, name: str = "laptop") -> Any:
        return self.hub.transport(self.hub.machines[name])

    def messages(self) -> list[str]:
        return [r["msg"] for r in self.log.records]


def remote_config(directory: Path, **agents: Any) -> RemoteConfig:
    return RemoteConfig.model_validate(
        {
            "machines": [agent_machine("laptop"), agent_machine("desk", allowSend=True)],
            "agents": agents_section(directory, **agents),
        }
    )


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    with socket_dir() as directory:
        log = LogCapture()
        clock = FakeClock(T0)
        hub = AgentHub(remote_config(directory), str(directory), log.logger, now=clock)
        await hub.start()
        assert hub.problem is None, hub.problem
        context = Env(hub, directory, log, clock)
        try:
            yield context
        finally:
            for fake in context.machines:
                fake.close()
            for sock in context.sockets:
                sock.close()
            await hub.stop()


# --- the socket and its directory ------------------------------------------------------------------


async def test_the_relay_socket_is_0660_and_the_control_socket_0600(env: Env) -> None:
    assert stat.S_IMODE(os.stat(env.socket).st_mode) == 0o660
    assert stat.S_ISSOCK(os.stat(env.socket).st_mode)
    assert stat.S_IMODE(os.stat(env.hub.control_path).st_mode) == 0o600


async def test_stop_removes_both_sockets(env: Env) -> None:
    await env.hub.stop()
    assert not os.path.exists(env.socket)  # noqa: ASYNC240
    assert not os.path.exists(env.hub.control_path)  # noqa: ASYNC240


@pytest.mark.parametrize("mode", [0o750, 0o2770, 0o2755, 0o2700])
async def test_a_socket_directory_with_another_mode_is_not_served(mode: int) -> None:
    with socket_dir() as directory:
        directory.chmod(mode)
        log = LogCapture()
        hub = AgentHub(remote_config(directory), str(directory), log.logger)
        await hub.start()
        try:
            assert hub.problem is not None
            assert "2750" in hub.problem
            assert not os.path.exists(directory / "hub.sock")  # noqa: ASYNC240
            assert hub.status()["problem"] == hub.problem
            assert "agent socket not served" in [r["msg"] for r in log.records]
        finally:
            await hub.stop()


async def test_a_missing_directory_or_relay_user_is_not_served(tmp_path: Path) -> None:
    remote = RemoteConfig.model_validate(
        {
            "machines": [agent_machine("laptop")],
            "agents": {"socketPath": "/nonexistent-sb/hub.sock", "relayUser": OWN_USER},
        }
    )
    hub = AgentHub(remote, str(tmp_path), LogCapture().logger, control_path=str(tmp_path / "c.sock"))
    await hub.start()
    assert hub.problem is not None
    assert "does not exist" in hub.problem
    await hub.stop()
    with socket_dir() as directory:
        remote = RemoteConfig.model_validate(
            {"machines": [agent_machine("laptop")], "agents": agents_section(directory, relayUser="no-such-user-sb")}
        )
        hub = AgentHub(remote, str(directory), LogCapture().logger)
        await hub.start()
        assert hub.problem == "the relay user no-such-user-sb does not exist"
        await hub.stop()


async def test_a_stale_socket_of_the_hub_is_replaced_and_a_foreign_file_is_not() -> None:
    with socket_dir() as directory:
        first = AgentHub(remote_config(directory), str(directory), LogCapture().logger)
        await first.start()
        # A crash leaves the socket behind: simulate it by closing the servers only.
        for server in first._servers:
            server.close()
        second = AgentHub(
            remote_config(directory), str(directory), LogCapture().logger, control_path=str(directory / "c2")
        )
        await second.start()
        assert second.problem is None
        await second.stop()
        (directory / "hub.sock").write_text("", encoding="utf-8")
        third = AgentHub(
            remote_config(directory), str(directory), LogCapture().logger, control_path=str(directory / "c3")
        )
        await third.start()
        assert third.problem is not None
        assert "not a socket" in third.problem
        await third.stop()


async def test_no_agent_machine_serves_nothing(tmp_path: Path) -> None:
    hub = AgentHub(RemoteConfig(), str(tmp_path), LogCapture().logger)
    assert not hub.enabled
    await hub.start()
    assert not os.path.exists(tmp_path / agents_module.CONTROL_SOCKET_NAME)  # noqa: ASYNC240
    await hub.stop()


async def test_a_connection_from_another_uid_is_closed_before_reading(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.hub, "_relay_uid", os.getuid() + 1)
    fake = await env.connect()
    assert await fake.closed() == []
    assert env.hub.live("laptop") is None
    assert "agent connection from another user refused" in env.messages()


async def test_the_connection_cap(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env.hub, "_open", 2 * 2 + 4)
    fake = await env.connect()
    assert await fake.closed() == []
    assert "agent connection refused: too many connections" in env.messages()


# --- handshake -----------------------------------------------------------------------------------


async def test_a_connected_machine(env: Env) -> None:
    await env.connected("laptop")
    status = env.hub.status()
    laptop = next(m for m in status["machines"] if m["name"] == "laptop")
    assert laptop["connected"] is True
    assert laptop["sinceMs"] == T0
    assert laptop["peer"] == "203.0.113.5"
    assert laptop["version"] == hello_frame()["version"]
    assert laptop["warnings"] == []
    desk = next(m for m in status["machines"] if m["name"] == "desk")
    assert desk["connected"] is False
    assert "agent connected" in env.messages()


async def test_an_unknown_machine_gets_bye_unknown_machine(env: Env) -> None:
    fake = await env.connect("intruder")
    assert await fake.closed() == [{"t": "bye", "reason": "unknown_machine"}]
    assert env.hub.live("intruder") is None
    record = next(r for r in env.log.records if r["msg"] == "agent connection for an unknown machine")
    assert record["machine"] == "intruder"


@pytest.mark.parametrize(
    "preamble",
    [
        b"not json\n",
        b'{"t":"relay","protocol":1,"machine":"laptop"}\n',
        b'{"t":"relay","protocol":2,"machine":"laptop","peer":null}\n',
        b'{"t":"relay","protocol":1,"machine":"Laptop","peer":null}\n',
        b'{"t":"relay","protocol":1,"machine":"laptop","peer":null,"x":1}\n',
        b'{"t":"hello","protocol":1,"machine":"laptop","peer":null}\n',
    ],
)
async def test_a_bad_preamble_is_refused(env: Env, preamble: bytes) -> None:
    reader, writer = await asyncio.open_unix_connection(env.socket)
    writer.write(preamble)
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 10) == b'{"t":"bye","reason":"protocol"}\n'
    writer.close()
    assert env.hub.live("laptop") is None


async def test_a_peer_that_is_not_an_address_is_dropped(env: Env) -> None:
    await env.connected(peer="evil.example; rm")
    assert env.hub.live("laptop").peer is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "hello",
    [
        hello_frame(protocol=2),
        hello_frame(version="0.6.0 evil"),
        hello_frame(verbs="list"),
        hello_frame(allowSend="yes"),
        hello_frame(policy="maybe"),
        {**hello_frame(), "machine": "desk"},
        {k: v for k, v in hello_frame().items() if k != "policy"},
    ],
)
async def test_a_bad_hello_is_refused(env: Env, hello: dict[str, Any]) -> None:
    fake = await env.connect(hello=hello)
    assert await fake.closed() == [{"t": "bye", "reason": "protocol"}]
    assert env.hub.live("laptop") is None


async def test_handshake_timeouts(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_module, "PREAMBLE_SECONDS", 0.2)
    monkeypatch.setattr(agents_module, "HELLO_SECONDS", 0.2)
    reader, writer = await asyncio.open_unix_connection(env.socket)
    assert await asyncio.wait_for(reader.read(), 10) == b'{"t":"bye","reason":"protocol"}\n'
    writer.close()
    fake = await env.connect(send_hello=False)
    assert await fake.closed() == [{"t": "bye", "reason": "protocol"}]


async def test_hello_flags_that_differ_from_the_hub_are_warnings(env: Env) -> None:
    await env.connected("desk", hello=hello_frame(allowSend=False, allowUsage=False, policy="bad_policy"))
    desk = next(m for m in env.hub.status()["machines"] if m["name"] == "desk")
    assert len(desk["warnings"]) == 3
    assert any("bad_policy" in w for w in desk["warnings"])
    assert any("allowSend" in w for w in desk["warnings"])
    assert env.messages().count("agent policy differs") == 3


# --- replacement -----------------------------------------------------------------------------------


async def test_a_newer_connection_replaces_the_older(env: Env) -> None:
    old = await env.connected()
    first = env.hub.live("laptop")
    env.clock.t += 1000
    new = await env.connected()
    assert await old.closed() == [{"t": "bye", "reason": "replaced"}]
    second = env.hub.live("laptop")
    assert second is not None
    assert second is not first
    assert second.since_ms == T0 + 1000
    laptop = next(m for m in env.hub.status()["machines"] if m["name"] == "laptop")
    assert laptop["replacementsLastHour"] == 1
    assert laptop["connected"] is True
    new.close()
    await wait_until(lambda: env.hub.live("laptop") is None)
    laptop = next(m for m in env.hub.status()["machines"] if m["name"] == "laptop")
    assert laptop["lastDisconnectReason"] == "eof"


async def test_frequent_replacements_log_a_warning(env: Env) -> None:
    for _ in range(5):
        await env.connected()
        env.clock.t += 1000
    assert "agent connection replaced often: two connectors share a key, or the key was stolen" in env.messages()


async def test_a_read_request_in_flight_is_retried_on_the_new_connection(env: Env) -> None:
    old = await env.connected()
    call = asyncio.create_task(env.transport().call("version", timeout_seconds=10))
    request = await old.expect("req")
    assert request["verb"] == "version"
    new = await env.connected()
    retried = await new.expect("req")
    assert retried["verb"] == "version"
    await new.send({"t": "res", "id": retried["id"], "doc": ok_doc(version="0.6.0", verbs=["version"])})
    result = await call
    assert result.ok
    assert isinstance(result.data, VersionResponse)


async def test_a_send_in_flight_on_a_replaced_connection_is_disconnected_never_retried(env: Env) -> None:
    old = await env.connected("desk")
    call = asyncio.create_task(
        env.transport("desk").call("send", ["%1", "1", "none", "message"], timeout_seconds=10, stdin=b"deploy")
    )
    request = await old.expect("req")
    assert request["input"] == "deploy"
    new = await env.connected("desk")
    result = await call
    assert (result.ok, result.error) == (False, "disconnected")
    # Nothing was sent on the new connection.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(new.expect("req"), 0.3)


# --- requests ------------------------------------------------------------------------------------


async def test_not_connected_answers_at_once(env: Env) -> None:
    result = await env.transport().call("list", timeout_seconds=10)
    assert (result.ok, result.error) == (False, "not_connected")
    assert result.elapsed_ms < 1000


async def test_a_list_round_trip(env: Env) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("list", timeout_seconds=10))
    request = await fake.expect("req")
    assert request == {"t": "req", "id": request["id"], "verb": "list", "args": []}
    pane = {"session": "fpga", "paneId": "%1", "panePid": 7, "windowIndex": 0}
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(panes=[pane])})
    result = await call
    assert result.ok
    assert isinstance(result.data, ListResponse)
    assert result.data.panes[0].pane_id == "%1"


async def test_a_refusal_is_refused_by_policy_with_the_dispatchers_code(env: Env) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("capture", ["%1", "10"], timeout_seconds=10))
    request = await fake.expect("req")
    await fake.send({"t": "res", "id": request["id"], "doc": {"protocol": 1, "ok": False, "error": "gone"}})
    result = await call
    assert (result.error, result.detail) == ("refused_by_policy", "gone")


@pytest.mark.parametrize(
    ("code", "error"), [("busy", "refused_by_policy"), ("bad_frame", "refused_by_policy"), ("crashed", "other")]
)
async def test_frame_errors(env: Env, code: str, error: str) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("version", timeout_seconds=10))
    request = await fake.expect("req")
    await fake.send({"t": "error", "id": request["id"], "error": code})
    result = await call
    assert (result.error, result.detail) == (error, code)


def test_a_busy_dispatcher_is_spoken_as_busy_not_refused() -> None:
    # After timeouts the dispatcher may still run earlier requests and answer `busy`.
    assert unreachable_sentence("laptop", "refused_by_policy", "busy") == (
        "laptop is not reachable: it is busy, try again in a moment."
    )
    assert unreachable_reason("refused_by_policy", "gone") == "switchboard-remote refused the request"


async def test_a_bad_document_is_bad_response(env: Env) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("capture", ["%1", "10"], timeout_seconds=10))
    request = await fake.expect("req")
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(paneId="%1")})
    assert (await call).error == "bad_response"
    # Two documents for a verb that answers with one.
    call = asyncio.create_task(env.transport().call("version", timeout_seconds=10))
    request = await fake.expect("req")
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(version="1", verbs=[]), "more": True})
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(version="1", verbs=[])})
    assert (await call).error == "bad_response"


async def test_a_usage_stream_is_parsed_like_ssh_output(env: Env) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("usage", ["0", "10"], timeout_seconds=10, stdin=b"[]"))
    request = await fake.expect("req")
    assert request["input"] == "[]"
    rid = request["id"]
    await fake.send({"t": "res", "id": rid, "doc": ok_doc(stream="usage"), "more": True})
    await fake.send({"t": "res", "id": rid, "doc": {"done": True, "gone": []}})
    result = await call
    assert result.ok, result
    # A stream without its done line is cut.
    call = asyncio.create_task(env.transport().call("usage", ["0", "10"], timeout_seconds=10))
    request = await fake.expect("req")
    assert "input" not in request
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(stream="usage")})
    assert (await call).error == "bad_response"


async def test_a_timeout_sends_cancel_and_the_late_answer_is_dropped(env: Env) -> None:
    fake = await env.connected()
    result = await env.transport().call("capture", ["%1", "10"], timeout_seconds=0.3)
    assert result.error == "timeout"
    request = await fake.expect("req")
    cancel = await fake.expect("cancel")
    assert cancel == {"t": "cancel", "id": request["id"]}
    await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(text="late", paneId="%1", session="fpga")})
    # The connection stays up and the next request gets its own answer.
    call = asyncio.create_task(env.transport().call("version", timeout_seconds=10))
    second = await fake.expect("req")
    assert second["id"] > request["id"]
    await fake.send({"t": "res", "id": second["id"], "doc": ok_doc(version="0.6.0", verbs=[])})
    assert (await call).ok


async def test_a_disconnect_mid_call_is_disconnected(env: Env) -> None:
    fake = await env.connected()
    call = asyncio.create_task(env.transport().call("capture", ["%1", "10"], timeout_seconds=10))
    await fake.expect("req")
    fake.close()
    assert (await call).error == "disconnected"
    await wait_until(lambda: env.hub.live("laptop") is None)
    assert (await env.transport().call("version", timeout_seconds=1)).error == "not_connected"


async def test_four_requests_in_flight_and_the_fifth_waits(env: Env) -> None:
    fake = await env.connected()
    calls = [asyncio.create_task(env.transport().call("version", timeout_seconds=10)) for _ in range(5)]
    requests = [await fake.expect("req") for _ in range(4)]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fake.expect("req"), 0.3)
    await fake.send({"t": "res", "id": requests[0]["id"], "doc": ok_doc(version="1", verbs=[])})
    fifth = await fake.expect("req")
    for request in [*requests[1:], fifth]:
        await fake.send({"t": "res", "id": request["id"], "doc": ok_doc(version="1", verbs=[])})
    assert all(result.ok for result in await asyncio.gather(*calls))
    assert len({r["id"] for r in [*requests, fifth]}) == 5


async def test_answers_out_of_order_reach_their_requests(env: Env) -> None:
    fake = await env.connected()
    first = asyncio.create_task(env.transport().call("capture", ["%1", "10"], timeout_seconds=10))
    request_one = await fake.expect("req")
    second = asyncio.create_task(env.transport().call("capture", ["%2", "10"], timeout_seconds=10))
    request_two = await fake.expect("req")
    await fake.send({"t": "res", "id": request_two["id"], "doc": ok_doc(text="two", paneId="%2", session="fpga")})
    await fake.send({"t": "res", "id": request_one["id"], "doc": ok_doc(text="one", paneId="%1", session="fpga")})
    one, two = await first, await second
    assert isinstance(one.data, CaptureResponse)
    assert isinstance(two.data, CaptureResponse)
    assert (one.data.text, two.data.text) == ("one", "two")


@pytest.mark.parametrize(
    "bad",
    [
        b"not json\n",
        b'{"t":"res","id":1}\n',
        b'{"t":"res","id":1,"doc":[]}\n',
        b'{"t":"res","id":1,"doc":{},"more":false}\n',
        b'{"t":"hello","protocol":1}\n',
        b'{"t":"req","id":1,"verb":"list","args":[]}\n',
        b'{"t":"error","id":1,"error":"whatever"}\n',
        b'{"t":"pong","id":0}\n',
    ],
)
async def test_any_malformed_machine_frame_closes_the_connection(env: Env, bad: bytes) -> None:
    fake = await env.connected()
    await fake.write(bad)
    frames = await fake.closed()
    assert frames[-1] == {"t": "bye", "reason": "protocol"}
    await wait_until(lambda: env.hub.live("laptop") is None)


async def test_a_machine_frame_over_the_cap_closes_the_connection(env: Env) -> None:
    fake = await env.connected()
    await fake.write(b'{"t":"pong","id":1,"x":"' + b"x" * agents_module.MAX_MACHINE_FRAME_BYTES + b'"}\n')
    await wait_until(lambda: env.hub.live("laptop") is None)


async def test_a_request_over_the_frame_cap_is_not_sent(env: Env) -> None:
    fake = await env.connected()
    result = await env.transport().call(
        "usage", ["0", "10"], timeout_seconds=5, stdin=b"x" * (agents_module.MAX_HUB_FRAME_BYTES + 1)
    )
    assert result.error == "other"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fake.expect("req"), 0.3)


async def test_arguments_outside_the_charset_are_never_sent(env: Env) -> None:
    fake = await env.connected()
    for verb, args in (("capture", ["%1;rm", "1"]), ("capture", ["-x", "1"]), ("bogus", [])):
        assert (await env.transport().call(verb, args, timeout_seconds=1)).error == "other"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fake.expect("req"), 0.3)


# --- heartbeat -----------------------------------------------------------------------------------


async def test_pings_and_a_missed_pong_closes_the_connection(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_module, "PONG_SECONDS", 0.3)
    env.hub.settings = env.hub.settings.model_copy(update={"heartbeat_seconds": 0.2})
    fake = await env.connected()
    ping = await fake.expect("ping")
    await fake.send({"t": "pong", "id": ping["id"]})
    await fake.expect("ping")
    # No answer now: the hub gives up.
    await wait_until(lambda: env.hub.live("laptop") is None, seconds=5)
    assert "agent heartbeat missed" in env.messages()


# --- a machine that stops reading ------------------------------------------------------------------

BIG_INPUT = b"k" * 900_000
"""More than the socket buffers between the hub and a FakeMachine that never reads hold."""


async def stops_reading(env: Env, machine: str = "laptop") -> socket.socket:
    """A machine that completes the handshake and then never reads its channel again (a plain
    socket: a FakeMachine's StreamReader would keep reading into its buffer)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(sock, env.socket)
    await loop.sock_sendall(sock, frame({"t": "relay", "protocol": 1, "machine": machine, "peer": None}))
    await loop.sock_sendall(sock, frame(hello_frame()))
    welcome = b""
    while not welcome.endswith(b"\n"):
        welcome += await loop.sock_recv(sock, 1)
    assert json.loads(welcome) == {"t": "welcome", "protocol": 1}
    await wait_until(lambda: env.hub.live(machine) is not None)
    env.sockets.append(sock)
    return sock


def last_close_reason(env: Env, machine: str = "laptop") -> str | None:
    entry = next(m for m in env.hub.status()["machines"] if m["name"] == machine)
    reason = entry["lastDisconnectReason"]
    return reason if isinstance(reason, str) else None


async def test_a_machine_that_stops_reading_is_closed_not_waited_for(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_module, "WRITE_STALL_SECONDS", 0.3)
    await stops_reading(env)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await env.transport().call("usage", timeout_seconds=60, stdin=BIG_INPUT)
    assert result.error == "disconnected"  # the request may have gone out in part
    assert loop.time() - started < 5
    await wait_until(lambda: last_close_reason(env) == "write_stalled", seconds=5)
    assert "agent connection stopped reading" in env.messages()
    assert (await env.transport().call("list", timeout_seconds=10)).error == "not_connected"


async def test_a_stalled_write_still_ends_the_call_at_its_deadline(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    # The cancel after the deadline goes out in the background, so the caller is not held up.
    monkeypatch.setattr(agents_module, "WRITE_STALL_SECONDS", 2.0)
    await stops_reading(env)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await env.transport().call("usage", timeout_seconds=0.3, stdin=BIG_INPUT)
    assert result.error == "timeout"
    assert loop.time() - started < 1.5
    await wait_until(lambda: last_close_reason(env) == "write_stalled", seconds=10)


async def test_a_heartbeat_that_cannot_be_written_closes_the_connection(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents_module, "WRITE_STALL_SECONDS", 0.3)
    monkeypatch.setattr(agents_module, "PONG_SECONDS", 60.0)
    monkeypatch.setattr(agents_module.AgentConnection, "send_later", lambda self, frame: None)
    env.hub.settings = env.hub.settings.model_copy(update={"heartbeat_seconds": 0.2})
    await stops_reading(env)
    # Fill the buffers with a request that times out; no cancel goes out (send_later is off), so
    # only the heartbeat's ping can notice that the machine stopped reading.
    assert (await env.transport().call("usage", timeout_seconds=0.2, stdin=BIG_INPUT)).error == "timeout"
    await wait_until(lambda: last_close_reason(env) == "write_stalled", seconds=10)


# --- unfinished handshakes ---------------------------------------------------------------------------


async def test_unfinished_handshakes_are_limited_per_machine(env: Env) -> None:
    waiting = [await env.connect("laptop", send_hello=False) for _ in range(2)]
    await wait_until(lambda: env.hub._handshaking.get("laptop") == 2)
    third = await env.connect("laptop", send_hello=False)
    assert await third.closed() == [{"t": "bye", "reason": "protocol"}]
    assert "agent connection refused: too many unfinished handshakes" in env.messages()
    # Another machine still connects, and so does laptop once a waiting handshake finishes.
    await env.connected("desk")
    await waiting[0].send(hello_frame())
    assert await waiting[0].read() == {"t": "welcome", "protocol": 1}
    await wait_until(lambda: env.hub._handshaking.get("laptop") == 1)
    await env.connected("laptop")


async def test_binding_the_sockets_leaves_the_umask_alone(tmp_path: Path) -> None:
    with socket_dir() as directory:
        hub = AgentHub(remote_config(directory), str(directory), LogCapture().logger)
        before = os.umask(0o022)
        try:
            await hub.start()
            assert os.umask(0o022) == 0o022
        finally:
            os.umask(before)
            await hub.stop()
        assert hub.problem is None


# --- control socket ------------------------------------------------------------------------------


async def test_the_control_socket_answers_status_and_check(env: Env) -> None:
    await env.connected()

    async def check(name: str) -> tuple[list[str], bool]:
        return [f"  checked {name}"], True

    env.hub.check = check
    status = await control_request(env.hub.control_path, {"t": "status"})
    assert status is not None
    assert [m["name"] for m in status["machines"]] == ["laptop", "desk"]
    assert await control_request(env.hub.control_path, {"t": "check", "machine": "laptop"}) == {
        "ok": True,
        "lines": ["  checked laptop"],
    }


@pytest.mark.parametrize(
    "request_",
    [
        {"t": "check", "machine": "nope"},
        {"t": "check"},
        {"t": "send", "machine": "laptop", "text": "hi"},
        {"t": "capture", "machine": "laptop", "pane": "%1"},
        {"t": "status", "extra": 1},
    ],
)
async def test_the_control_socket_answers_nothing_else(env: Env, request_: dict[str, Any]) -> None:
    env.hub.check = None
    assert await control_request(env.hub.control_path, request_) == {"ok": False, "error": "bad_request"}


async def test_control_request_without_a_hub_is_none(tmp_path: Path) -> None:
    assert await control_request(str(tmp_path / "missing.sock"), {"t": "status"}) is None


async def test_the_control_socket_checks_the_peer_uid(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_module, "_peer_uid", lambda _writer: os.getuid() + 1)
    assert await control_request(env.hub.control_path, {"t": "status"}) is None


# --- authorized_keys lint ------------------------------------------------------------------------

RELAY = "/opt/voice-coding-mcp/.venv/bin/python -I -m voice_coding_mcp.remote.relay --machine"
KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def test_a_good_key_file_has_no_problems() -> None:
    text = (
        "# relay keys\n\n"
        f'restrict,command="{RELAY} laptop" {KEY_A} switchboard-connect laptop\n'
        f'from="198.51.100.7",restrict,command="{RELAY} desk --socket /run/switchboard-relay/hub.sock" {KEY_B} desk\n'
    )
    assert lint_authorized_keys(text, ["laptop", "desk"]) == []


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        (f"{KEY_A} bare", "a key without options"),
        (f'command="{RELAY} laptop" {KEY_A}', "no restrict option"),
        (f"restrict {KEY_A}", "needs exactly one command= option"),
        (f'restrict,command="/bin/sh" {KEY_A}', "command= is not the switchboard relay"),
        (f'restrict,command="{RELAY} laptop; sh" {KEY_A}', "command= is not the switchboard relay"),
        (f'restrict,command="{RELAY} other" {KEY_A}', "--machine other is not a configured agent machine"),
        (f'restrict,command="{RELAY} laptop",command="{RELAY} laptop" {KEY_A}', "needs exactly one command="),
        (f'restrict,port-forwarding,command="{RELAY} laptop" {KEY_A}', "unexpected options port-forwarding"),
        (f'restrict,command="{RELAY} laptop"', "no key after the options"),
        ('restrict,command="unterminated', "cannot read the options"),
    ],
)
def test_key_file_problems(line: str, problem: str) -> None:
    problems = lint_authorized_keys(line + "\n", ["laptop"])
    assert any(problem in p for p in problems), problems


def test_the_same_key_twice_and_missing_machines() -> None:
    text = f'restrict,command="{RELAY} laptop" {KEY_A}\nrestrict,command="{RELAY} laptop" {KEY_A}\n'
    problems = lint_authorized_keys(text, ["laptop", "desk"])
    assert "line 2: the same key as line 1" in problems
    assert "no key line for agent machine desk" in problems


def test_status_is_json_serializable(env: Env) -> None:
    json.dumps(env.hub.status())
