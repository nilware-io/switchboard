"""Media attachments from a machine's outbox, for `remote_attachments` and `remote_attachment`
(docs/SWITCHBOARD.md, "Attachments").

Only images, audio and video, only from a machine that opted in twice (`allowAttachments` here and
`--allow-attachments` in the dispatcher's command line). The hub trusts nothing the dispatcher
says about a file: it runs `dispatcher.media_check` again on the bytes it received, so a
compromised machine cannot hand the client SVG, HTML or anything over the size caps. Bytes stay in
memory for one tool call; nothing is written to disk.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
)

from ..clock import ago, now_ms
from ..text import plural
from .protocol import ATTACHMENT_CAPS, AttachmentInfo
from .speech import quote

FETCH_WINDOW_MS = 10 * 60 * 1000
MAX_FETCHES_PER_WINDOW = 20
"""Attachments one machine may send within FETCH_WINDOW_MS."""
BYTE_WINDOW_MS = 60 * 60 * 1000
MAX_BYTES_PER_WINDOW = 1024 * 1024 * 1024
"""Bytes all machines together may send within BYTE_WINDOW_MS: four 250 MB videos an hour."""
MAX_CONCURRENT_FETCHES = 1
"""Attachment fetches in flight on the hub at once (a 250 MB video needs about twice its size while
it is reassembled and checked, and the check runs in a worker thread)."""
BUSY_WORDS = "I am already fetching an attachment. Try again in a moment."
SPOKEN_ATTACHMENTS = 5
KIND_WORDS = {"image": "an image", "audio": "audio", "video": "a video"}
KIND_NOUNS = {"image": "image", "audio": "audio file", "video": "video"}

REFUSALS = {
    "not_allowed": "switchboard-remote on {machine} does not allow attachments; it needs --allow-attachments.",
    "bad_request": "switchboard-remote on {machine} is too old for attachments; it needs version 0.7.0.",
    "bad_outbox": (
        "The outbox on {machine} is not a plain folder of that user, or others can write to it, so I do not read it."
    ),
}
FILE_REFUSALS = {
    "not_found": "{machine} has no file {name} in its outbox any more.",
    "not_media": "{name} on {machine} is not a whole image, audio or video file, so I did not send it.",
    "too_large": "{name} on {machine} is too large to send: images up to {image}, audio {audio}, video {video}.",
    "changed": "{name} on {machine} changed while I read it. Try again in a moment.",
}


@dataclass(frozen=True)
class Media:
    """One checked attachment for the client."""

    machine: str
    name: str
    kind: str
    mime: str
    data: bytes | bytearray
    """The hub's own cleaned copy (`media_check`)."""


