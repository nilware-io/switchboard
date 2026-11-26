#!/usr/bin/env python3
"""A fake OpenSSH client for switchboard-connect (the machine's outbound connection).

The connector builds ssh's environment from scratch, so the settings come from the JSON file named
by FAKE_SSH_OUT_CONFIG, which a per-test wrapper script sets. Every invocation appends its argv to
the config's `log`. Unless every option the connector must pass is present, it exits 255 like
OpenSSH refusing an option set. Modes (`mode`):

- `hub`: plays the hub on stdin/stdout (the dispatcher's stdout and stdin): reads the hello, sends
  each request of `requests`, records hello and answers in `out`, then sends `bye` (if set) and waits
  for the end of input; exits `exit` (default 0).
- `hold`: reads the hello, records it, then waits for the end of input (or a signal).
- `relay`: runs the real relay in this process with `socket` and `machine` (and an own uid that is
  not the test user's, since the test's hub socket is the test user's own).
- `auth`, `hostkey`, `refused`: OpenSSH's message for that failure on stderr, exit 255.
- `exit`: exits `exit` at once.
"""

from __future__ import annotations

import json
import os
import sys
import time

REQUIRED = [
    "-F",
    "/dev/null",
    "-T",
    "-e",
    "none",
    "BatchMode=yes",
    "IdentitiesOnly=yes",
    "IdentityAgent=none",
    "ForwardAgent=no",
    "ForwardX11=no",
    "ClearAllForwardings=yes",
    "PermitLocalCommand=no",
    "StrictHostKeyChecking=yes",
    "GlobalKnownHostsFile=/dev/null",
    "HostKeyAlias=switchboard-hub",
    "HostKeyAlgorithms=ssh-ed25519",
    "UpdateHostKeys=no",
    "CheckHostIP=no",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
    "RequestTTY=no",
]
MESSAGES = {
    "auth": "sbrelay@hub.example.org: Permission denied (publickey).",
    "hostkey": "Host key verification failed.",
    "refused": "ssh: connect to host hub.example.org port 22: Connection refused",
}


def read_line(buffer: bytearray) -> bytes | None:
    while b"\n" not in buffer:
        chunk = os.read(0, 65536)
        if not chunk:
            return None
        buffer.extend(chunk)
    end = buffer.index(b"\n")
    line = bytes(buffer[:end])
    del buffer[: end + 1]
    return line


def send(frame: dict[str, object]) -> None:
    os.write(1, (json.dumps(frame) + "\n").encode("ascii"))


def wait_for_eof(buffer: bytearray) -> None:
    while read_line(buffer) is not None:
        pass


def main() -> int:
    with open(os.environ["FAKE_SSH_OUT_CONFIG"], encoding="utf-8") as handle:
        config = json.load(handle)
    argv = sys.argv[1:]
    with open(config["log"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "env": dict(os.environ), "pid": os.getpid()}) + "\n")
    missing = [option for option in REQUIRED if option not in argv]
    if missing or argv[-2] != "--":
        sys.stderr.write(f"fake ssh: missing {missing}\n")
        return 255
    mode = config.get("mode", "hub")
    if mode in MESSAGES:
        sys.stderr.write(MESSAGES[mode] + "\n")
        return 255
    if mode == "exit":
        return int(config.get("exit", 0))
    if mode == "relay":
        from voice_coding_mcp.remote import relay  # noqa: PLC0415 (only this mode needs the package)

        return relay.run(
            ["--machine", config["machine"], "--socket", config["socket"]],
            {"SSH_CONNECTION": "203.0.113.9 40000 192.0.2.1 22"},
            own_uid=os.getuid() + 1,
        )
    buffer = bytearray()
    out = open(config["out"], "a", encoding="utf-8")  # noqa: SIM115
    hello = read_line(buffer)
    out.write((hello or b"null").decode("ascii") + "\n")
    out.flush()
    if mode == "hold":
        wait_for_eof(buffer)
        return 0
    for number, request in enumerate(config.get("requests", []), 1):
        send({"t": "req", "id": number, **request})
        answer = read_line(buffer)
        out.write((answer or b"null").decode("ascii") + "\n")
        out.flush()
    if config.get("bye"):
        send({"t": "bye", "reason": config["bye"]})
    wait_for_eof(buffer)
    time.sleep(float(config.get("linger", 0)))
    return int(config.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
