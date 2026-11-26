"""Transcript fixtures shaped like Claude Code 2.x JSONL (only the fields the usage parser reads,
plus some text that must never leave the machine)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from voice_coding_mcp.clock import to_iso

T0 = 1_764_000_000_000
"""2025-11-24T16:00:00Z, a Monday."""
MIN = 60_000
SECRET_TEXT = "the secret prompt text sk-ant-api03-notreal"


def user(t: int, cwd: str = "/home/USER/projects/fpgadc", text: str = SECRET_TEXT, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": to_iso(t),
        "cwd": cwd,
        "sessionId": "ignored",
        "message": {"role": "user", "content": text},
        **extra,
    }


def tool_result(t: int, cwd: str = "/home/USER/projects/fpgadc") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": to_iso(t),
        "cwd": cwd,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": SECRET_TEXT}]},
    }


def assistant(
    t: int,
    msg_id: str = "msg_1",
    *,
    model: str = "claude-opus-4-8",
    request: str = "req_1",
    tokens: tuple[int, int, int, int] = (10, 20, 300, 4000),
    cache_1h: int | None = None,
    cwd: str = "/home/USER/projects/fpgadc",
    **extra: Any,
) -> dict[str, Any]:
    """`tokens`: input, output, cache creation, cache read. With `cache_1h` the cache creation is split
    into `cache_creation` 5m (the creation count) and 1h."""
    input_tokens, output, creation, read = tokens
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_creation_input_tokens": creation + (cache_1h or 0),
        "cache_read_input_tokens": read,
    }
    if cache_1h is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": creation, "ephemeral_1h_input_tokens": cache_1h}
    return {
        "type": "assistant",
        "timestamp": to_iso(t),
        "cwd": cwd,
        "requestId": request,
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": SECRET_TEXT}],
            "usage": usage,
        },
        **extra,
    }


def write_transcript(
    claude_home: str | Path,
    session_id: str,
    lines: list[dict[str, Any] | str],
    project_dir: str = "-home-USER-projects-fpgadc",
    mtime_ms: int | None = None,
) -> str:
    directory = Path(claude_home) / "projects" / project_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    text = "".join(
        (line if isinstance(line, str) else json.dumps(line, separators=(",", ":"))) + "\n" for line in lines
    )
    path.write_text(text, encoding="utf-8")
    if mtime_ms is not None:
        os.utime(path, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
    return str(path)


S1 = "22222222-0000-4000-8000-000000000001"
S2 = "22222222-0000-4000-8000-000000000002"
S3 = "22222222-0000-4000-8000-000000000003"
