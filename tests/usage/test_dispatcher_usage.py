"""The dispatcher's `usage` verb: policy, request validation, the transcript parser and what leaves
the machine (no text, no paths)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import DISPATCHER
from tests.usage.helpers import MIN, S1, S2, S3, SECRET_TEXT, T0, assistant, tool_result, user, write_transcript
from voice_coding_mcp.remote import dispatcher as d

FORCED = ["--sessions", "fpga", "--allow-usage"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "remote-home"
    (path / ".claude" / "projects").mkdir(parents=True)
    return path


def run(home: Path, command: str, stdin: bytes = b"", argv: list[str] | None = None) -> list[dict[str, Any]]:
    env = {"HOME": str(home), "SSH_ORIGINAL_COMMAND": command, "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    return list(d.respond(argv if argv is not None else FORCED, env, stdin))


def claude(home: Path) -> Path:
    return home / ".claude"


def test_usage_needs_allow_usage(home: Path) -> None:
    write_transcript(claude(home), S1, [user(T0)])
    assert run(home, "switchboard-remote usage 0 512", argv=["--sessions", "fpga"]) == [
        {"protocol": 1, "ok": False, "error": "not_allowed"}
    ]


@pytest.mark.parametrize(
    "command",
    [
        "switchboard-remote usage",
        "switchboard-remote usage 0",
        "switchboard-remote usage 0 0",
        "switchboard-remote usage 0 2049",
        "switchboard-remote usage -1 512",
        "switchboard-remote usage 0 512 extra",
        "switchboard-remote usage 1.5 512",
        "switchboard-remote usage 0 abc",
    ],
)
def test_bad_usage_requests(home: Path, command: str) -> None:
    assert run(home, command) == [{"protocol": 1, "ok": False, "error": "bad_request"}]


@pytest.mark.parametrize(
    "stdin",
    [
        b"{}",
        b"not json",
        b'[{"id": "x", "size": 1, "mtime": 1}]',
        b'[{"id": "%s", "size": "1", "mtime": 1}]' % S1.encode(),
        b"[1]",
        b"[" + b" " * (d.MAX_USAGE_KNOWN_BYTES + 1) + b"]",
    ],
)
def test_bad_known_lists(home: Path, stdin: bytes) -> None:
    assert run(home, "switchboard-remote usage 0 512", stdin) == [{"protocol": 1, "ok": False, "error": "bad_request"}]


def test_records_carry_no_text_and_no_paths(home: Path) -> None:
    write_transcript(
        claude(home),
        S1,
        [
            {"type": "summary", "summary": SECRET_TEXT},
            user(T0),
            assistant(T0 + MIN, "msg_a", tokens=(1, 2, 3, 4)),
            tool_result(T0 + 2 * MIN),
            assistant(T0 + 3 * MIN, "msg_b", tokens=(5, 6, 7, 8), cache_1h=9, model="claude-sonnet-4-6"),
            user(T0 + 4 * MIN, isMeta=True),
            user(
                T0 + 5 * MIN,
                text=SECRET_TEXT,
                message={"role": "user", "content": [{"type": "text", "text": SECRET_TEXT}]},
            ),
        ],
    )
    out = run(home, "switchboard-remote usage 0 512")
    assert out[0] == {"protocol": 1, "ok": True, "stream": "usage"}
    header, *records, done = out[1:]
    assert header["file"] == S1
    assert header["project"] == "fpgadc"
    assert header["records"] == 4
    assert records == [
        {"t": T0, "k": "u"},
        {
            "t": T0 + MIN,
            "k": "a",
            "m": "claude-opus-4-8",
            "id": "msg_a",
            "rq": "req_1",
            "in": 1,
            "out": 2,
            "cw5": 3,
            "cw1": 0,
            "cr": 4,
        },
        {
            "t": T0 + 3 * MIN,
            "k": "a",
            "m": "claude-sonnet-4-6",
            "id": "msg_b",
            "rq": "req_1",
            "in": 5,
            "out": 6,
            "cw5": 7,
            "cw1": 9,
            "cr": 8,
        },
        {"t": T0 + 5 * MIN, "k": "u"},
    ]
    assert done == {"done": True, "gone": []}
    text = json.dumps(out)
    assert SECRET_TEXT not in text
    assert "/home/USER" not in text
    assert "notreal" not in text


def test_project_is_the_latest_main_cwd_basename(home: Path) -> None:
    write_transcript(
        claude(home),
        S1,
        [
            user(T0, cwd="/home/USER/projects/old"),
            user(T0 + MIN, cwd="/home/USER/projects/new-name/"),
            assistant(T0 + 2 * MIN, cwd="/home/USER/projects/elsewhere", isSidechain=True),
        ],
    )
    header = run(home, "switchboard-remote usage 0 512")[1]
    assert header["project"] == "new-name"


def test_sidechain_tokens_count_but_sidechain_prompts_do_not(home: Path) -> None:
    write_transcript(claude(home), S1, [user(T0, isSidechain=True), assistant(T0 + MIN, isSidechain=True)])
    records = run(home, "switchboard-remote usage 0 512")[2:-1]
    assert [r["k"] for r in records] == ["a"]


def test_malformed_and_overlong_lines_are_skipped(home: Path) -> None:
    long_line = json.dumps(assistant(T0, "msg_long"))[:-1] + ', "pad": "' + "x" * (d.MAX_USAGE_LINE_BYTES + 10) + '"}'
    write_transcript(
        claude(home),
        S1,
        [
            "{not json",
            "[1, 2]",
            long_line,
            json.dumps(assistant(T0 + MIN, "msg_ok")),
            json.dumps({**user(T0 + 2 * MIN), "timestamp": "yesterday"}),
            json.dumps({**user(T0 + 2 * MIN), "timestamp": "2026-02-30T00:00:00Z"}),
        ],
    )
    records = run(home, "switchboard-remote usage 0 512")[2:-1]
    assert [r.get("id") for r in records] == ["msg_ok"]


def test_cache_creation_without_split_counts_as_5m(home: Path) -> None:
    write_transcript(claude(home), S1, [assistant(T0, tokens=(0, 0, 70, 0))])
    record = run(home, "switchboard-remote usage 0 512")[2]
    assert (record["cw5"], record["cw1"]) == (70, 0)


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2025-11-24T16:00:00.000Z", T0),
        ("2025-11-24T16:00:00Z", T0),
        ("2025-11-24T16:00:00.5Z", T0 + 500),
        ("2025-11-24T16:00:00.123456789Z", T0 + 123),
        ("2025-11-24T18:00:00+02:00", T0),
        ("2025-11-24T11:00:00-0500", T0),
        ("2025-11-24T16:00:00", T0),
        ("2025-11-24", None),
        ("", None),
        (12, None),
    ],
)
def test_parse_timestamp(stamp: object, expected: int | None) -> None:
    assert d.parse_timestamp(stamp) == expected


def test_negative_and_odd_counts_are_clamped(home: Path) -> None:
    line = assistant(T0)
    line["message"]["usage"] = {
        "input_tokens": -5,
        "output_tokens": True,
        "cache_read_input_tokens": 10**20,
        "cache_creation_input_tokens": "9",
    }
    write_transcript(claude(home), S1, [line])
    record = run(home, "switchboard-remote usage 0 512")[2]
    assert (record["in"], record["out"], record["cr"], record["cw5"]) == (0, 0, d.MAX_TOKENS, 0)


def test_hostile_ids_and_models_are_dropped_or_cleaned(home: Path) -> None:
    line = assistant(T0, msg_id="msg x;rm -rf", request="r" * 100, model="claude\x1b[31m-opus\u202e")
    write_transcript(claude(home), S1, [line])
    record = run(home, "switchboard-remote usage 0 512")[2]
    assert record["id"] is None
    assert record["rq"] is None
    assert record["m"] == "claude[31m-opus"


def test_known_unchanged_files_are_not_sent_and_missing_ones_are_gone(home: Path) -> None:
    path = write_transcript(claude(home), S1, [user(T0)], mtime_ms=T0)
    write_transcript(claude(home), S2, [user(T0)], mtime_ms=T0)
    size = os.path.getsize(path)
    known = json.dumps([{"id": S1, "size": size, "mtime": T0}, {"id": S3, "size": 1, "mtime": 1}]).encode()
    out = run(home, "switchboard-remote usage 0 512", known)
    files = [line["file"] for line in out if "file" in line]
    assert files == [S2]
    assert out[-1] == {"done": True, "gone": [S3]}


def test_changed_known_files_are_sent_again(home: Path) -> None:
    path = write_transcript(claude(home), S1, [user(T0)], mtime_ms=T0)
    known = json.dumps([{"id": S1, "size": os.path.getsize(path) - 1, "mtime": T0}]).encode()
    assert [line["file"] for line in run(home, "switchboard-remote usage 0 512", known) if "file" in line] == [S1]


def test_files_older_than_since_are_not_read(home: Path) -> None:
    write_transcript(claude(home), S1, [user(T0)], mtime_ms=T0)
    write_transcript(claude(home), S2, [user(T0)], mtime_ms=T0 + 10_000)
    out = run(home, f"switchboard-remote usage {(T0 + 5_000) // 1000} 512")
    assert [line["file"] for line in out if "file" in line] == [S2]


def test_large_files_are_skipped_and_reported(home: Path) -> None:
    write_transcript(claude(home), S1, [user(T0, text="x" * (1024 * 1024 + 10))], mtime_ms=T0)
    out = run(home, "switchboard-remote usage 0 1")
    assert out[1] == {
        "file": S1,
        "size": os.path.getsize(claude(home) / "projects" / "-home-USER-projects-fpgadc" / f"{S1}.jsonl"),
        "mtime": T0,
        "skipped": True,
    }
    assert out[2]["done"] is True


def test_only_top_level_transcripts_no_symlinks_no_other_names(home: Path, tmp_path: Path) -> None:
    write_transcript(claude(home), S1, [user(T0)])
    project = claude(home) / "projects" / "-home-USER-projects-fpgadc"
    (project / S1 / "subagents").mkdir(parents=True)
    (project / S1 / "subagents" / f"{S2}.jsonl").write_text(json.dumps(user(T0)) + "\n", encoding="utf-8")
    (project / "notes.jsonl").write_text(json.dumps(user(T0)) + "\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(user(T0)) + "\n", encoding="utf-8")
    (project / f"{S3}.jsonl").symlink_to(outside)
    linked = tmp_path / "linked-project"
    linked.mkdir()
    (linked / f"{S2}.jsonl").write_text(json.dumps(user(T0)) + "\n", encoding="utf-8")
    (claude(home) / "projects" / "linked").symlink_to(linked)
    out = run(home, "switchboard-remote usage 0 512")
    assert [line["file"] for line in out if "file" in line] == [S1]


def test_claude_home_follows_the_policy(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "other-claude"
    write_transcript(other, S2, [user(T0)])
    write_transcript(claude(home), S1, [user(T0)])
    out = run(home, "switchboard-remote usage 0 512", argv=[*FORCED, "--claude-home", str(other)])
    assert [line["file"] for line in out if "file" in line] == [S2]


def test_parse_transcript_reads_at_most_the_limit() -> None:
    first = json.dumps(user(T0)).encode() + b"\n"
    second = json.dumps(user(T0 + MIN)).encode() + b"\n"
    project, records = d.parse_transcript(io.BytesIO(first + second), len(first))
    assert project == "fpgadc"
    assert records == [{"t": T0, "k": "u"}]


def test_main_streams_json_lines(home: Path) -> None:
    write_transcript(claude(home), S1, [user(T0), assistant(T0 + MIN)])
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "SSH_ORIGINAL_COMMAND": "switchboard-remote usage 0 512",
        "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22",
    }
    done = subprocess.run(
        [sys.executable, "-I", DISPATCHER, *FORCED], input=b"[]", capture_output=True, env=env, check=True, timeout=30
    )
    lines = [json.loads(line) for line in done.stdout.decode().splitlines()]
    assert lines[0] == {"protocol": 1, "ok": True, "stream": "usage"}
    assert lines[1]["file"] == S1
    assert lines[-1] == {"done": True, "gone": []}
    assert len(lines) == 5
