"""The hub's half of `remote_send`: text rules, the risky classifier, the screen echo tracker and the
confirmation store (docs/SWITCHBOARD.md, "remote_send and remote_send_confirm").

The text rules are the dispatcher's own (`dispatcher.text_problem`), so hub and dispatcher cannot
drift apart. Nothing here talks to a machine; `tools.py` does.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ..clock import SECOND_MS
from .dispatcher import PERMISSION_ANSWER_RE, dialog_hash, shows_dialog, text_problem

__all__ = [
    "PERMISSION_ANSWER_RE",
    "RISKY",
    "Confirmation",
    "ConfirmationStore",
    "RiskClassifier",
    "ScreenEcho",
    "dialog_hash",
    "normalize_text",
    "shows_dialog",
    "text_problem",
]


@dataclass(frozen=True)
class RiskRule:
    code: str
    pattern: re.Pattern[str]
    says: str
    """Completes "Careful, this ..."."""
    noun: str
    """Completes "this answers a question about ..."."""
    yes: str
    """The explicit yes the read-back asks for: "say yes, <yes>"."""


def _rule(code: str, pattern: str, says: str, noun: str, yes: str = "send it anyway") -> RiskRule:
    return RiskRule(code, re.compile(pattern, re.I), says, noun, yes)


RISKY: tuple[RiskRule, ...] = (
    _rule("push", r"\bpush\b", "asks for a push", "a push", "send the push"),
    _rule("force", r"--force|\bforce\b|-f\b", "asks to force something", "forcing something"),
    _rule("delete", r"\bdelete\b|\bremove\b|\brm\b|\bunlink\b", "asks to delete something", "deleting something"),
    _rule("deploy", r"\bdeploy\b|\brelease\b|\bpublish\b", "asks for a deploy or a release", "a deploy or a release"),
    _rule("drop", r"\bdrop\b|\btruncate\b", "asks to drop or truncate data", "dropping data"),
    _rule(
        "discard",
        r"reset\s+--hard|\bclean\s+-[a-z]*f|\brevert\b|\boverwrite\b",
        "may throw work away",
        "throwing work away",
    ),
    _rule("merge", r"\bmerge\b|\brebase\b", "asks for a merge or a rebase", "a merge or a rebase"),
    _rule(
        "money",
        r"\bpay\b|\bpurchase\b|\bbuy\b|\bspend\b|\bbilling\b|\$\s?\d|\d+\s?(?:eur|usd|euro|dollar)",
        "may spend money",
        "spending money",
    ),
    _rule("privileges", r"\bsudo\b|\bchmod\b|\bchown\b", "changes permissions or uses sudo", "permissions or sudo"),
    _rule("kill", r"\bkill\b|\breboot\b|\bshutdown\b", "stops a process or the machine", "stopping something"),
    _rule(
        "sending",
        r"\bsend\b.*\b(?:mail|email|message)\b|\bpost\b",
        "sends something out",
        "sending something out",
    ),
)
CUSTOM = "custom"
QUESTION_PREFIX = "question:"
ECHO = "echo"
PERMISSION = "permission"
SHELL = "shell"

_CONTEXT_SAYS = {
    ECHO: "these words came from a screen",
    PERMISSION: "this answers a permission question",
    SHELL: "this types into a plain shell, not into Claude Code",
}


def normalize_text(text: str) -> str:
    """The form of a send's text that is checked, typed and audited: NFC, nothing else changed."""
    return unicodedata.normalize("NFC", text)


