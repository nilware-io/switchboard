#!/usr/bin/env python3
"""Fake `tailscale`, `sudo` and `systemctl` for the deploy/linux/sb-linux tests. Standard library only.

Started as `fake_linux_host.py <tool> <args...>` by wrapper scripts named after the tool. Every call
appends `{"tool", "argv"}` to `$FAKE_HOST_DIR/calls.jsonl`. State lives in `$FAKE_HOST_DIR`:

- tailscale: `ts.json` `{"funnel": null | "<target>"}`. `$FAKE_HOST_DIR/ts_mode` (a file, so a test
  can change it between runs) is `ok` (default), `stopped` (BackendState "Stopped"), `approval`
  (`funnel --bg` prints an approval link and waits like the real CLI), `denied` (`funnel --bg`
  fails like a non-operator). `status --json` reports `example-host.example-tailnet.ts.net.`;
  `funnel status` prints the real CLI's text layout.
- sudo: `sudo [-H] [-n] [-u USER] [--] cmd...` runs cmd (no user switch).
- systemctl: `is-active --quiet UNIT`, `start`, `stop`, `restart`. `start` runs the JSON argv in
  `$FAKE_HOST_DIR/service.json` (`{"argv": [...]}`) detached, output to `service.log`, pid in
  `service.pid`; `stop` sends SIGTERM and waits for the process to exit.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOST = Path(os.environ["FAKE_HOST_DIR"])
DNS_NAME = "example-host.example-tailnet.ts.net"
LINK = "https://login.tailscale.com/f/funnel?node=nEXAMPLE"


def log(tool: str, argv: list[str]) -> None:
    with open(HOST / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": tool, "argv": argv}) + "\n")


def read(name: str, fallback: Any) -> Any:
    path = HOST / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def write(name: str, value: Any) -> None:
    (HOST / name).write_text(json.dumps(value), encoding="utf-8")


def ts_mode() -> str:
    path = HOST / "ts_mode"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "ok"


def proxy_of(target: str) -> str:
    return f"http://127.0.0.1:{target}" if target.isdigit() else target


def tailscale(argv: list[str]) -> int:
    state = read("ts.json", {"funnel": None})
    mode = ts_mode()
    if argv == ["status", "--json"]:
        backend = "Stopped" if mode == "stopped" else "Running"
        print(
            json.dumps(
                {
                    "BackendState": backend,
                    "CurrentTailnet": {"Name": "example-tailnet", "MagicDNSEnabled": True},
                    "Self": {"DNSName": DNS_NAME + "."},
                }
            )
        )
        return 0
    if argv == ["funnel", "status"]:
        if state["funnel"] is None:
            print("No serve config")
            return 0
        print(f"# Funnel on:\n#     - https://{DNS_NAME}\n")
        print(f"https://{DNS_NAME} (Funnel on)\n|-- / proxy {proxy_of(state['funnel'])}\n")
        return 0
    if argv[:2] == ["funnel", "--bg"] and len(argv) == 3:
        if mode == "approval":
            print(f"Funnel is not enabled on your tailnet.\nTo enable, visit:\n\n         {LINK}\n", flush=True)
            time.sleep(60)
            return 1
        if mode == "denied":
            print("Access denied: serve config denied", file=sys.stderr)
            return 1
        write("ts.json", {"funnel": argv[2]})
        print(f"Available on the internet:\n\nhttps://{DNS_NAME}/\n|-- proxy {proxy_of(argv[2])}\n")
        return 0
    if argv == ["funnel", "--https=443", "off"]:
        write("ts.json", {"funnel": None})
        return 0
    print(f"fake tailscale: unexpected arguments {argv}", file=sys.stderr)
    return 2


def sudo(argv: list[str]) -> int:
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "--":
            i += 1
            break
        if argv[i] == "-u":
            i += 2
            continue
        if argv[i] not in ("-H", "-n"):
            print(f"fake sudo: unexpected option {argv[i]}", file=sys.stderr)
            return 2
        i += 1
    os.execvp(argv[i], argv[i:])


def service_pid() -> int | None:
    path = HOST / "service.pid"
    if not path.exists():
        return None
    pid = int(path.read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    # A zombie of an exited service counts as stopped.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return None if stat.rsplit(")", 1)[1].split()[0] == "Z" else pid


def start() -> None:
    service = read("service.json", None)
    assert service is not None, "service.json missing"
    with open(HOST / "service.log", "ab") as out:
        proc = subprocess.Popen(
            service["argv"], stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True
        )
    (HOST / "service.pid").write_text(str(proc.pid), encoding="utf-8")


def stop() -> None:
    pid = service_pid()
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 25
    while service_pid() is not None and time.monotonic() < deadline:
        time.sleep(0.1)


def systemctl(argv: list[str]) -> int:
    if argv[:2] == ["is-active", "--quiet"] and len(argv) == 3:
        return 0 if service_pid() is not None else 3
    if len(argv) == 2 and argv[0] in ("start", "stop", "restart"):
        if argv[0] in ("stop", "restart"):
            stop()
        if argv[0] in ("start", "restart") and service_pid() is None:
            start()
        return 0
    print(f"fake systemctl: unexpected arguments {argv}", file=sys.stderr)
    return 2


def main() -> int:
    tool, argv = sys.argv[1], sys.argv[2:]
    log(tool, argv)
    if tool == "tailscale":
        return tailscale(argv)
    if tool == "sudo":
        return sudo(argv)
    if tool == "systemctl":
        return systemctl(argv)
    print(f"fake_linux_host: unknown tool {tool}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
