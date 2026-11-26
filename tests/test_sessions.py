"""Listing, matching and reading the owner's Claude Code sessions."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.session_fixtures import (
    assistant_text,
    claude_home,
    json_line,
    peer_message,
    prompt,
    write_live_session,
    write_transcript,
)
from voice_coding_mcp import sessions as sessions_module
from voice_coding_mcp.projects import MatchFailed, MatchOk
from voice_coding_mcp.sessions import (
    ListSessionsOptions,
    LiveSession,
    ReplyScanner,
    ReplyWatchOptions,
    ReplyWatchResult,
    SessionInfo,
    is_session_id,
    list_sessions,
    match_session,
    read_live_sessions,
    read_transcript_meta,
    relay_input,
    unique_by_name,
    watch_for_reply,
)

IDS = {
    "titled": "11111111-0000-4000-8000-000000000001",
    "ai_titled": "11111111-0000-4000-8000-000000000002",
    "untitled": "11111111-0000-4000-8000-000000000003",
    "headless": "11111111-0000-4000-8000-000000000004",
    "outside": "11111111-0000-4000-8000-000000000005",
    "old": "11111111-0000-4000-8000-000000000006",
    "live": "11111111-0000-4000-8000-000000000007",
    "duplicate_old": "11111111-0000-4000-8000-000000000008",
    "big": "11111111-0000-4000-8000-000000000009",
    "relocated": "11111111-0000-4000-8000-00000000000a",
}


@dataclass(frozen=True)
class Env:
    base: str
    home: str
    root: str


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    base = str(tmp_path_factory.mktemp("vcm-sessions").resolve())
    root = os.path.join(base, "projects")
    outside = os.path.join(base, "elsewhere")
    for directory in ("alpha", "beta/sub", "assistant"):
        os.makedirs(os.path.join(root, directory), exist_ok=True)
    os.makedirs(outside, exist_ok=True)
    home = claude_home(base)
    alpha = os.path.join(root, "alpha")
    now = time.time() * 1000
    write_transcript(
        home,
        IDS["titled"],
        [
            prompt(alpha, "Fix the login bug"),
            {"type": "custom-title", "customTitle": "Old title", "sessionId": IDS["titled"]},
            assistant_text(alpha, "Done."),
            {"type": "custom-title", "customTitle": "Přihlášení a chyby", "sessionId": IDS["titled"]},
        ],
        now - 60_000,
    )
    write_transcript(
        home,
        IDS["ai_titled"],
        [
            prompt(os.path.join(root, "beta", "sub"), "Write docs"),
            {"type": "ai-title", "aiTitle": "Documentation pass", "sessionId": IDS["ai_titled"]},
        ],
        now - 120_000,
    )
    write_transcript(
        home,
        IDS["untitled"],
        [
            prompt(alpha, "<command-name>/clear</command-name>"),
            {**prompt(alpha, "A task notification"), "turnOrigin": "task_notification"},
            prompt(alpha, "Jak   rychle běží\nbuild na CI serveru, když se změní jen dokumentace?"),
        ],
        now - 180_000,
    )
    write_transcript(home, IDS["headless"], [{**prompt(alpha, "run -p"), "entrypoint": "sdk-cli"}], now - 30_000)
    write_transcript(
        home,
        IDS["outside"],
        [
            prompt(outside, "secret project"),
            {"type": "custom-title", "customTitle": "Outside", "sessionId": IDS["outside"]},
        ],
        now,
    )
    write_transcript(
        home,
        IDS["old"],
        [prompt(alpha, "ancient"), {"type": "custom-title", "customTitle": "Ancient", "sessionId": IDS["old"]}],
        now - 10 * 86_400_000,
    )
    assistant = os.path.join(root, "assistant")
    write_transcript(
        home,
        IDS["live"],
        [
            prompt(assistant, "Render the room"),
            {"type": "custom-title", "customTitle": "billing-fix", "sessionId": IDS["live"]},
        ],
        now - 600_000,
    )
    write_transcript(
        home,
        IDS["duplicate_old"],
        [
            prompt(assistant, "Older one"),
            {"type": "custom-title", "customTitle": "billing-fix", "sessionId": IDS["duplicate_old"]},
        ],
        now - 5000,
    )
    write_transcript(
        home,
        IDS["relocated"],
        [
            prompt(outside, "moved later"),
            {"type": "relocated", "relocatedCwd": os.path.join(root, "beta"), "sessionId": IDS["relocated"]},
            {"type": "custom-title", "customTitle": "Moved in", "sessionId": IDS["relocated"]},
        ],
        now - 240_000,
    )
    # Big transcript: title at the very end, first prompt at the start, a huge line in between.
    filler = assistant_text(alpha, "x" * 300_000)
    write_transcript(
        home,
        IDS["big"],
        [
            prompt(alpha, "Start of a long session"),
            filler,
            filler,
            filler,
            {"type": "custom-title", "customTitle": "Long one", "sessionId": IDS["big"]},
        ],
        now - 300_000,
    )
    # Registry: one live entry (this test process is alive), one stale (dead pid).
    sessions_dir = os.path.join(home, "sessions")
    live_entry = {
        "pid": os.getpid(),
        "sessionId": IDS["live"],
        "cwd": assistant,
        "messagingSocketPath": "/tmp/cc-socks/test.sock",
        "status": "idle",
        "name": "billing-fix",
    }
    Path(sessions_dir, f"{os.getpid()}.json").write_text(json.dumps(live_entry))
    stale = {"pid": 999999, "sessionId": IDS["titled"], "messagingSocketPath": "/tmp/x.sock", "status": "idle"}
    Path(sessions_dir, "999999.json").write_text(json.dumps(stale))
    Path(sessions_dir, "999999.secret.key").write_text("not json")
    return Env(base=base, home=home, root=root)


async def test_lists_recent_sessions_inside_the_roots(env: Env) -> None:
    sessions = await list_sessions(ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7))
    by_id = {s.id: s for s in sessions}
    assert [s.id for s in sessions] == [
        IDS["duplicate_old"],
        IDS["titled"],
        IDS["ai_titled"],
        IDS["untitled"],
        IDS["relocated"],
        IDS["big"],
        IDS["live"],
    ]
    assert by_id[IDS["titled"]].name == "Přihlášení a chyby"
    assert by_id[IDS["titled"]].project == "alpha"
    assert by_id[IDS["titled"]].live is None, "stale registry entry ignored"
    assert by_id[IDS["ai_titled"]].name == "Documentation pass"
    assert by_id[IDS["ai_titled"]].project == "beta"
    assert by_id[IDS["untitled"]].name == "alpha: Jak rychle běží build na CI serveru,…"
    assert by_id[IDS["untitled"]].titled is False
    assert by_id[IDS["relocated"]].project == "beta"
    assert by_id[IDS["big"]].name == "Long one"
    live = by_id[IDS["live"]].live
    assert live == LiveSession(
        pid=os.getpid(),
        socket_path="/tmp/cc-socks/test.sock",
        status="idle",
        name="billing-fix",
        cwd=live.cwd if live is not None else None,
    )
    assert live is not None
    assert live.cwd is not None
    assert live.cwd.endswith(os.sep + "assistant")


async def test_leaves_out_excluded_ids_and_honours_the_day_limit(env: Env) -> None:
    sessions = await list_sessions(
        ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7, exclude_ids={IDS["titled"]})
    )
    assert not any(s.id == IDS["titled"] for s in sessions)
    every = await list_sessions(ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=30))
    assert any(s.id == IDS["old"] for s in every)


async def test_skips_transcripts_whose_name_is_not_a_session_id(env: Env) -> None:
    alpha = os.path.join(env.root, "alpha")
    for bad in ["--help", "not-a-uuid", "11111111-0000-4000-8000-00000000000g", "x" + IDS["titled"]]:
        write_transcript(env.home, bad, [prompt(alpha, "odd file")])
    sessions = await list_sessions(ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7))
    assert all(is_session_id(s.id) for s in sessions)
    assert len(sessions) == 7
    assert is_session_id(IDS["titled"].upper())


async def test_returns_nothing_for_a_missing_claude_home(env: Env) -> None:
    options = ListSessionsOptions(claude_home=os.path.join(env.base, "none"), project_roots=[env.root], days=7)
    assert await list_sessions(options) == []


async def test_concurrent_scans_share_the_metadata_cache(env: Env) -> None:
    options = ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7)
    expected = await list_sessions(options)
    for _ in range(5):
        sessions_module._meta_cache.clear()  # every scan reads and writes the cache at once
        results = await asyncio.gather(*(list_sessions(options) for _ in range(4)))
        assert all(result == expected for result in results)
    assert sessions_module._meta_cache


async def test_lists_with_a_registry_the_caller_already_read(env: Env) -> None:
    options = ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7, live={})
    sessions = await list_sessions(options)
    assert all(s.live is None for s in sessions)


async def test_matches_spoken_names_loosely(env: Env) -> None:
    sessions = await list_sessions(ListSessionsOptions(claude_home=env.home, project_roots=[env.root], days=7))

    def name(spoken: str) -> str:
        m = match_session(sessions, spoken)
        return m.item.id if isinstance(m, MatchOk) else m.reason

    assert name("prihlaseni a chyby") == IDS["titled"]
    assert name("přihlášení") == IDS["titled"]
    assert name("billing fix") == IDS["live"], "running duplicate wins over a newer one"
    assert name("documentation") == IDS["ai_titled"]
    assert name("Long on") == IDS["big"]
    assert name("alpha") == IDS["titled"], "exact project name: its latest session"
    assert name(IDS["big"]) == IDS["big"]
    assert name(IDS["big"][:8]) == "not_found", "shared id prefix: no id match"
    assert name("kubernetes") == "not_found"
    assert name("../alpha") == "not_found"
    deduped = [s.id for s in unique_by_name(sessions)]
    assert IDS["live"] in deduped
    assert IDS["duplicate_old"] not in deduped


def test_reports_ambiguous_names() -> None:
    def make(session_id: str, name: str) -> SessionInfo:
        return SessionInfo(
            id=session_id,
            name=name,
            titled=True,
            project="p",
            cwd="/p",
            last_activity_ms=1,
            transcript_path="/x",
            live=None,
        )

    m = match_session([make("a1", "Render kitchen"), make("a2", "Render garden")], "render")
    assert isinstance(m, MatchFailed)
    assert m.reason == "ambiguous"


def test_a_session_or_project_name_counts_only_as_whole_words_of_the_query() -> None:
    def make(session_id: str, name: str, project: str) -> SessionInfo:
        return SessionInfo(
            id=session_id,
            name=name,
            titled=True,
            project=project,
            cwd=f"/{project}",
            last_activity_ms=1,
            transcript_path="/x",
            live=None,
        )

    sessions = [make("b1", "app", "web"), make("b2", "api", "tools")]

    def name(spoken: str) -> str:
        m = match_session(sessions, spoken)
        return m.item.id if isinstance(m, MatchOk) else m.reason

    assert name("the api server") == "b2"
    assert name("web please") == "b1", "project name as a whole word"
    for spoken in ["mapping tool", "happy", "website redesign", "rapid prototype"]:
        assert name(spoken) == "not_found", spoken


def test_read_transcript_meta_reads_head_and_tail_of_a_big_file(env: Env) -> None:
    """Python-only check of the partial read: cwd from an oversized line, first prompt from the head."""
    path = os.path.join(env.home, "projects", "-encoded-cwd", f"{IDS['big']}.jsonl")
    meta = read_transcript_meta(path)
    assert meta.first_prompt == "Start of a long session"
    assert meta.custom_title == "Long one"
    assert meta.cwd == os.path.join(env.root, "alpha")
    assert meta.last_entrypoint == "claude-desktop"


FROM = "vcm-relay-7"


def _write(path: str, text: str, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def test_finds_the_relayed_message_then_the_end_of_turn_text() -> None:
    scan = ReplyScanner(FROM)
    scan.line(json_line(assistant_text("/p", "before the message")))
    scan.line(json_line(peer_message("/p", "other-sender", "not ours")))
    assert scan.arrived is False
    scan.line(json_line(peer_message("/p", FROM, "ahoj")))
    assert scan.arrived is True
    scan.line(
        json_line(
            {
                **assistant_text("/p", "Thinking aloud"),
                "message": {
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [{"type": "text", "text": "Checking."}],
                },
            }
        )
    )
    assert scan.finished is False
    scan.line(json_line(assistant_text("/p", "Přijato.")))
    assert scan.finished is True
    assert scan.best_reply == "Přijato."


def test_prefers_what_the_target_sent_back_with_send_message() -> None:
    scan = ReplyScanner(FROM)
    scan.line(json_line(peer_message("/p", FROM, "ahoj")))
    tool_use = {"type": "tool_use", "name": "SendMessage", "input": {"to": FROM, "message": "The tests pass."}}
    scan.line(
        json_line(
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [tool_use]}}
        )
    )
    scan.line(json_line(assistant_text("/p", "Replied.")))
    assert scan.best_reply == "The tests pass."


async def test_follows_a_growing_transcript_until_the_reply(real_tmp: Path) -> None:
    file = str(real_tmp / "watch.jsonl")
    _write(file, json_line(peer_message("/p", FROM, "stale, before the offset")) + "\n")
    offset = os.stat(file).st_size
    arrived = 0

    def on_arrived() -> None:
        nonlocal arrived
        arrived += 1

    def append(text: str) -> None:
        _write(file, text, "a")

    watching = asyncio.create_task(
        watch_for_reply(
            ReplyWatchOptions(
                transcript_path=file, offset=offset, from_name=FROM, timeout_ms=5000, poll_ms=20, on_arrived=on_arrived
            )
        )
    )
    await asyncio.sleep(0.06)
    append(json_line(assistant_text("/p", "Unrelated end of turn")) + "\n")
    append(json_line(peer_message("/p", FROM, "ahoj")) + "\n" + json_line(assistant_text("/p", "Hot"))[:20])
    await asyncio.sleep(0.06)
    append(json_line(assistant_text("/p", "Hotovo."))[20:] + "\n")
    result = await watching
    assert result == ReplyWatchResult(arrived=True, finished=True, reply="Hotovo.", aborted=False)
    assert arrived == 1


async def test_times_out_when_the_message_never_arrives_and_stops_on_abort(real_tmp: Path) -> None:
    file = str(real_tmp / "quiet.jsonl")
    _write(file, "")
    quiet = await watch_for_reply(
        ReplyWatchOptions(transcript_path=file, offset=0, from_name=FROM, timeout_ms=100, poll_ms=20)
    )
    assert quiet == ReplyWatchResult(arrived=False, finished=False, reply=None, aborted=False)
    abort = asyncio.Event()
    pending = asyncio.create_task(
        watch_for_reply(
            ReplyWatchOptions(
                transcript_path=file, offset=0, from_name=FROM, timeout_ms=60_000, poll_ms=10_000, abort=abort
            )
        )
    )
    asyncio.get_running_loop().call_later(0.03, abort.set)
    start = time.monotonic()
    assert (await pending).aborted is True
    assert time.monotonic() - start < 2


async def test_a_shrunk_transcript_drops_the_partial_line(real_tmp: Path) -> None:
    file = str(real_tmp / "shrink.jsonl")
    _write(file, "x" * 4000 + "\n" + '{"half a line')  # an incomplete line is kept as partial
    watching = asyncio.create_task(
        watch_for_reply(ReplyWatchOptions(transcript_path=file, offset=0, from_name=FROM, timeout_ms=5000, poll_ms=20))
    )
    await asyncio.sleep(0.06)
    # Rewritten, shorter file: the old partial line must not be glued to the new first line.
    _write(file, json_line(peer_message("/p", FROM, "ahoj")) + "\n" + json_line(assistant_text("/p", "Hotovo.")) + "\n")
    result = await watching
    assert result == ReplyWatchResult(arrived=True, finished=True, reply="Hotovo.", aborted=False)


def test_only_owner_prompts_and_end_of_turn_system_lines_end_the_reply() -> None:
    scan = ReplyScanner(FROM)
    scan.line(json_line(peer_message("/p", FROM, "ahoj")))
    scan.line(
        json_line(
            {
                **assistant_text("/p", "Working."),
                "message": {
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [{"type": "text", "text": "Working."}],
                },
            }
        )
    )
    scan.line(json_line({"type": "system", "subtype": "informational", "content": "x"}))
    scan.line(json_line({"type": "user", "turnOrigin": "peer", "message": {"role": "user", "content": "x"}}))
    scan.line(json_line({**prompt("/p", "sidechain"), "turnOrigin": "human", "isSidechain": True}))
    assert scan.finished is False
    scan.line(json_line({"type": "system", "subtype": "turn_duration"}))
    assert scan.finished is True
    assert scan.best_reply == "Working."
    owner = ReplyScanner(FROM)
    owner.line(json_line(peer_message("/p", FROM, "ahoj")))
    owner.line(json_line({**prompt("/p", "new question"), "turnOrigin": "human"}))
    assert owner.finished is True


async def test_reads_a_big_append_in_bounded_chunks(real_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_module, "REPLY_READ_CHUNK", 100)
    file = str(real_tmp / "big.jsonl")
    filler = "\n".join(json_line(assistant_text("/p", f"noise {i}")) for i in range(20))
    _write(file, filler + "\n" + json_line(peer_message("/p", FROM, "ahoj")) + "\n")
    _write(file, json_line(assistant_text("/p", "x" * 500)) + "\n", "a")
    result = await watch_for_reply(
        ReplyWatchOptions(transcript_path=file, offset=0, from_name=FROM, timeout_ms=5000, poll_ms=10_000)
    )
    assert result == ReplyWatchResult(arrived=True, finished=True, reply="x" * 500, aborted=False)


def test_frames_the_relay_input_with_the_owner_request_and_boundary_lines() -> None:
    text = relay_input("billing-fix", "line one\nline two", "MESSAGE abc")
    assert 'send the message below, unchanged, to my Claude Code session "billing-fix"' in text
    assert text.endswith("----- MESSAGE abc -----\nline one\nline two\n----- MESSAGE abc -----")


def test_reads_linux_registry_fields(tmp_path: Path) -> None:
    """Linux-shaped entries: `tmux` for a session started inside tmux, `entrypoint`, `cwd`, a
    `/run/user` socket path and `waiting` with `waitingFor`."""
    home = claude_home(str(tmp_path))
    sid = "44444444-0000-4000-8000-000000000001"
    write_live_session(
        home,
        os.getpid(),
        {
            "sessionId": sid,
            "cwd": "/home/USER/projects/alpha",
            "tmux": "0:@0.%3",
            "entrypoint": "cli",
            "status": "waiting",
            "waitingFor": "permission prompt",
            "messagingSocketPath": "/run/user/1000/cc-socks/1.sock",
        },
    )
    live = read_live_sessions(home)[sid]
    assert (live.cwd, live.tmux, live.entrypoint) == ("/home/USER/projects/alpha", "0:@0.%3", "cli")
    assert (live.status, live.waiting_for, live.socket_path) == (
        "waiting",
        "permission prompt",
        "/run/user/1000/cc-socks/1.sock",
    )