class RiskClassifier:
    """Risk reasons of a send, as codes (`push`, `custom`, `question:push`, `echo`, `permission`,
    `shell`), in a fixed order without duplicates."""

    def __init__(self, extra_patterns: Sequence[str] = ()) -> None:
        self._custom = [re.compile(pattern, re.I) for pattern in extra_patterns]

    def _matches(self, text: str) -> list[str]:
        codes = [rule.code for rule in RISKY if rule.pattern.search(text)]
        if any(pattern.search(text) for pattern in self._custom):
            codes.append(CUSTOM)
        return codes

    def classify(
        self, text: str, *, question: str | None, echoed: bool, permission_answer: bool, shell: bool
    ) -> list[str]:
        reasons = self._matches(text)
        if question:
            reasons += [QUESTION_PREFIX + code for code in self._matches(question)][:1]
        if echoed:
            reasons.append(ECHO)
        if permission_answer:
            reasons.append(PERMISSION)
        if shell:
            reasons.append(SHELL)
        return reasons


def _rule_of(code: str) -> RiskRule | None:
    return next((rule for rule in RISKY if rule.code == code), None)


def risk_words(reasons: Sequence[str]) -> str:
    """ "this asks for a push, and these words came from a screen" (for "Careful, ...")."""
    parts: list[str] = []
    for code in reasons:
        if code.startswith(QUESTION_PREFIX):
            asked = code.removeprefix(QUESTION_PREFIX)
            rule = _rule_of(asked)
            noun = rule.noun if rule else "something you marked risky"
            parts.append(f"this answers a question about {noun}")
        elif code == CUSTOM:
            parts.append("this matches one of your risky patterns")
        elif code in _CONTEXT_SAYS:
            parts.append(_CONTEXT_SAYS[code])
        else:
            rule = _rule_of(code)
            parts.append(f"this {rule.says}" if rule else "this looks risky")
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def yes_phrase(reasons: Sequence[str]) -> str:
    """What the owner says to confirm the risky part: "yes, send the push". The first code with a rule
    names it, also as the question's code (`question:push`)."""
    for code in reasons:
        rule = _rule_of(code.removeprefix(QUESTION_PREFIX))
        if rule is not None:
            return f"yes, {rule.yes}"
    return "yes, send it anyway"


