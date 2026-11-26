"""The hub reading usage answers: strict line parsing, the `usage` response through `parse_response`,
and the verb end to end over the fake ssh and the local transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import RemoteWorld
from tests.usage.helpers import MIN, S1, S2, T0, assistant, user, write_transcript
from voice_coding_mcp.remote.config import MachineConfig, RemoteConfig, SshConfig
from voice_coding_mcp.remote.protocol import MAX_USAGE_RESPONSE_BYTES, ResponseError, UsageResponse, parse_response
from voice_coding_mcp.remote.transport import LocalTransport, SshTransport, make_transport
from voice_coding_mcp.usage.records import (
    Record,
    UsageStreamError,
    parse_documents,
    parse_lines,
    read_summaries,
    scan_local,
)

HEADER = {"protocol": 1, "ok": True, "stream": "usage"}


def lines(*documents: Any) -> bytes:
    return b"".join(json.dumps(doc).encode() + b"\n" for doc in documents)


def file_header(file_id: str = S1, records: int = 1, **extra: Any) -> dict[str, Any]:
    return {"file": file_id, "size": 100, "mtime": T0, "project": "fpgadc", "records": records, **extra}


RECORD = {
    "t": T0,
    "k": "a",
    "m": "claude-opus-4-8",
    "id": "msg_1",
    "rq": "req_1",
    "in": 1,
    "out": 2,
    "cw5": 3,
    "cw1": 4,
    "cr": 5,
}


def test_parse_documents_builds_the_batch() -> None:
    batch = parse_documents(
        [
            file_header(records=2),
            RECORD,
            {"t": T0 + MIN, "k": "u"},
            {"file": S2.upper(), "size": 9, "mtime": T0, "skipped": True},
            {"done": True, "gone": ["22222222-0000-4000-8000-0000000000FF"]},
        ]
    )
    assert [f.id for f in batch.files] == [S1, S2]
    assert batch.files[0].records == [
        Record(
            t=T0,
            kind="a",
            model="claude-opus-4-8",
            msg_id="msg_1",
            req_id="req_1",
            input=1,
            output=2,
            cache_write_5m=3,
            cache_write_1h=4,
            cache_read=5,
        ),
        Record(t=T0 + MIN, kind="u"),
    ]
    assert batch.files[1].skipped
    assert batch.gone == ["22222222-0000-4000-8000-0000000000ff"]


@pytest.mark.parametrize(
    "documents",
    [
        [file_header(), RECORD],
        [RECORD, {"done": True}],
        [file_header(records=2), RECORD, {"done": True}],
        [file_header(records=0), RECORD, {"done": True}],
        [file_header(), {**RECORD, "k": "x"}, {"done": True}],
        [file_header(), {**RECORD, "in": -1}, {"done": True}],
        [file_header(), {**RECORD, "in": True}, {"done": True}],
        [file_header(), {**RECORD, "in": 1.5}, {"done": True}],
        [file_header(), {**RECORD, "m": "a\nb"}, {"done": True}],
        [file_header(), {**RECORD, "m": "x" * 65}, {"done": True}],
        [file_header(), {**RECORD, "t": "now"}, {"done": True}],
        [file_header(file_id="../../etc/passwd"), RECORD, {"done": True}],
        [{"done": True}, {"done": True}],
        [{"what": 1}, {"done": True}],
        [[1], {"done": True}],
        [{"done": True, "gone": "x"}],
    ],
)
def test_parse_documents_refuses_bad_shapes(documents: list[Any]) -> None:
    with pytest.raises(UsageStreamError):
        parse_documents(documents)


def test_project_names_are_capped_and_printable() -> None:
    batch = parse_documents([file_header(records=0, project="a\x1b[2Jb" + "x" * 100), {"done": True}])
    assert batch.files[0].project == ("a[2Jb" + "x" * 100)[:64]


def test_parse_lines_refuses_non_json() -> None:
    with pytest.raises(UsageStreamError):
        parse_lines([b"{nope"])


def test_parse_response_usage() -> None:
    data = parse_response("usage", lines(HEADER, file_header(), RECORD, {"done": True, "gone": []}))
    assert isinstance(data, UsageResponse)
    assert data.batch.files[0].records[0].input == 1


def test_parse_response_usage_refusal_and_garbage() -> None:
    assert parse_response("usage", lines({"protocol": 1, "ok": False, "error": "not_allowed"})) == "not_allowed"
    with pytest.raises(ResponseError) as cut:
        parse_response("usage", lines(HEADER, file_header(), RECORD))
    assert cut.value.code == "bad_response"
    with pytest.raises(ResponseError) as no_stream:
        parse_response("usage", lines({"protocol": 1, "ok": True}, {"done": True}))
    assert no_stream.value.code == "bad_response"
    with pytest.raises(ResponseError) as missing:
        parse_response("usage", b"bash: switchboard-remote: command not found\n")
    assert missing.value.code == "dispatcher_missing"
    with pytest.raises(ResponseError):
        parse_response("usage", b"x" * (MAX_USAGE_RESPONSE_BYTES + 1))


def test_read_summaries(tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "summaries.jsonl").write_text(
        '{"time":"2025-11-24T16:00:00.000Z","model":"haiku","input":10,"output":5,"cacheRead":2,"cacheWrite":3,"costUsd":0.01}\n'
        '{"time":"2025-11-24T16:01:00.000Z","model":"haiku","input":1,"costUsd":NaN}\n'
        "garbage\n"
        '{"time":"2020-01-01T00:00:00.000Z","input":99}\n',
        encoding="utf-8",
    )
    records = read_summaries(str(tmp_path), since_ms=T0 - 1)
    assert records == [
        Record(t=T0, kind="a", model="haiku", input=10, output=5, cache_write_5m=3, cache_read=2, cost_usd=0.01),
        Record(t=T0 + MIN, kind="a", model="haiku", input=1),
    ]


def test_scan_local_uses_the_dispatcher_parser(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    write_transcript(home, S1, [user(T0), assistant(T0 + MIN)])
    batch = scan_local(str(home), 0, 1024 * 1024, {})
    assert [len(f.records) for f in batch.files] == [2]


# --- end to end ------------------------------------------------------------------------------------


def machine(world: RemoteWorld, **keys: object) -> MachineConfig:
    return MachineConfig.model_validate(
        {
            "name": "dev",
            "host": "localhost",
            "user": "owner",
            "identityFile": "/home/switchboard/.ssh/switchboard_dev",
            "knownHostsFile": str(world.root / "known_hosts"),
            "sessions": ["fpga"],
            **keys,
        }
    )


async def test_usage_over_ssh(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path, forced=["--sessions", "fpga", "--allow-usage"])
    write_transcript(Path(world.home) / ".claude", S1, [user(T0), assistant(T0 + MIN)])
    remote = RemoteConfig(ssh=SshConfig(bin=world.ssh))
    t = make_transport(
        machine(world), remote, str(tmp_path / "state"), {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    )
    assert isinstance(t, SshTransport)
    known = json.dumps([{"id": S2, "size": 1, "mtime": 1}]).encode()
    result = await t.call("usage", ["0", "512"], timeout_seconds=30, stdin=known)
    assert result.ok, result
    assert isinstance(result.data, UsageResponse)
    assert [f.id for f in result.data.batch.files] == [S1]
    assert result.data.batch.gone == [S2]
    call = world.ssh_calls()[0]
    assert call["argv"][-1] == "switchboard-remote usage 0 512"
    assert call["stdin"] == known.decode()


async def test_usage_refused_without_allow_usage(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path, forced=["--sessions", "fpga"])
    remote = RemoteConfig(ssh=SshConfig(bin=world.ssh))
    t = make_transport(machine(world), remote, str(tmp_path / "state"), {"PATH": "/usr/bin:/bin"})
    result = await t.call("usage", ["0", "512"], timeout_seconds=30, stdin=b"[]")
    assert (result.ok, result.error, result.detail) == (False, "refused_by_policy", "not_allowed")


async def test_usage_through_the_local_transport(tmp_path: Path) -> None:
    claude_home = tmp_path / "owner-claude"
    write_transcript(claude_home, S1, [user(T0)])
    local = MachineConfig.model_validate(
        {"name": "dev", "transport": "local", "sessions": ["fpga"], "claudeHome": str(claude_home)}
    )
    t = LocalTransport(local, {"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert "--allow-usage" in t.policy_args()
    result = await t.call("usage", ["0", "512"], timeout_seconds=30, stdin=b"[]")
    assert isinstance(result.data, UsageResponse), result
    assert [f.id for f in result.data.batch.files] == [S1]
    off = LocalTransport(local.model_copy(update={"allow_usage": False}), {"PATH": os.environ["PATH"]})
    refused = await off.call("usage", ["0", "512"], timeout_seconds=30, stdin=b"[]")
    assert refused.detail == "not_allowed"
