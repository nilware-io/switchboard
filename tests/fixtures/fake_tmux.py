#!/usr/bin/env python3
"""Fake tmux for the dispatcher tests. Standard library only.

Reads the scenario JSON named by FAKE_TMUX_SCENARIO:

  {"noServer": false, "panes": [{"session_name": "fpga", "pane_id": "%1", ...}], "text": {"%1": "..."}}

Pane dicts use tmux format names (`session_name`, `window_index`, `pane_pid`, ...); missing ones
render empty. Answers `list-panes -a -F <format>` and `capture-pane -p -J -t <pane> -S -<n>` (the
last n lines of the pane's text), and for sends `load-buffer -b <name> -` (the buffer from stdin),
`paste-buffer -p -d -b <name> -t <pane>`, `send-keys -t <pane> Enter` and `delete-buffer -b <name>`;
`send-keys -t <pane> <key>...` records the keys in the scenario's `keys` list and applies
its `afterKeys` once (like `afterPaste`, and its `panes` replace the pane list); `captureSequence` ({pane: [text, ...]}) makes each capture
of that pane first take the next text from its list (a screen that changes between two checks); `resize-window -t <pane> -x <n> -y <n>` records `resized`.
Buffers live in `<scenario>.buffers.json`. After a paste, the scenario's `afterPaste` (once) replaces
pane texts (`text`) and writes files (`write`: {path: JSON value}), so a test can open a permission
dialog or change the registry between the paste and the Enter. `failOn` lists subcommands that fail.
Every call is appended to FAKE_TMUX_LOG as {"argv", "env", "stdin"}. A leading `-L <socket>` is
accepted and logged.
"""

from __future__ import annotations

import json
import os
import re
import sys


def render(fmt: str, pane: dict[str, object]) -> str:
    return re.sub(r"#\{([a-z_]+)\}", lambda m: str(pane.get(m.group(1), "")), fmt)


def _buffers(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.buffer.read() if sys.stdin is not None and not sys.stdin.closed else b""
    log = os.environ.get("FAKE_TMUX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            entry = {"argv": argv, "env": dict(os.environ), "stdin": stdin.decode("utf-8", "replace")}
            handle.write(json.dumps(entry) + "\n")
    scenario_path = os.environ["FAKE_TMUX_SCENARIO"]
    buffers_path = scenario_path + ".buffers.json"
    with open(scenario_path, encoding="utf-8") as handle:
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
        sequence = scenario.get("captureSequence", {}).get(pane_id)
        if sequence:
            scenario.setdefault("text", {})[pane_id] = sequence.pop(0)
            _save(scenario_path, scenario)
        text = scenario.get("text", {}).get(pane_id, "")
        sys.stdout.write("\n".join(text.split("\n")[-lines:]) + "\n")
        return 0
    if argv[:1] and argv[0] in scenario.get("failOn", []):
        sys.stderr.write(f"tmux: {argv[0]} failed\n")
        return 1
    if argv[:2] == ["load-buffer", "-b"] and len(argv) == 4 and argv[3] == "-":
        buffers = _buffers(buffers_path)
        buffers[argv[2]] = stdin.decode("utf-8")
        _save(buffers_path, buffers)
        return 0
    if argv[:4] == ["paste-buffer", "-p", "-d", "-b"] and len(argv) == 7 and argv[5] == "-t":
        buffers = _buffers(buffers_path)
        if argv[4] not in buffers:
            sys.stderr.write("no buffer\n")
            return 1
        if argv[6] not in {p.get("pane_id") for p in panes}:
            sys.stderr.write(f"can't find pane: {argv[6]}\n")
            return 1
        pasted = buffers.pop(argv[4])
        _save(buffers_path, buffers)
        scenario.setdefault("pasted", []).append({"pane": argv[6], "text": pasted})
        after = scenario.pop("afterPaste", None)
        if isinstance(after, dict):
            scenario.setdefault("text", {}).update(after.get("text", {}))
            for path, value in after.get("write", {}).items():
                _save(path, value)
        _save(scenario_path, scenario)
        return 0
    if argv[:2] == ["send-keys", "-t"] and len(argv) >= 4:
        if argv[2] not in {p.get("pane_id") for p in panes}:
            sys.stderr.write(f"can't find pane: {argv[2]}\n")
            return 1
        scenario.setdefault("keys", []).append({"pane": argv[2], "keys": argv[3:]})
        after = scenario.pop("afterKeys", None)
        if isinstance(after, dict):
            scenario.setdefault("text", {}).update(after.get("text", {}))
            if "panes" in after:
                scenario["panes"] = after["panes"]
        _save(scenario_path, scenario)
        return 0
    if argv[:2] == ["resize-window", "-t"] and len(argv) == 7 and argv[3] == "-x" and argv[5] == "-y":
        if argv[2] not in {p.get("pane_id") for p in panes}:
            sys.stderr.write(f"can't find window: {argv[2]}\n")
            return 1
        scenario.setdefault("resized", []).append({"pane": argv[2], "x": argv[4], "y": argv[6]})
        _save(scenario_path, scenario)
        return 0
    if argv[:2] == ["delete-buffer", "-b"] and len(argv) == 3:
        buffers = _buffers(buffers_path)
        buffers.pop(argv[2], None)
        _save(buffers_path, buffers)
        return 0
    sys.stderr.write(f"unknown command: {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
