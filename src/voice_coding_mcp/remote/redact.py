"""Cleaning terminal text before it is summarised, spoken or pushed (docs/SWITCHBOARD.md, "Redaction").

`strip_terminal` removes ANSI CSI/OSC sequences and control characters; `redact` replaces secrets
with `[secret]`. Both are best effort, which is why pane text still never goes to logs.
"""

from __future__ import annotations

import re
import unicodedata

SECRET = "[secret]"

_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x9d[^\x07\x1b\x9c]*(?:\x07|\x9c)?")
_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[@-Z\\-_]|\x1b[ -/]*[0-~]")
_INVISIBLE = frozenset(
    [0x061C, 0x200B, 0x200E, 0x200F, 0x2028, 0x2029, 0xFEFF, *range(0x202A, 0x202F), *range(0x2060, 0x206A)]
)


def strip_terminal(text: str) -> str:
    """Without ANSI escape sequences, C0 and C1 controls (newline and tab kept) and invisible or
    bidi format characters; `\\r\\n` becomes `\\n`."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _ESC_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    return "".join(
        ch
        for ch in text
        if ch in "\n\t"
        or (ord(ch) not in _INVISIBLE and unicodedata.category(ch) not in ("Cc", "Cs", "Co") and ch != "\x7f")
    )


def clean_name(value: str | None, limit: int = 40) -> str:
    """A name from a machine (tmux session or window, Claude Code session name) fit for a spoken
    answer: terminal-stripped, one line, at most `limit` characters."""
    if not value:
        return ""
    text = " ".join(strip_terminal(value).split())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text[:limit].strip()


_KEY_WORDS = r"(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential)"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(ssh-(?:rsa|ed25519|ecdsa)|ecdsa-sha2-nistp\d+)\s+[A-Za-z0-9+/=]{60,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_\w{20,}"),
    re.compile(r"\bsk-ant-[\w-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)\S+"),
    re.compile(r"\bxox[abprs]-[\w-]{10,}"),
    re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),
    re.compile(r"(?i)(authorization\s*:\s*)(?:\w+\s+)?\S+"),
    re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]{8,}"),
    re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+(?=@)", re.I),
    re.compile(rf"(?i)(\b[\w.-]*{_KEY_WORDS}[\w.-]*[\"']?\s*[=:]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)"),
)
_LONG_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")
_COMMIT_BEFORE_RE = re.compile(r"(?i)\bcommit\s*$")


def _keep_prefix(match: re.Match[str]) -> str:
    """Keeps the first group (a key name, a scheme) when the pattern has one."""
    if match.re.groups and match.group(1) is not None:
        if match.re.pattern.startswith(r"\b(ssh-"):
            return f"{match.group(1)} {SECRET}"
        return f"{match.group(1)}{SECRET}"
    return SECRET


def _long_run(match: re.Match[str], text: str) -> str:
    token = match.group(0)
    is_hex = re.fullmatch(r"[0-9a-fA-F]{32,64}", token) is not None
    if is_hex and _COMMIT_BEFORE_RE.search(text[max(0, match.start() - 16) : match.start()]):
        return token
    if not is_hex and "=" not in token and not re.search(r"[0-9]", token):
        # Long words without digits (names written with - or _) stay.
        return token
    if "/" in token and all(re.fullmatch(r"[a-z0-9_-]*", part) for part in token.split("/")):
        # A lowercase path such as src/remote/v2/dispatcher stays; mixed-case runs do not.
        return token
    return SECRET


def redact(text: str) -> str:
    """`text` with secrets replaced by `[secret]` (PEM keys, tokens of common services, JWTs,
    Authorization and Bearer values, URL credentials, `password=...`-style values, and long base64
    or hex runs except a commit hash shown after the word `commit`)."""
    for pattern in _PATTERNS:
        text = pattern.sub(_keep_prefix, text)
    return _LONG_RUN_RE.sub(lambda m: _long_run(m, text), text)


def clean_screen(text: str) -> str:
    """Terminal-stripped and redacted: the only form of pane text the hub works with."""
    return redact(strip_terminal(text))
