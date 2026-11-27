"""Dispatcher 0.10.0 additions to `usage`: agent transcripts, entrypoints, work projects, prompt
origins and sidechain marks, and how the hub reads and caches them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.usage.helpers import MIN, S1, S2, SECRET_TEXT, T0, assistant, user, write_transcript
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.usage.cache import MachineCache, apply_batch, load_cache, save_cache
from voice_coding_mcp.usage.records import UsageStreamError, parse_documents, scan_local

FORCED = ["--sessions", "fpga", "--allow-usage"]
PROJECT_DIR = "-home-USER-projects-fpgadc"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "remote-home"
    (path / ".claude" / "projects").mkdir(parents=True)
    return path


def run(home: Path) -> list[dict[str, Any]]:
    env = {"HOME": str(home), "SSH_ORIGINAL_COMMAND": "switchboard-remote usage 0 512"}
    env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 22"
    return list(d.respond(FORCED, env, b""))


def write_agent(home: Path, session: str, agent: str, lines: list[dict[str, Any]], workflow: str | None = None) -> Path:
    directory = home / ".claude" / "projects" / PROJECT_DIR / session / "subagents"
    if workflow is not None:
        directory = directory / "workflows" / workflow
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def headers(out: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {line["file"]: line for line in out if "file" in line}


def test_version_is_0_10_0() -> None:
    assert d.VERSION == "0.10.0"


def test_agent_transcripts_are_listed_with_session_prefixed_ids(home: Path, tmp_path: Path) -> None:
    write_transcript(home / ".claude", S1, [user(T0, entrypoint="cli")])
    write_agent(home, S1, "A1b2", [user(T0, isSidechain=True), assistant(T0 + MIN, isSidechain=True)])
    write_agent(home, S1, "c3", [assistant(T0 + 2 * MIN, isSidechain=True)], workflow="wf_1234-abc")
    session_dir = home / ".claude" / "projects" / PROJECT_DIR / S1 / "subagents"
    (session_dir / "notes.jsonl").write_text(json.dumps(user(T0)) + "\n", encoding="utf-8")
    (session_dir / "agent-x.meta.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(assistant(T0)) + "\n", encoding="utf-8")
    (session_dir / "agent-linked.jsonl").symlink_to(outside)
    out = run(home)
    found = headers(out)
    assert sorted(found) == sorted([S1, f"{S1}:a1b2", f"{S1}:c3"])
    agent = found[f"{S1}:a1b2"]
    assert agent["project"] == "fpgadc"
    assert agent["records"] == 1
    records = out[out.index(agent) + 1 : out.index(agent) + 2]
    assert records[0]["sc"] == 1


def test_known_agent_ids_are_accepted_and_reported_gone(home: Path) -> None:
    write_transcript(home / ".claude", S1, [user(T0)])
    path = write_agent(home, S1, "a1", [assistant(T0, isSidechain=True)])
    first = headers(run(home))
    known = [{"id": key, "size": value["size"], "mtime": value["mtime"]} for key, value in first.items()]
    path.unlink()
    env = {"HOME": str(home), "SSH_ORIGINAL_COMMAND": "switchboard-remote usage 0 512"}
    env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 22"
    out = list(d.respond(FORCED, env, json.dumps(known).encode()))
    assert out[-1] == {"done": True, "gone": [f"{S1}:a1"]}


def test_entrypoint_origin_and_work_projects(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "work" / "shrike4-rtl"
    (repo / "model").mkdir(parents=True)
    (repo / ".git").mkdir()
    worktree = tmp_path / "work" / "switchboard-timesheet"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    write_transcript(
        home / ".claude",
        S1,
        [
            user(T0, cwd=str(repo / "model"), entrypoint="claude-vscode", origin={"kind": "human"}),
            user(T0 + MIN, cwd=str(repo / "model"), origin={"kind": "task-notification"}),
            user(T0 + 2 * MIN, cwd=str(repo / "model"), isCompactSummary=True),
            assistant(T0 + 3 * MIN, "m1", cwd=str(worktree)),
            assistant(T0 + 4 * MIN, "m2", cwd=str(repo / "model")),
        ],
    )
    out = run(home)
    header = headers(out)[S1]
    assert header["ep"] == "claude-vscode"
    assert header["project"] == "model"
    assert header["wp"] == "shrike4-rtl"
    records = out[2:-1]
    assert records[0] == {"t": T0, "k": "u"}
    assert records[1] == {"t": T0 + MIN, "k": "u", "o": "n"}
    assert records[2] == {"t": T0 + 2 * MIN, "k": "u", "o": "n"}
    assert records[3]["p"] == "switchboard-timesheet"
    assert "p" not in records[4]
    text = json.dumps(out)
    assert str(tmp_path) not in text
    assert SECRET_TEXT not in text


def test_work_project_without_a_repository_is_the_basename() -> None:
    roots: dict[str, str] = {}
    assert d.work_project("/nonexistent-root-xyz/a/b/", roots) == "b"
    assert roots == {"/nonexistent-root-xyz/a/b": "b"}


def test_odd_entrypoints_are_left_out(home: Path) -> None:
    write_transcript(home / ".claude", S1, [user(T0, entrypoint="bad value with spaces")])
    assert "ep" not in headers(run(home))[S1]


def test_hub_reads_the_new_fields_and_caches_them(home: Path, tmp_path: Path) -> None:
    write_transcript(
        home / ".claude",
        S1,
        [
            user(T0, entrypoint="sdk-cli", origin={"kind": "task-notification"}),
            assistant(T0 + MIN, cwd="/elsewhere/other"),
        ],
    )
    write_agent(home, S1, "a1", [assistant(T0 + 2 * MIN, "m9", isSidechain=True)])
    batch = scan_local(str(home / ".claude"), 0, 512 * 1024 * 1024, {})
    files = {t.id: t for t in batch.files}
    main, agent = files[S1], files[f"{S1}:a1"]
    assert main.entrypoint == "sdk-cli"
    assert not main.is_agent_file
    assert agent.is_agent_file
    assert main.project == "other"
    assert main.records[0].other_origin
    assert main.records[0].project == "fpgadc"
    assert main.records[1].project is None
    assert agent.records[0].sidechain
    cache = MachineCache(machine="dev")
    apply_batch(cache, batch, T0 + DAY)
    state = tmp_path / "state"
    save_cache(str(state), cache)
    again = load_cache(str(state), "dev")
    assert again.files[S1].entrypoint == "sdk-cli"
    assert again.files[S1].records == main.records
    assert again.files[f"{S1}:a1"].records == agent.records


DAY = 86_400_000


def test_old_cache_versions_are_read_as_empty(tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "dev.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
    assert load_cache(str(tmp_path), "dev").refreshed_ms is None


@pytest.mark.parametrize(
    "line",
    [
        {"t": 1, "k": "u", "o": "x"},
        {"t": 1, "k": "a", "sc": 2},
        {"t": 1, "k": "a", "p": ""},
        {"t": 1, "k": "a", "p": "\u0007"},
    ],
)
def test_hub_rejects_bad_new_fields(line: dict[str, Any]) -> None:
    with pytest.raises(UsageStreamError):
        parse_documents([{"file": S2, "size": 1, "mtime": 1, "project": "x", "records": 1}, line, {"done": True}])


@pytest.mark.parametrize("entrypoint", ["", "a b", 5])
def test_hub_rejects_bad_entrypoints(entrypoint: object) -> None:
    with pytest.raises(UsageStreamError):
        parse_documents(
            [{"file": S2, "size": 1, "mtime": 1, "project": "x", "records": 0, "ep": entrypoint}, {"done": True}]
        )


@pytest.mark.parametrize("file_id", [f"{S2}:", f"{S2}:ABC", f"{S2}:a/b", f"x:{S2}"])
def test_hub_rejects_bad_agent_ids(file_id: str) -> None:
    with pytest.raises(UsageStreamError):
        parse_documents([{"file": file_id, "size": 1, "mtime": 1, "project": "x", "records": 0}, {"done": True}])
