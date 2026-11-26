#!/usr/bin/env python3
"""Fake tmux for the dispatcher tests. Standard library only.

Reads the scenario JSON named by FAKE_TMUX_SCENARIO:

  {"noServer": false, "panes": [{"session_name": "fpga", "pane_id": "%1", ...}], "text": {"%1": "..."}}

Pane dicts use tmux format names (`session_name`, `window_index`, `pane_pid`, ...); missing ones
render empty. Answers `list-panes -a -F <format>` and `capture-pane -p -J -t <pane> -S -<n>` (the
last n lines of the pane's text). Every call is appended to FAKE_TMUX_LOG as {"argv", "env"}.
A leading `-L <socket>` is accepted and logged.
"""

from __future__ import annotations

import json
import os
import re
import sys


def render(fmt: str, pane: dict[str, object]) -> str:
    return re.sub(r"#\{([a-z_]+)\}", lambda m: str(pane.get(m.group(1), "")), fmt)


def main() -> int:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_TMUX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ)}) + "\n")
    with open(os.environ["FAKE_TMUX_SCENARIO"], encoding="utf-8") as handle:
        scenario = json.load(handle)
    if argv[:1] == ["-L"]:
        argv = argv[2:]
    if scenario.get("noServer"):
        sys.stderr.write("no server running on /tmp/tmux-1000/default\n")
        return 1
    if scenario.get("fail"):
        sys.stderr.write("tmux: something broke\n")
        return 1
    panes = scenario.get("panes", [])
    if argv[:3] == ["list-panes", "-a", "-F"] and len(argv) == 4:
        for pane in panes:
            sys.stdout.write(render(argv[3], pane) + "\n")
        for raw in scenario.get("rawLines", []):
            sys.stdout.write(raw + "\n")
        return 0
    if argv[:3] == ["capture-pane", "-p", "-J"] and len(argv) == 7 and argv[3] == "-t" and argv[5] == "-S":
        pane_id = argv[4]
        if pane_id not in {p.get("pane_id") for p in panes}:
            sys.stderr.write(f"can't find pane: {pane_id}\n")
            return 1
        lines = int(argv[6].lstrip("-"))
        text = scenario.get("text", {}).get(pane_id, "")
        sys.stdout.write("\n".join(text.split("\n")[-lines:]) + "\n")
        return 0
    sys.stderr.write(f"unknown command: {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
