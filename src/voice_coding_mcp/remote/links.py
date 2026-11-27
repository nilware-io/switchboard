"""One-time links for attachments too large for a tool result (docs/SWITCHBOARD.md, "Large
videos: a link").

A video over `EMBED_MAX_BYTES` cannot go back inside an MCP tool result (a 250 MB video is about
333 MB of base64). The hub keeps its own cleaned copy in memory only, never on disk, under a random
256-bit token for `LINK_TTL_MS`, and serves it at `<publicUrl>/attachments/<token>` with HTTP range
support (phones play video by ranges). The store holds at most `MAX_LINK_BYTES` in all (the oldest
link is dropped beyond it) and serves at most `MAX_SERVED_FACTOR` times a file's size per link.

Lookup is by the SHA-256 of the token, so the dict lookup and the comparison after it depend only
on the hash, never on how many characters of a guess are right; the token itself is kept in memory
only to hand the same link out again. Logs and audit lines carry only `log.fingerprint` of it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.types import Receive, Scope, Send

from ..clock import Clock, now_ms
from ..log import fingerprint

LINK_PATH = "/attachments/"
LINK_TTL_MS = 10 * 60 * 1000
LINK_TTL_WORDS = "10 minutes"
TOKEN_BYTES = 32
"""256 random bits, 43 characters of base64url."""
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
MAX_LINK_BYTES = 512 * 1024 * 1024
"""Bytes all held links may hold together: two 250 MB videos."""
MAX_SERVED_FACTOR = 8
"""A link serves at most this many times its file's size (a player that seeks reads parts again);
then it is dropped, so a leaked link cannot be used as a free download mirror for its 10 minutes."""
SEND_BYTES = 1024 * 1024
"""Bytes per ASGI body message (each one waits for the client, uvicorn applies back-pressure)."""
CSP = "default-src 'none'; sandbox"


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def token_fingerprint(token: str) -> str:
    return fingerprint(token)


@dataclass
class Link:
    token: str
    key: str
    machine: str
    name: str
    kind: str
    mime: str
    data: bytes | bytearray
    created: int
    expires: int
    size: int
    """Of the file as listed (with `mtime`, to hand the same link out again)."""
    mtime: int | None
    requests: int = 0
    ranges: int = 0
    served: int = 0

    @property
    def fingerprint(self) -> str:
        return token_fingerprint(self.token)


EndHook = Callable[[Link, str], None]
"""Called once when a link ends, with the reason: `expired`, `evicted`, `served_limit` or `closed`."""


@dataclass
class LinkStore:
    now: Clock = now_ms
    on_end: EndHook | None = None
    max_bytes: int = MAX_LINK_BYTES
    _links: OrderedDict[str, Link] = field(default_factory=OrderedDict)

    def _end(self, link: Link, reason: str) -> None:
        self._links.pop(link.key, None)
        link.data = b""
        if self.on_end is not None:
            self.on_end(link, reason)

    def sweep(self) -> None:
        """Drops expired links (also called on every add and lookup)."""
        now = self.now()
        for link in [link for link in self._links.values() if link.expires <= now]:
            self._end(link, "expired")

    @property
    def held_bytes(self) -> int:
        return sum(len(link.data) for link in self._links.values())

    def __len__(self) -> int:
        return len(self._links)

    def add(
        self, *, machine: str, name: str, kind: str, mime: str, data: bytes | bytearray, size: int, mtime: int | None
    ) -> Link:
        """A new link; older links are dropped while all of them would hold more than `max_bytes`."""
        self.sweep()
        now = self.now()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        link = Link(
            token=token,
            key=token_key(token),
            machine=machine,
            name=name,
            kind=kind,
            mime=mime,
            data=data,
            created=now,
            expires=now + LINK_TTL_MS,
            size=size,
            mtime=mtime,
        )
        while self._links and self.held_bytes + len(data) > self.max_bytes:
            self._end(next(iter(self._links.values())), "evicted")
        self._links[link.key] = link
        return link

    def get(self, token: str) -> Link | None:
        self.sweep()
        if not TOKEN_RE.fullmatch(token):
            return None
        key = token_key(token)
        link = self._links.get(key)
        if link is None or not hmac.compare_digest(link.key, key):
            return None
        return link

    def find(self, machine: str, name: str, size: int, mtime: int | None) -> Link | None:
        """A live link of the same file (same name, size and modification time)."""
        self.sweep()
        for link in self._links.values():
            if (link.machine, link.name, link.size, link.mtime) == (machine, name, size, mtime):
                return link
        return None

    def close(self) -> None:
        for link in list(self._links.values()):
            self._end(link, "closed")

    def served(self, link: Link, length: int) -> bool:
        """Counts `length` bytes about to be served; False (and the link dropped) past the limit."""
        if link.served + length > MAX_SERVED_FACTOR * max(1, len(link.data)):
            self._end(link, "served_limit")
            return False
        link.served += length
        return True


def parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """(first, last) byte of a single `bytes=` range, None to send the whole file (no header, a
    header that is not one well-formed byte range: RFC 9110 lets a server ignore it), or
    "unsatisfiable"."""
    if header is None:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first_text, dash, last_text = spec.strip().partition("-")
    if not dash or not (first_text.isdigit() or first_text == "") or not (last_text.isdigit() or last_text == ""):
        return None
    if len(first_text) > 20 or len(last_text) > 20:
        return None
    if first_text == "":
        if last_text == "":
            return None
        suffix = int(last_text)
        if suffix == 0 or size == 0:
            return "unsatisfiable"
        return max(0, size - suffix), size - 1
    first = int(first_text)
    if last_text and int(last_text) < first:
        return None
    last = int(last_text) if last_text else size - 1
    if first >= size:
        return "unsatisfiable"
    return first, min(last, size - 1)


def link_headers(link: Link) -> list[tuple[bytes, bytes]]:
    # The name matches ATTACHMENT_NAME_RE (letters, digits, dot, underscore, hyphen), so it needs
    # no quoting beyond the quotes.
    return [
        (b"content-type", link.mime.encode("ascii")),
        (b"content-disposition", f'inline; filename="{link.name}"'.encode("ascii")),
        (b"x-content-type-options", b"nosniff"),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", CSP.encode("ascii")),
        (b"accept-ranges", b"bytes"),
    ]


async def serve_link(store: LinkStore, link: Link, scope: Scope, receive: Receive, send: Send) -> None:
    """GET or HEAD of a live link: 200 with the whole file, 206 with one range, 416 for a range
    past the end. The bytes go out as slices of the held copy, nothing is copied whole."""
    data = link.data
    size = len(data)
    headers = dict((k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers", []))
    wanted = parse_range(headers.get("range"), size)
    head = scope["method"] == "HEAD"
    base = link_headers(link)
    link.requests += 1
    if wanted == "unsatisfiable":
        await send(
            {
                "type": "http.response.start",
                "status": 416,
                "headers": [*base, (b"content-range", f"bytes */{size}".encode("ascii")), (b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    if wanted is None:
        first, last, status = 0, size - 1, 200
        extra: list[tuple[bytes, bytes]] = []
    else:
        assert not isinstance(wanted, str)
        first, last = wanted
        status = 206
        link.ranges += 1
        extra = [(b"content-range", f"bytes {first}-{last}/{size}".encode("ascii"))]
    length = last - first + 1 if size else 0
    if not head and not store.served(link, length):
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"cache-control", b"no-store")],
            }
        )
        await send({"type": "http.response.body", "body": b"Not found."})
        return
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [*base, *extra, (b"content-length", str(length).encode("ascii"))],
        }
    )
    if head or length == 0:
        await send({"type": "http.response.body", "body": b""})
        return
    view = memoryview(data)
    position = first
    end = last + 1
    while position < end:
        stop = min(end, position + SEND_BYTES)
        await send({"type": "http.response.body", "body": bytes(view[position:stop]), "more_body": stop < end})
        position = stop