class ScreenEcho:
    """Remembers, as keyed hashes of 20-character shingles of normalised text (no text), what screen
    text the hub returned in the last 10 minutes, so a send that repeats it is marked risky: an
    injected screen is the likely source of such words."""

    SHINGLE = 20
    WINDOW_MS = 10 * 60 * SECOND_MS
    MAX_ENTRIES = 400_000

    def __init__(self, now: Callable[[], int]) -> None:
        self._now = now
        self._key = secrets.token_bytes(16)
        self._seen: dict[bytes, int] = {}

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

    def _shingles(self, text: str) -> set[bytes]:
        norm = self._normalise(text)
        size = self.SHINGLE
        return {
            hashlib.blake2b(norm[i : i + size].encode("utf-8"), digest_size=8, key=self._key).digest()
            for i in range(len(norm) - size + 1)
        }

    def observe(self, text: str | None) -> None:
        if not text:
            return
        now = self._now()
        for digest in self._shingles(text):
            self._seen[digest] = now
        if len(self._seen) > self.MAX_ENTRIES:
            cutoff = now - self.WINDOW_MS
            self._seen = {digest: at for digest, at in self._seen.items() if at >= cutoff}
            if len(self._seen) > self.MAX_ENTRIES:
                newest = sorted(self._seen.items(), key=lambda item: item[1])[-self.MAX_ENTRIES // 2 :]
                self._seen = dict(newest)

    def echoed(self, text: str) -> bool:
        """At least 20 characters of `text` (normalised) appeared in recent screen text."""
        cutoff = self._now() - self.WINDOW_MS
        return any(self._seen.get(digest, -1) >= cutoff for digest in self._shingles(text))


@dataclass(frozen=True)
class Confirmation:
    """A prepared send, kept in memory only until it is confirmed, replaced or expired."""

    id: str
    client: str
    """`log.fingerprint` of the OAuth grant id of the client that prepared it."""
    client_name: str | None
    machine: str
    session: str
    """The tmux session name (cleaned), for the audit line."""
    place: str
    """Spoken: "fpga on dev"."""
    pane_id: str
    pane_pid: int
    session_id: str | None
    """The Claude Code session id; None for a pane without Claude Code."""
    text: str
    risky: tuple[str, ...]
    permission_answer: bool
    dialog: str | None
    """For a permission answer: `dispatcher.dialog_hash` of the dialog the read-back quoted (None: a
    registry-only permission wait). The dispatcher types the digit only into that dialog."""
    created_ms: int
    expires_ms: int


LookupProblem = Literal["unknown", "expired", "mismatch", "early"]


MAX_TOTAL_PENDING = 64
"""All clients together; beyond it the oldest of any client is dropped (memory bound only)."""


class ConfirmationStore:
    """At most `max_pending` confirmations per OAuth client; a new one beyond that drops that client's
    oldest, never another client's. Ids are 4-digit numbers, unique among live confirmations: not
    secrets, they bind a confirm call to one read-back."""

    def __init__(
        self,
        *,
        max_pending: int,
        confirm_seconds: int,
        min_confirm_seconds: int,
        now: Callable[[], int],
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._max = max_pending
        self._ttl_ms = confirm_seconds * SECOND_MS
        self._min_ms = min_confirm_seconds * SECOND_MS
        self._now = now
        self._new_id = new_id or (lambda: f"{1000 + secrets.randbelow(9000)}")
        self._items: dict[str, Confirmation] = {}

    def _expire(self) -> None:
        now = self._now()
        for key in [key for key, item in self._items.items() if item.expires_ms <= now]:
            del self._items[key]

    def add(
        self,
        *,
        client: str,
        client_name: str | None,
        machine: str,
        session: str,
        place: str,
        pane_id: str,
        pane_pid: int,
        session_id: str | None,
        text: str,
        risky: Sequence[str],
        permission_answer: bool,
        dialog: str | None = None,
    ) -> tuple[Confirmation, list[Confirmation]]:
        """The new confirmation and the ones of the same client it dropped (oldest first)."""
        self._expire()
        dropped: list[Confirmation] = []
        while (mine := [item for item in self._items.values() if item.client == client]) and len(mine) >= self._max:
            oldest = min(mine, key=lambda item: item.created_ms)
            dropped.append(self._items.pop(oldest.id))
        while len(self._items) >= MAX_TOTAL_PENDING:
            oldest = min(self._items.values(), key=lambda item: item.created_ms)
            del self._items[oldest.id]
        new_id = self._new_id()
        for _ in range(100):
            if new_id not in self._items:
                break
            new_id = self._new_id()
        now = self._now()
        item = Confirmation(
            id=new_id,
            client=client,
            client_name=client_name,
            machine=machine,
            session=session,
            place=place,
            pane_id=pane_id,
            pane_pid=pane_pid,
            session_id=session_id,
            text=text,
            risky=tuple(risky),
            permission_answer=permission_answer,
            dialog=dialog,
            created_ms=now,
            expires_ms=now + self._ttl_ms,
        )
        self._items[new_id] = item
        return item, dropped

    def check(self, confirmation_id: str, client: str) -> Confirmation | LookupProblem:
        """The live confirmation, or why it cannot be used now. Nothing is consumed: `early` keeps
        it, and a different client never learns or removes another client's confirmation."""
        now = self._now()
        item = self._items.get(confirmation_id)
        if item is None:
            return "unknown"
        if item.expires_ms <= now:
            del self._items[confirmation_id]
            return "expired"
        if not secrets.compare_digest(item.client, client):
            return "mismatch"
        if now - item.created_ms < self._min_ms:
            return "early"
        return item

    def consume(self, confirmation_id: str) -> None:
        """Single use: removed before the send starts, whatever its outcome."""
        self._items.pop(confirmation_id, None)

    def __len__(self) -> int:
        self._expire()
        return len(self._items)
