"""The OpenSSH argv and environment for one dispatcher call (docs/SWITCHBOARD.md, "SSH").

Pure functions: no config file of any kind (`-F /dev/null`), no agent, no forwarding, a pinned
host key from a dedicated known_hosts file, and only the dedicated identity file.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import MachineConfig, SshConfig

SSH_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL")
"""The only variables ssh gets from the server's environment (never SSH_AUTH_SOCK)."""


def hardening_options(ssh: SshConfig, known_hosts: str) -> list[tuple[str, str]]:
    """`-o` options, in argv order. Tests check every one of them."""
    return [
        ("BatchMode", "yes"),
        ("IdentitiesOnly", "yes"),
        ("IdentityAgent", "none"),
        ("ForwardAgent", "no"),
        ("ForwardX11", "no"),
        ("ClearAllForwardings", "yes"),
        ("PermitLocalCommand", "no"),
        ("StrictHostKeyChecking", "yes"),
        ("UserKnownHostsFile", known_hosts),
        ("GlobalKnownHostsFile", "/dev/null"),
        ("UpdateHostKeys", "no"),
        ("HashKnownHosts", "no"),
        ("CheckHostIP", "no"),
        ("ConnectTimeout", str(ssh.connect_timeout_seconds)),
        ("ServerAliveInterval", "5"),
        ("ServerAliveCountMax", "2"),
        ("LogLevel", "ERROR"),
    ]


def build_ssh_argv(ssh: SshConfig, machine: MachineConfig, known_hosts: str, command: str) -> list[str]:
    """argv (executable first) for one call. `machine` must be a validated ssh machine and
    `command` a string from `protocol.build_command`."""
    if machine.host is None or machine.user is None or machine.identity_file is None:
        raise ValueError(f"machine {machine.name} is not an ssh machine")
    argv = [ssh.bin, "-F", "/dev/null", "-T"]
    for key, value in hardening_options(ssh, known_hosts):
        argv += ["-o", f"{key}={value}"]
    argv += ["-i", machine.identity_file, "-p", str(machine.port), "-l", machine.user, "--", machine.host, command]
    return argv


def ssh_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """The environment of ssh, built from scratch."""
    return {key: base_env[key] for key in SSH_ENV_KEYS if key in base_env}
