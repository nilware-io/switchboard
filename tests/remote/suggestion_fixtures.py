"""Claude Code 2.1.332 input boxes as `tmux capture-pane -p -e -J` returns them (tmux 3.4, a 100x40
pane, recorded live on 2026-11-27 in a scratch tmux server; the assistant text above the box is
shortened, the escape sequences are exactly as captured). A prompt suggestion is dim (SGR 2) after the
prompt and a no-break space; the empty box's hint `Try "..."` is dim too; typed text, a paste
placeholder and slash commands are not dim."""

from __future__ import annotations

RULE = "\x1b[38;5;244m" + "─" * 100
ABOVE = (
    "  The new line is \x1b[38;5;153mimport datetime; print(datetime.date.today())\x1b[39m. I put the import on"
    " the same line\n"
    "  so the file stays at two lines.\n"
    "\n"
    "\x1b[38;5;246m✻\x1b[39m \x1b[38;5;246mChurned for 14s · done 2:24 PM\x1b[39m\n"
    "\n"
)
STATUS = "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m"


def screen(input_line: str, status: str = STATUS) -> str:
    """The captured screen with `input_line` as the box's prompt line."""
    return f"{ABOVE}{RULE}\n{input_line}\n{RULE}\n{status}\n"


SUGGESTION_LINE = "\x1b[39m❯\xa0\x1b[2mmove the import to the top of the file\x1b[0m"
TYPED_LINE = "\x1b[39m❯\xa0hello there"
HINT_LINE = '\x1b[39m❯\xa0\x1b[2mTry "refactor <filepath>"\x1b[0m'
PASTE_LINE = "\x1b[39m❯\xa0[Pasted text #1 +12 lines]"
SLASH_LINE = "\x1b[39m❯\xa0\x1b[38;5;153m/model\x1b[39m foo"
EXIT_STATUS = "\x1b[39m  \x1b[38;5;246mPress Ctrl-C again to exit\x1b[39m"

SUGGESTION = screen(SUGGESTION_LINE)
"""The idle box with a prompt suggestion: nothing typed."""
TYPED = screen(TYPED_LINE)
HINT = screen(HINT_LINE)
PASTE = screen(PASTE_LINE, "\x1b[39m  \x1b[38;5;246mpaste again to expand\x1b[39m")
SLASH = screen(SLASH_LINE)
SUGGESTION_AFTER_CLEAR = screen(SUGGESTION_LINE, EXIT_STATUS)
"""Right after a Ctrl+C cleared a draft: the suggestion is back and the exit hint shows."""
OPEN_PR = screen("\x1b[39m❯\xa0\x1b[2mopen a PR for this\x1b[0m")
"""An empty input whose suggestion was once read as typed and as done (`remote_send` refused, the summary said a PR was opened)."""
