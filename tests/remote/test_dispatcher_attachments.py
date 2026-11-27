"""The dispatcher's `attachments` and `attachment` verbs: the double opt-in's machine side, the
outbox rules (plain files directly in it, no links, no subdirectories), chunked answers in forced
and serve mode, and the hub's parsing of those answers (a lying dispatcher gets `bad_response`)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.remote.agent_helpers import ServeSession
from tests.remote.helpers import RemoteWorld
from tests.remote.media_fixtures import GPS, SVG, exif_segment, jpeg, mp4, png, wav
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.protocol import AttachmentResponse, AttachmentsResponse, ResponseError, parse_response

FORCED = ["--sessions", "fpga", "--allow-attachments"]


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    return RemoteWorld.create(tmp_path)


def outbox(world: RemoteWorld) -> Path:
    path = Path(world.home) / d.OUTBOX_DIR
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)
    return path


def put(world: RemoteWorld, name: str, data: bytes, mtime: int) -> Path:
    path = outbox(world) / name
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def documents(world: RemoteWorld, argv: list[str], command: str) -> list[dict[str, Any]]:
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22", "SSH_ORIGINAL_COMMAND": command}
    return list(d.respond(argv, env))


def first(world: RemoteWorld, argv: list[str], command: str) -> dict[str, Any]:
    return documents(world, argv, command)[0]


def stdout(docs: list[dict[str, Any]]) -> bytes:
    return b"\n".join(json.dumps(doc).encode() for doc in docs) + b"\n"


def test_off_without_the_option(world: RemoteWorld) -> None:
    put(world, "shot.png", png(), 1_764_000_000)
    for command in ("switchboard-remote attachments", "switchboard-remote attachment shot.png"):
        assert first(world, ["--sessions", "fpga"], command) == {"protocol": 1, "ok": False, "error": "not_allowed"}
    version = first(world, FORCED, "switchboard-remote version")
    assert version["allowAttachments"] is True
    assert "attachments" in version["verbs"]
    assert "attachment" in version["verbs"]
    assert first(world, ["--sessions", "fpga"], "switchboard-remote version")["allowAttachments"] is False


def test_policy_options_are_validated(world: RemoteWorld) -> None:
    for argv in (
        [*FORCED, "--outbox", "relative/path"],
        [*FORCED, "--outbox", "/a/../b"],
        [*FORCED, "--outbox", "/a b"],
        [*FORCED, "--outbox"],
        [*FORCED, "--allow-attachment"],
    ):
        assert first(world, argv, "switchboard-remote attachments")["error"] == "bad_policy", argv
    policy = Path(world.home) / d.POLICY_FILE
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"sessions": ["fpga"], "allowAttachments": True}), encoding="utf-8")
    put(world, "shot.png", png(), 1_764_000_000)
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    listed: Any = d.handle(["attachments"], env)
    assert [a["name"] for a in listed["attachments"]] == ["shot.png"]
    policy.write_text(json.dumps({"sessions": ["fpga"], "allowAttachments": "yes"}), encoding="utf-8")
    assert d.handle(["attachments"], env)["error"] == "no_policy"


def test_request_arguments(world: RemoteWorld) -> None:
    for command in (
        "switchboard-remote attachments extra",
        "switchboard-remote attachment",
        "switchboard-remote attachment a.png b.png",
        "switchboard-remote attachment .hidden.png",
        "switchboard-remote attachment ../x.png",
        "switchboard-remote attachment " + "a" * 61 + ".png",
    ):
        assert first(world, FORCED, command)["error"] == "bad_request", command


def test_missing_outbox_lists_empty_and_nothing_is_created(world: RemoteWorld) -> None:
    assert first(world, FORCED, "switchboard-remote attachments") == {
        "protocol": 1,
        "ok": True,
        "attachments": [],
        "skipped": 0,
        "outbox": False,
    }
    assert not (Path(world.home) / d.OUTBOX_DIR).exists()
    assert first(world, FORCED, "switchboard-remote attachment shot.png")["error"] == "not_found"


def test_list_is_newest_first_and_only_media(world: RemoteWorld, tmp_path: Path) -> None:
    put(world, "old.png", png(), 1_764_000_000)
    put(world, "new.jpg", jpeg(), 1_764_000_300)
    put(world, "take.wav", wav(), 1_764_000_200)
    put(world, "drawing.png", SVG, 1_764_000_400)
    put(world, "drawing.svg", SVG, 1_764_000_400)
    put(world, "notes.txt", b"hello", 1_764_000_400)
    put(world, "empty.png", b"", 1_764_000_400)
    put(world, ".partial.png", png(), 1_764_000_500)
    box = outbox(world)
    (box / "sub.png").mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    (box / "link.png").symlink_to(outside)
    os.link(outside, box / "hard.png")
    os.mkfifo(box / "pipe.png")
    (box / "a name.png").write_bytes(png())
    listed = first(world, FORCED, "switchboard-remote attachments")
    assert listed["ok"] is True
    assert [(a["name"], a["kind"], a["mime"]) for a in listed["attachments"]] == [
        ("new.jpg", "image", "image/jpeg"),
        ("take.wav", "audio", "audio/wav"),
        ("old.png", "image", "image/png"),
    ]
    assert listed["attachments"][0]["size"] == len(jpeg())
    assert listed["attachments"][0]["mtime"] == 1_764_000_300_000
    # drawing.png, drawing.svg, notes.txt, empty.png, sub.png, link.png, hard.png, pipe.png and
    # "a name.png"; the dot file is not counted.
    assert listed["skipped"] == 9
    parsed = parse_response("attachments", stdout([listed]))
    assert isinstance(parsed, AttachmentsResponse)
    assert parsed.attachments[0].name == "new.jpg"


def test_at_most_fifty_are_listed(world: RemoteWorld) -> None:
    for index in range(55):
        put(world, f"shot{index:02}.png", png(), 1_764_000_000 + index)
    listed = first(world, FORCED, "switchboard-remote attachments")
    assert len(listed["attachments"]) == d.MAX_ATTACHMENTS
    assert listed["attachments"][0]["name"] == "shot54.png"
    assert listed["skipped"] == 5


def test_get_refuses_links_directories_and_other_files(world: RemoteWorld, tmp_path: Path) -> None:
    box = outbox(world)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    (box / "link.png").symlink_to(outside)
    os.link(outside, box / "hard.png")
    (box / "sub.png").mkdir()
    os.mkfifo(box / "pipe.png")
    put(world, "drawing.png", SVG, 1_764_000_000)
    for name in ("link.png", "hard.png", "sub.png", "pipe.png", "missing.png"):
        assert first(world, FORCED, f"switchboard-remote attachment {name}")["error"] == "not_found", name
    assert first(world, FORCED, "switchboard-remote attachment drawing.png")["error"] == "not_media"
    assert first(world, FORCED, "switchboard-remote attachment notes.txt")["error"] == "not_media"


def test_outbox_must_be_a_plain_private_directory(world: RemoteWorld, tmp_path: Path) -> None:
    real = tmp_path / "real-outbox"
    real.mkdir()
    (real / "shot.png").write_bytes(png())
    link = tmp_path / "linked-outbox"
    link.symlink_to(real)
    command = "switchboard-remote attachments"
    assert first(world, [*FORCED, "--outbox", str(link)], command)["error"] == "bad_outbox"
    assert first(world, [*FORCED, "--outbox", str(real / "shot.png")], command)["error"] == "bad_outbox"
    real.chmod(0o775)
    assert first(world, [*FORCED, "--outbox", str(real)], command)["error"] == "bad_outbox"
    real.chmod(0o700)
    listed = first(world, [*FORCED, "--outbox", str(real)], command)
    assert [a["name"] for a in listed["attachments"]] == ["shot.png"]


def test_get_sends_the_cleaned_file_in_chunks(world: RemoteWorld) -> None:
    put(world, "photo.jpg", jpeg(exif_segment()), 1_764_000_000)
    docs = documents(world, FORCED, "switchboard-remote attachment photo.jpg")
    header = docs[0]
    assert header["stream"] == "attachment"
    assert (header["name"], header["kind"], header["mime"], header["chunks"]) == (
        "photo.jpg",
        "image",
        "image/jpeg",
        1,
    )
    data = base64.b64decode(docs[1]["data"])
    assert GPS not in data
    assert data == jpeg()
    assert header["sha256"] == hashlib.sha256(data).hexdigest()
    assert docs[-1] == {"done": True}
    parsed = parse_response("attachment", stdout(docs))
    assert isinstance(parsed, AttachmentResponse)
    assert parsed.data == jpeg()
    # The original file is not changed.
    assert GPS in (outbox(world) / "photo.jpg").read_bytes()


def test_a_large_video_comes_in_several_frames(world: RemoteWorld) -> None:
    video = mp4(mdat=os.urandom(5 * 1024 * 1024 + 123))
    put(world, "clip.mp4", video, 1_764_000_000)
    docs = documents(world, FORCED, "switchboard-remote attachment clip.mp4")
    assert docs[0]["chunks"] == 6
    assert all(len(json.dumps(doc)) < d.MAX_MACHINE_FRAME_BYTES // 2 for doc in docs)
    parsed = parse_response("attachment", stdout(docs))
    assert isinstance(parsed, AttachmentResponse)
    assert len(parsed.data) == len(video)


def test_oversize_files_are_refused_before_reading(world: RemoteWorld) -> None:
    put(world, "big.png", png() + bytes(d.ATTACHMENT_CAPS["image"]), 1_764_000_000)
    assert first(world, FORCED, "switchboard-remote attachment big.png")["error"] == "too_large"
    assert first(world, FORCED, "switchboard-remote attachments")["skipped"] == 1


def test_serve_mode_streams_and_cancels(world: RemoteWorld) -> None:
    put(world, "clip.mp4", mp4(mdat=os.urandom(3 * 1024 * 1024 - 1000)), 1_764_000_000)
    put(world, "shot.png", png(), 1_764_000_000)
    session = ServeSession(["--serve", *FORCED], world.tmux_env())
    try:
        hello = session.read()
        assert hello is not None
        assert hello["allowAttachments"] is True
        docs = session.request(1, "attachment", ["shot.png"])
        assert docs[0]["kind"] == "image"
        assert docs[-1] == {"done": True}
        listed = session.request(2, "attachments", [])
        assert [a["name"] for a in listed[0]["attachments"]] == ["clip.mp4", "shot.png"]
        session.send({"t": "req", "id": 3, "verb": "attachment", "args": ["clip.mp4"]})
        got = session.read()
        assert got is not None
        assert got["doc"]["chunks"] == 3
        session.send({"t": "cancel", "id": 3})
        frames = [got]
        while frames[-1].get("more"):
            frame = session.read()
            assert frame is not None
            frames.append(frame)
        assert frames[-1]["doc"] in ({"done": True}, {"protocol": 1, "ok": False, "error": "cancelled"})
    finally:
        session.finish()


def test_hello_names_attachments_only_when_on(world: RemoteWorld) -> None:
    session = ServeSession(["--serve", "--sessions", "fpga"], world.tmux_env())
    try:
        hello = session.read()
        assert hello is not None
        assert "allowAttachments" not in hello
    finally:
        session.finish()


# --- the hub's parsing: a dispatcher that lies -------------------------------------------------


def _answer(data: bytes, **header: Any) -> list[dict[str, Any]]:
    head = {
        "protocol": 1,
        "ok": True,
        "stream": "attachment",
        "name": "shot.png",
        "kind": "image",
        "mime": "image/png",
        "size": len(data),
        "mtime": 1,
        "sha256": hashlib.sha256(data).hexdigest(),
        "chunks": 1,
        **header,
    }
    return [head, {"chunk": 0, "data": base64.b64encode(data).decode()}, {"done": True}]


def test_parse_accepts_a_correct_answer() -> None:
    parsed = parse_response("attachment", stdout(_answer(png())))
    assert isinstance(parsed, AttachmentResponse)
    assert parsed.data == png()


@pytest.mark.parametrize(
    "docs",
    [
        _answer(png(), sha256="0" * 64),
        _answer(png(), size=len(png()) + 1),
        _answer(png(), chunks=2),
        _answer(png(), kind="document"),
        _answer(png(), name="../etc/passwd"),
        _answer(png(), name="shot.png", stream="usage"),
        _answer(png())[:2],
        [*_answer(png())[:2], {"chunk": 1, "data": ""}, {"done": True}],
        [_answer(png())[0], {"chunk": 0, "data": "not base64!"}, {"done": True}],
        [_answer(png())[0], {"chunk": 0, "data": base64.b64encode(png()).decode(), "x": 1}, {"done": True}],
        _answer(bytes(d.ATTACHMENT_CAPS["image"] + 1), chunks=4),
    ],
)
def test_parse_refuses_answers_that_do_not_add_up(docs: list[dict[str, Any]]) -> None:
    with pytest.raises(ResponseError):
        parse_response("attachment", stdout(docs))
