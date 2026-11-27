"""`stateDir/audit.jsonl`: one line per `remote_send_confirm` that reached the send step, also when
sending failed (docs/SWITCHBOARD.md, "Audit"). Messages are `remote_send` lines, keys
`remote_send_keys` lines (with the key names, which come from a fixed list) and window sizes
`remote_resize` lines. Each `remote_attachment` fetch that reached the machine adds a
`remote_attachment` line (machine, HMAC of the file name, kind, bytes, outcome; no content).

The file is 0600, opened in append mode and fsynced per line. The text itself is stored only as an
HMAC-SHA256 with a random key in `stateDir/audit.key` (0600), because a plain hash of a short text
such as "push the branch" is found by trying phrases; with `logPrompts` the redacted text is added.
Append-only by convention only: the server's user can rewrite the file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..clock import now_ms, to_iso
from ..json_file import ensure_dir
from .redact import redact
from .send import KEYS, RESIZE, Confirmation

AUDIT_FILE = "audit.jsonl"
KEY_FILE = "audit.key"
KEY_BYTES = 32
WARN_BYTES = 10 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
EVENTS = {KEYS: "remote_send_keys", RESIZE: "remote_resize"}


def audit_path(state_dir: str) -> str:
    return os.path.join(state_dir, AUDIT_FILE)


def _read_key(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            key = handle.read(KEY_BYTES + 1)
    except FileNotFoundError:
        return None
    return key if len(key) == KEY_BYTES else None


def load_key(state_dir: str) -> bytes:
    """The HMAC key, created (0600, exclusively) on first use. A key file of the wrong size is an
    error rather than being replaced, so earlier lines stay comparable."""
    ensure_dir(state_dir)
    path = os.path.join(state_dir, KEY_FILE)
    key = _read_key(path)
    if key is not None:
        return key
    if os.path.exists(path):
        raise ValueError(f"{path} is not a {KEY_BYTES}-byte key")
    fresh = secrets.token_bytes(KEY_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = _read_key(path)
        if key is None:
            raise ValueError(f"{path} is not a {KEY_BYTES}-byte key") from None
        return key
    with os.fdopen(fd, "wb") as handle:
        handle.write(fresh)
        handle.flush()
        os.fsync(handle.fileno())
    return fresh


def text_hmac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class AuditLog:
    state_dir: str
    log_prompts: bool = False
    now: Callable[[], int] = now_ms

    @property
    def path(self) -> str:
        return audit_path(self.state_dir)

    def record(self, item: Confirmation, *, outcome: str, risky_ok: bool) -> dict[str, Any]:
        """Appends the line of one send and returns it."""
        key = load_key(self.state_dir)
        line: dict[str, Any] = {
            "time": to_iso(self.now()),
            "event": EVENTS.get(item.kind, "remote_send"),
            "client": item.client,
            "clientName": item.client_name,
            "machine": item.machine,
            "session": item.session,
            "paneId": item.pane_id,
            "risky": list(item.risky),
            "riskyOk": risky_ok,
            "permissionAnswer": item.permission_answer,
            "preparedAt": to_iso(item.created_ms),
            "textHmac": text_hmac(key, item.text),
            "textChars": len(item.text),
            "outcome": outcome,
        }
        if item.kind == KEYS:
            line["keys"] = list(item.keys)
            line["menuBound"] = item.menu is not None
        if item.kind == RESIZE and item.size is not None:
            line["size"] = {"columns": item.size[0], "rows": item.size[1]}
        if self.log_prompts:
            line["text"] = redact(item.text)
        self._append(line)
        return line

    def record_attachment(
        self,
        *,
        machine: str,
        name: str,
        kind: str | None,
        size: int,
        outcome: str,
        client: str,
        client_name: str | None,
    ) -> dict[str, Any]:
        """Appends the line of one `remote_attachment` fetch: the file name only as an HMAC, never
        any content."""
        key = load_key(self.state_dir)
        line: dict[str, Any] = {
            "time": to_iso(self.now()),
            "event": "remote_attachment",
            "client": client,
            "clientName": client_name,
            "machine": machine,
            "nameHmac": text_hmac(key, name),
            "kind": kind,
            "bytes": size,
            "outcome": outcome,
        }
        self._append(line)
        return line

    def record_link(
        self,
        *,
        link: str,
        machine: str,
        name: str,
        kind: str,
        size: int,
        outcome: str,
        expires: int | None = None,
        requests: int | None = None,
        ranges: int | None = None,
        served: int | None = None,
    ) -> dict[str, Any]:
        """Appends an `attachment_link` line: outcome `created` (with `expiresAt`), or how the link
        ended (`expired`, `evicted`, `served_limit`, `closed`) with the requests, range requests
        and bytes it served. The token only as its fingerprint, the file name as an HMAC."""
        key = load_key(self.state_dir)
        line: dict[str, Any] = {
            "time": to_iso(self.now()),
            "event": "attachment_link",
            "link": link,
            "machine": machine,
            "nameHmac": text_hmac(key, name),
            "kind": kind,
            "bytes": size,
            "outcome": outcome,
        }
        if expires is not None:
            line["expiresAt"] = to_iso(expires)
        if requests is not None:
            line["requests"] = requests
            line["ranges"] = ranges
            line["bytesServed"] = served
        self._append(line)
        return line

    def _append(self, line: dict[str, Any]) -> None:
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)


def audit_size_warning(state_dir: str) -> str | None:
    """A startup warning when the audit file has grown past 10 MB (there is no rotation)."""
    try:
        size = os.path.getsize(audit_path(state_dir))
    except OSError:
        return None
    if size <= WARN_BYTES:
        return None
    return f"{audit_path(state_dir)} is {size // (1024 * 1024)} MB; it is never rotated, archive it by hand"


def read_audit(state_dir: str, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """The last `limit` lines (all when None) as objects, and how many lines could not be read."""
    try:
        with open(audit_path(state_dir), "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return [], 0
    rows: list[dict[str, Any]] = []
    bad = 0
    for chunk in raw.split(b"\n"):
        if not chunk.strip():
            continue
        try:
            value = json.loads(chunk[:MAX_LINE_BYTES].decode("utf-8")) if len(chunk) <= MAX_LINE_BYTES else None
        except (UnicodeDecodeError, ValueError):
            value = None
        if isinstance(value, dict):
            rows.append(value)
        else:
            bad += 1
    if limit is not None:
        rows = rows[-limit:] if limit > 0 else []
    return rows, bad


AUDIT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("time", "time"),
    ("event", "event"),
    ("machine", "machine"),
    ("session", "session"),
    ("paneId", "pane"),
    ("outcome", "outcome"),
    ("risky", "risky"),
    ("riskyOk", "risky ok"),
    ("textChars", "chars"),
    ("keys", "keys"),
    ("kind", "kind"),
    ("bytes", "bytes"),
    ("clientName", "client"),
)


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "-"
    text = str(value)
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:40]


def format_audit_table(rows: Sequence[dict[str, Any]], *, with_text: bool) -> str:
    """A plain text table of audit lines (control characters replaced)."""
    columns = list(AUDIT_COLUMNS) + ([("text", "text")] if with_text else [])
    table = [[title for _, title in columns]]
    table += [[_cell(row.get(key)) for key, _ in columns] for row in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(columns))]
    return (
        "\n".join(
            "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip() for line in table
        )
        + "\n"
    )
