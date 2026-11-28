#!/usr/bin/env python3
"""Fake OpenSSH client for tests. Standard library only.

The hub starts ssh with an environment built from scratch, so the fake reads its settings from the
JSON file named by FAKE_SSH_CONFIG, which a per-test wrapper script sets (tests/remote/helpers.py):

  log         file that gets one JSON line {"argv": [...], "env": {...}} per call
  modes       {"<host>": mode}; default mode "ok"
  verbModes   {"<verb>": mode}: overrides `modes` for one dispatcher verb (for example "send")
  forced      the forced-command argv of the dispatcher (the authorized_keys line under test)
  explicit    instead of a forced command: the dispatcher path on the "remote" machine; the hub's
              command is then run like a login shell would (path, then the request tokens)
  home        HOME of the remote user (its .claude registry lives there)
  scenario    fake tmux scenario file; tmuxLog: fake tmux log file
  sleep       seconds to sleep before answering in mode "slow"
  dispatcherPath  run this dispatcher file instead of the package's (an installed copy, for update)

The argv must carry every hardening option of docs/SWITCHBOARD.md "SSH" with the expected value and
no other `-o`; otherwise it exits 255 like a refused connection, so a dropped flag fails the tests.

Modes: ok, slow (sleep, then ok), timeout (sleep 60), hostkey, auth, refused, connect_timeout,
no_dispatcher (exit 127 like a shell that has no such command), garbage (exit 0, not JSON),
hang_after (runs the dispatcher, then hangs without answering: a connection lost after a send).

stdin is passed on to the dispatcher (the text of a send) and logged as `stdin`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCHER = os.path.join(os.path.dirname(HERE), "..", "src", "voice_coding_mcp", "remote", "dispatcher.py")
FAKE_TMUX = os.path.join(HERE, "fake_tmux.py")

EXPECTED = {
    "BatchMode": "yes",
    "IdentitiesOnly": "yes",
    "IdentityAgent": "none",
    "ForwardAgent": "no",
    "ForwardX11": "no",
    "ClearAllForwardings": "yes",
    "PermitLocalCommand": "no",
    "StrictHostKeyChecking": "yes",
    "GlobalKnownHostsFile": "/dev/null",
    "UpdateHostKeys": "no",
    "HashKnownHosts": "no",
    "CheckHostIP": "no",
    "ServerAliveInterval": "5",
    "ServerAliveCountMax": "2",
    "LogLevel": "ERROR",
}


def fail(message: str, code: int = 255) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def parse(argv: list[str]) -> tuple[str, str, dict[str, str]]:
    """(host, command, flags) after checking the hardening; exits 255 when something is missing."""
    if argv[:3] != ["-F", "/dev/null", "-T"]:
        fail("fake ssh: argv must start with -F /dev/null -T")
    options: dict[str, str] = {}
    flags: dict[str, str] = {}
    i = 3
    while i < len(argv) and argv[i] != "--":
        flag = argv[i]
        if i + 1 >= len(argv):
            fail(f"fake ssh: {flag} without a value")
        value = argv[i + 1]
        if flag == "-o":
            key, _, option_value = value.partition("=")
            if key in options:
                fail(f"fake ssh: option {key} given twice")
            options[key] = option_value
        elif flag in ("-i", "-p", "-l"):
            flags[flag] = value
        else:
            fail(f"fake ssh: unexpected flag {flag}")
        i += 2
    for key, value in EXPECTED.items():
        if options.get(key) != value:
            fail(f"fake ssh: missing or wrong option {key}")
    known_hosts = options.get("UserKnownHostsFile", "")
    if not known_hosts.startswith("/"):
        fail("fake ssh: missing or wrong option UserKnownHostsFile")
    if not options.get("ConnectTimeout", "").isdigit():
        fail("fake ssh: missing or wrong option ConnectTimeout")
    extra = set(options) - set(EXPECTED) - {"UserKnownHostsFile", "ConnectTimeout"}
    if extra:
        fail(f"fake ssh: unexpected option {sorted(extra)[0]}")
    for flag in ("-i", "-p", "-l"):
        if flag not in flags:
            fail(f"fake ssh: missing {flag}")
    rest = argv[i + 1 :] if i < len(argv) else []
    if len(rest) != 2:
        fail("fake ssh: expected -- <host> <command>")
    if "SSH_AUTH_SOCK" in os.environ:
        fail("fake ssh: SSH_AUTH_SOCK must not be passed to ssh")
    return rest[0], rest[1], {**flags, "knownHosts": known_hosts}


def main() -> None:
    with open(os.environ["FAKE_SSH_CONFIG"], encoding="utf-8") as handle:
        config = json.load(handle)
    argv = sys.argv[1:]
    stdin = sys.stdin.buffer.read() if not sys.stdin.closed else b""
    with open(config["log"], "a", encoding="utf-8") as handle:
        entry = {"argv": argv, "env": dict(os.environ), "stdin": stdin.decode("utf-8", "replace")}
        handle.write(json.dumps(entry) + "\n")
    host, command, flags = parse(argv)
    mode = config.get("modes", {}).get(host, "ok")
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    verb = tokens[1] if len(tokens) > 1 else ""
    mode = config.get("verbModes", {}).get(verb, mode)
    user, port = flags["-l"], flags["-p"]
    if mode == "timeout":
        time.sleep(60)
    if mode == "slow":
        time.sleep(float(config.get("sleep", 1)))
    if mode == "hostkey":
        fail(
            f"No ED25519 host key is known for {host} and you have requested strict checking.\nHost key verification failed."
        )
    if mode == "auth":
        fail(f"{user}@{host}: Permission denied (publickey).")
    if mode == "refused":
        fail(f"ssh: connect to host {host} port {port}: Connection refused")
    if mode == "connect_timeout":
        fail(f"ssh: connect to host {host} port {port}: Connection timed out")
    if mode == "no_dispatcher":
        fail(f"bash: line 1: {shlex.split(command)[0]}: command not found", 127)
    if mode == "garbage":
        sys.stdout.write("Welcome to example-host!\n")
        sys.exit(0)

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": config["home"],
        "SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22",
        "SWITCHBOARD_REMOTE_TEST": "1",
        "SWITCHBOARD_REMOTE_TEST_TMUX": FAKE_TMUX,
        # `clear` skips its waits (about 2.5 s per call); tests/remote/test_dispatcher_clear.py
        # checks them.
        "SWITCHBOARD_REMOTE_TEST_CLEAR_NO_WAIT": "1",
        "FAKE_TMUX_SCENARIO": config["scenario"],
        "FAKE_TMUX_LOG": config["tmuxLog"],
    }
    explicit = config.get("explicit")
    if explicit:
        tokens = shlex.split(command)
        if not tokens or tokens[0] != explicit:
            fail(f"bash: line 1: {tokens[0] if tokens else ''}: command not found", 127)
        dispatcher_argv = tokens[1:]
    else:
        env["SSH_ORIGINAL_COMMAND"] = command
        dispatcher_argv = list(config.get("forced", []))
    done = subprocess.run(
        [sys.executable, "-I", os.path.normpath(config.get("dispatcherPath") or DISPATCHER), *dispatcher_argv],
        env=env,
        input=stdin,
        capture_output=True,
        check=False,
    )
    if mode == "hang_after":
        time.sleep(60)
    sys.stdout.buffer.write(done.stdout)
    sys.stderr.buffer.write(done.stderr)
    sys.exit(done.returncode)


if __name__ == "__main__":
    main()
