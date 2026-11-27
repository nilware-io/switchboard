"""Per-machine usage cache, `stateDir/usage/<machine>.json` (docs/SWITCHBOARD.md, "Usage computation").

It holds per transcript `{id, size, mtime, project, skipped, records}` (records as compact arrays, no
text). A refresh sends the known (id, size, mtime) list, receives only changed files and replaces
them; files the machine reports gone are removed. Records and files older than RETENTION_DAYS are
dropped. A broken cache file is treated as empty (the next refresh reads everything again).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import dataclass, field

from ..clock import DAY_MS
from ..json_file import ensure_dir
from ..remote.dispatcher import MAX_USAGE_KNOWN_BYTES
from .records import Record, Transcript, UsageBatch, UsageStreamError, record_from

CACHE_VERSION = 2
"""2: dispatcher 0.10.0 fields (agent transcripts, entrypoint, work projects, origins); a version 1
cache is read as empty, so the first refresh reads every transcript again."""
RETENTION_DAYS = 400
MAX_CACHE_BYTES = 512 * 1024 * 1024
HUB = "local"
"""Cache name of the hub's own Claude Code home (a machine may not be called `local`)."""


@dataclass
class MachineCache:
    machine: str
    refreshed_ms: int | None = None
    """When the last refresh succeeded (None: never)."""
    files: dict[str, Transcript] = field(default_factory=dict)


def usage_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "usage")


def cache_path(state_dir: str, machine: str) -> str:
    return os.path.join(usage_dir(state_dir), f"{machine}.json")


def _trim(row: list[object]) -> list[object]:
    """Trailing default values (None, 0, False) left out of a cached row."""
    while row and (row[-1] is None or row[-1] is False or row[-1] == 0) and len(row) > 2:
        row.pop()
    return row


def _record_row(record: Record) -> list[object]:
    if record.kind == "u":
        return _trim([record.t, "u", "n" if record.other_origin else None, record.project])
    return _trim(
        [
            record.t,
            "a",
            record.model,
            record.msg_id,
            record.req_id,
            record.input,
            record.output,
            record.cache_write_5m,
            record.cache_write_1h,
            record.cache_read,
            1 if record.sidechain else 0,
            record.project,
        ]
    )


_ROW_KEYS = ("t", "k", "m", "id", "rq", "in", "out", "cw5", "cw1", "cr", "sc", "p")
_PROMPT_ROW_KEYS = ("t", "k", "o", "p")


def _record_of(row: object) -> Record:
    if not isinstance(row, list) or len(row) < 2:
        raise UsageStreamError("bad cached record")
    keys = _PROMPT_ROW_KEYS if row[1] == "u" else _ROW_KEYS
    if len(row) > len(keys):
        raise UsageStreamError("bad cached record")
    return record_from({k: v for k, v in zip(keys, row, strict=False) if v is not None})


def load_cache(state_dir: str, machine: str) -> MachineCache:
    cache = MachineCache(machine=machine)
    try:
        with open(cache_path(state_dir, machine), "rb") as handle:
            raw = handle.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return cache
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return cache
    try:
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return cache
        refreshed = data.get("refreshedAt")
        files: dict[str, Transcript] = {}
        for item in data.get("files", []):
            if not isinstance(item, dict):
                raise UsageStreamError("bad cached file")
            size, mtime, project = item.get("size"), item.get("mtime"), item.get("project", "")
            if not isinstance(size, int) or not isinstance(mtime, int) or not isinstance(project, str):
                raise UsageStreamError("bad cached file")
            entrypoint, work = item.get("ep"), item.get("wp", "")
            if (entrypoint is not None and not isinstance(entrypoint, str)) or not isinstance(work, str):
                raise UsageStreamError("bad cached file")
            transcript = Transcript(
                id=str(item.get("id")),
                size=size,
                mtime=mtime,
                project=project,
                skipped=item.get("skipped") is True,
                records=[_record_of(row) for row in item.get("records", [])],
                entrypoint=entrypoint,
                work_project=work,
            )
            files[transcript.id] = transcript
    except (UsageStreamError, TypeError):
        return cache
    cache.refreshed_ms = refreshed if isinstance(refreshed, int) and not isinstance(refreshed, bool) else None
    cache.files = files
    return cache


def save_cache(state_dir: str, cache: MachineCache) -> None:
    """Atomic, mode 0600, compact JSON."""
    directory = usage_dir(state_dir)
    ensure_dir(directory)
    data = {
        "version": CACHE_VERSION,
        "machine": cache.machine,
        "refreshedAt": cache.refreshed_ms,
        "files": [
            {
                "id": t.id,
                "size": t.size,
                "mtime": t.mtime,
                "project": t.project,
                "skipped": t.skipped,
                "records": [_record_row(r) for r in t.records],
                **({"ep": t.entrypoint} if t.entrypoint else {}),
                **({"wp": t.work_project} if t.work_project else {}),
            }
            for t in cache.files.values()
        ],
    }
    path = cache_path(state_dir, cache.machine)
    tmp = os.path.join(directory, f".{cache.machine}.json.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def retention_start(now_ms: int) -> int:
    return now_ms - RETENTION_DAYS * DAY_MS


def apply_batch(cache: MachineCache, batch: UsageBatch, now_ms: int) -> None:
    """Replaces the changed files, removes the gone ones, drops what is older than the retention."""
    for gone in batch.gone:
        cache.files.pop(gone, None)
    for transcript in batch.files:
        cache.files[transcript.id] = transcript
    oldest = retention_start(now_ms)
    for file_id in list(cache.files):
        transcript = cache.files[file_id]
        if transcript.mtime < oldest:
            del cache.files[file_id]
            continue
        transcript.records = [r for r in transcript.records if r.t >= oldest]
    cache.refreshed_ms = now_ms


def known_list(cache: MachineCache) -> bytes:
    """The `usage` verb's stdin: known files, newest first, cut to fit MAX_USAGE_KNOWN_BYTES (a file
    left out is only sent again)."""
    items = sorted(cache.files.values(), key=lambda t: -t.mtime)
    parts: list[str] = []
    size = 2
    for t in items:
        part = json.dumps({"id": t.id, "size": t.size, "mtime": t.mtime}, separators=(",", ":"))
        if size + len(part) + 1 > MAX_USAGE_KNOWN_BYTES:
            break
        parts.append(part)
        size += len(part) + 1
    return ("[" + ",".join(parts) + "]").encode("ascii")


def known_map(cache: MachineCache) -> dict[str, tuple[int, int]]:
    """The same list for the hub's own home (no size limit: nothing crosses a pipe)."""
    return {t.id: (t.size, t.mtime) for t in cache.files.values()}