def size_words(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def media_content(answer_text: str, media: Media) -> CallToolResult:
    """The tool result: a short text line, then the media as MCP content. MCP has image and audio
    content; a video goes as an embedded resource with a blob (MCP has no video content type)."""
    data = base64.b64encode(media.data).decode("ascii")
    text = TextContent(type="text", text=answer_text)
    if media.kind == "image":
        return CallToolResult(content=[text, ImageContent(type="image", data=data, mime_type=media.mime)])
    if media.kind == "audio":
        return CallToolResult(content=[text, AudioContent(type="audio", data=data, mime_type=media.mime)])
    resource = BlobResourceContents(uri=f"attachment://{media.machine}/{media.name}", mime_type=media.mime, blob=data)
    return CallToolResult(content=[text, EmbeddedResource(type="resource", resource=resource)])


@dataclass
class Reservation:
    """One fetch counted against the limits; `size` is corrected to the bytes actually received."""

    time: int
    size: int


@dataclass
class FetchLimiter:
    """Sliding-window limits on attachment fetches (in memory only). A fetch is counted when it is
    asked of the machine, whatever comes back: refused, failed and rejected transfers count too."""

    now: Callable[[], int] = now_ms
    _fetches: dict[str, list[int]] = field(default_factory=dict)
    _bytes: list[Reservation] = field(default_factory=list)

    def _trim(self, machine: str) -> list[int]:
        now = self.now()
        times = [t for t in self._fetches.get(machine, []) if now - t < FETCH_WINDOW_MS]
        self._fetches[machine] = times
        self._bytes = [r for r in self._bytes if now - r.time < BYTE_WINDOW_MS]
        return times

    def _total(self) -> int:
        return sum(r.size for r in self._bytes)

    def allows(self, machine: str) -> bool:
        times = self._trim(machine)
        return len(times) < MAX_FETCHES_PER_WINDOW and self._total() < MAX_BYTES_PER_WINDOW

    def fits(self, size: int) -> bool:
        return self._total() + size <= MAX_BYTES_PER_WINDOW

    def reserve(self, machine: str, size: int) -> Reservation:
        """Count a fetch of about `size` bytes before it is asked of the machine."""
        now = self.now()
        self._trim(machine).append(now)
        reservation = Reservation(now, size)
        self._bytes.append(reservation)
        return reservation


FILLER_WORDS = frozenset(("the", "a", "an", "my", "that", "this", "last", "latest", "newest", "file"))


def pick(entries: Sequence[AttachmentInfo], name: str | None, kind: str | None) -> AttachmentInfo | None:
    """The attachment a spoken request means: the newest (of `kind`) without a name; with one, the
    exact file name, else the newest whose name without extension matches, else the newest whose
    name contains every spoken word (case ignored, "the", "last" and similar left out). Entries
    are newest first."""
    pool = [e for e in entries if kind is None or e.kind == kind]
    wanted = (name or "").strip().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", wanted) if w and w not in FILLER_WORDS]
    if not words:
        return pool[0] if pool else None
    for test in (
        lambda e: e.name.lower() == wanted,
        lambda e: e.name.rsplit(".", 1)[0].lower() == wanted,
        lambda e: all(w in e.name.lower() for w in words),
    ):
        found = [e for e in pool if test(e)]
        if found:
            # Several match: the newest one, as a person means "the screenshot".
            return found[0]
    return None


def _entry_words(entry: AttachmentInfo, now: int) -> str:
    when = f", {ago(now - entry.mtime)}" if entry.mtime is not None else ""
    return f"{KIND_WORDS[entry.kind]} {quote(entry.name)}{when}, {size_words(entry.size)}"


def format_attachments(
    machine: str, entries: Sequence[AttachmentInfo], *, skipped: int, outbox: bool, now: int, kind: str | None = None
) -> str:
    if not outbox:
        return f"{machine} has no outbox folder, so there is nothing to show."
    pool = [e for e in entries if kind is None or e.kind == kind]
    what = {"image": "images", "audio": "audio files", "video": "videos"}.get(kind or "", "images, audio or video")
    if not pool:
        text = f"The outbox on {machine} has no {what}."
    else:
        noun = KIND_NOUNS[kind] if kind else "media file"
        spoken = "; ".join(_entry_words(e, now) for e in pool[:SPOKEN_ATTACHMENTS])
        more = f"; and {len(pool) - SPOKEN_ATTACHMENTS} more" if len(pool) > SPOKEN_ATTACHMENTS else ""
        newest = pool[0]
        age = f" from {ago(now - newest.mtime)}" if newest.mtime is not None else ""
        text = (
            f"{machine} has {plural(len(pool), noun)} in its outbox; the newest is {KIND_WORDS[newest.kind]}{age}. "
            f"Newest first, with the file names the machine gives: {spoken}{more}."
        )
    if skipped:
        text += (
            f" {plural(skipped, 'other file')} there {'is' if skipped == 1 else 'are'} not shown: "
            "not an image, audio or video file I can send, or too large."
        )
    return text


def format_sent(media: Media, mtime: int | None, now: int) -> str:
    when = f", from {ago(now - mtime)}" if mtime is not None else ""
    noun = {"image": "Image", "audio": "Audio", "video": "Video"}[media.kind]
    text = f"{noun} {quote(media.name)} from {media.machine}, {size_words(len(media.data))}{when}. The file name comes from the machine."
    if media.kind != "image":
        text += " Tags inside audio and video files may not all be removed."
    return text


def refusal_words(code: str | None, machine: str, name: str | None) -> str | None:
    """The spoken refusal of a dispatcher error code, None when it is not an attachment refusal."""
    if code in REFUSALS:
        return REFUSALS[code].format(machine=machine)
    if code in FILE_REFUSALS and name is not None:
        caps = {kind: size_words(cap) for kind, cap in ATTACHMENT_CAPS.items()}
        return FILE_REFUSALS[code].format(machine=machine, name=quote(name), **caps)
    return None
