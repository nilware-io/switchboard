"""deploy/linux/sb-linux against fake `tailscale`, `sudo` and `systemctl` (tests/fixtures/fake_linux_host.py)
and a real `voice-coding-mcp serve` on a loopback port, in both layouts: `service` (the dedicated hub
user under systemd) and `user` (the hub runs as the owner). Nothing here touches the real Tailscale,
systemd, sudo or the owner's home: PATH starts with a directory of fakes and HOME is a temp dir."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FIXTURES, claude_config

REPO = Path(__file__).resolve().parent.parent
SB_LINUX = REPO / "deploy" / "linux" / "sb-linux"
FAKE_HOST = FIXTURES / "fake_linux_host.py"
URL = "https://example-host.example-tailnet.ts.net"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("flock") is None, reason="sb-linux runs on Linux (needs util-linux flock)"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def healthz(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return bool(response.status == 200)
    except OSError:
        return False


@dataclass
class Host:
    root: Path
    home: Path
    host: Path
    config: Path
    port: int
    env: dict[str, str]

    def run(self, *args: str, mode: str = "user", timeout: float = 60, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SB_LINUX), *args],
            env={**self.env, "SB_MODE": mode, **env},
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=self.home,
            check=False,
        )

    def calls(self, tool: str | None = None) -> list[list[str]]:
        path = self.host / "calls.jsonl"
        if not path.exists():
            return []
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [c["argv"] for c in lines if tool is None or c["tool"] == tool]

    def funnel(self) -> Any:
        path = self.host / "ts.json"
        return json.loads(path.read_text(encoding="utf-8"))["funnel"] if path.exists() else None

    def public_url(self) -> Any:
        return json.loads(self.config.read_text(encoding="utf-8"))["publicUrl"]

    def ts_mode(self, mode: str) -> None:
        (self.host / "ts_mode").write_text(mode, encoding="utf-8")


def _kill_pid_file(path: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def host(real_tmp: Path) -> Iterator[Host]:
    home = real_tmp / "home"
    home.mkdir()
    fake_host = real_tmp / "host"
    fake_host.mkdir()
    bin_dir = real_tmp / "bin"
    bin_dir.mkdir()
    for tool in ("tailscale", "sudo", "systemctl"):
        wrapper = bin_dir / tool
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_HOST}" {tool} "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    vcm = bin_dir / "voice-coding-mcp"
    vcm.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m voice_coding_mcp "$@"\n', encoding="utf-8")
    vcm.chmod(0o755)
    projects = real_tmp / "projects"
    (projects / "alpha").mkdir(parents=True)
    claude_home = real_tmp / "claude-home"
    claude_home.mkdir()
    port = free_port()
    claude = claude_config(str(real_tmp / "record.jsonl"))
    config = real_tmp / "config.json"
    config.write_text(
        json.dumps(
            {
                "publicUrl": "https://placeholder.example.com",
                "listen": {"host": "127.0.0.1", "port": port},
                "stateDir": str(real_tmp / "state"),
                "projectRoots": [str(projects)],
                "claude": {"bin": claude.bin, "configDir": str(claude_home), "env": dict(claude.env)},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    (fake_host / "service.json").write_text(
        json.dumps({"argv": [str(vcm), "serve", "--config", str(config)]}), encoding="utf-8"
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "FAKE_HOST_DIR": str(fake_host),
        "SB_CONFIG": str(config),
        "SB_VCM": str(vcm),
        "SB_PORT": str(port),
        "SB_USER": "switchboard-test",
        "SB_SERVICE": "switchboard-test",
    }
    h = Host(root=real_tmp, home=home, host=fake_host, config=config, port=port, env=env)
    try:
        yield h
    finally:
        _kill_pid_file(fake_host / "service.pid")
        _kill_pid_file(home / ".local" / "state" / "sb-linux" / "server.pid")


# --- user mode ------------------------------------------------------------------------------------


def test_user_mode_on_status_off(host: Host) -> None:
    on = host.run("on")
    assert on.returncode == 0, on.stderr
    assert f"Set publicUrl in {host.config} to {URL}." in on.stdout
    assert f"On. Connector URL: {URL}/mcp" in on.stdout
    assert f"To pair a new client: {host.env['SB_VCM']} pair --config {host.config}" in on.stdout
    assert host.public_url() == URL
    assert healthz(host.port)
    # 127.0.0.1 is the CLI's default target, so the bare port is passed, as vcm-mac does.
    assert host.funnel() == str(host.port)
    assert ["funnel", "--bg", str(host.port)] in host.calls("tailscale")
    assert host.calls("sudo") == [], "user mode needs no sudo"
    assert host.calls("systemctl") == []
    run_dir = host.home / ".local" / "state" / "sb-linux"
    assert (run_dir.stat().st_mode & 0o777) == 0o700

    status = host.run("status")
    assert status.returncode == 0, status.stderr
    assert "State: on" in status.stdout
    assert f"Connector URL: {URL}/mcp" in status.stdout
    assert "Mode: user" in status.stdout

    again = host.run("on")
    assert again.returncode == 0, again.stderr
    assert "Server already running" in again.stdout
    assert "Set publicUrl" not in again.stdout

    off = host.run("off")
    assert off.returncode == 0, off.stderr
    assert "Funnel closed." in off.stdout
    assert "Stopping the server" in off.stdout
    assert off.stdout.rstrip().endswith("Off.")
    assert host.funnel() is None
    assert not healthz(host.port)
    assert not (run_dir / "server.pid").exists()

    twice = host.run("off")
    assert twice.returncode == 0, twice.stderr
    assert twice.stdout == "Off.\n"
    assert "State: off" in host.run("status").stdout


def test_a_funnel_that_needs_approval_prints_the_link_and_stops_the_server(host: Host) -> None:
    host.ts_mode("approval")
    on = host.run("on")
    assert on.returncode == 1
    assert "https://login.tailscale.com/f/funnel?node=nEXAMPLE" in on.stderr
    assert "Open this link, approve it, then run sb-linux on again" in on.stderr
    assert not healthz(host.port)
    assert host.funnel() is None


def test_a_user_who_is_not_the_tailscale_operator_gets_the_operator_hint(host: Host) -> None:
    host.ts_mode("denied")
    on = host.run("on")
    assert on.returncode == 1
    assert "sudo tailscale set --operator=" in on.stderr
    assert not healthz(host.port)


def test_a_disconnected_tailscale_stops_before_anything_starts(host: Host) -> None:
    host.ts_mode("stopped")
    on = host.run("on")
    assert on.returncode == 1
    assert "Tailscale is not connected (state: Stopped)" in on.stderr
    assert host.public_url() == "https://placeholder.example.com"
    assert not healthz(host.port)
    assert [c for c in host.calls("tailscale") if c[:1] == ["funnel"]] == []


def test_off_leaves_a_funnel_to_another_port_alone(host: Host) -> None:
    (host.host / "ts.json").write_text(json.dumps({"funnel": "9"}), encoding="utf-8")
    off = host.run("off")
    assert off.returncode == 0, off.stderr
    assert ["funnel", "--https=443", "off"] not in host.calls("tailscale")
    assert host.funnel() == "9"


def test_a_missing_config_is_named(host: Host) -> None:
    out = host.run("on", SB_CONFIG=str(host.root / "nope.json"))
    assert out.returncode == 1
    assert "Config not found" in out.stderr


# --- service mode ---------------------------------------------------------------------------------


def test_service_mode_on_status_off(host: Host) -> None:
    vcm = host.env["SB_VCM"]
    on = host.run("on", mode="service")
    assert on.returncode == 0, on.stderr
    # publicUrl is set by the hub user's own CLI (no direct file edit by the owner), then the unit starts.
    assert host.calls("sudo")[:2] == [
        ["-H", "-u", "switchboard-test", "--", vcm, "set-public-url", URL, "--config", str(host.config)],
        ["systemctl", "start", "switchboard-test"],
    ]
    assert host.public_url() == URL
    assert healthz(host.port)
    assert host.funnel() == str(host.port)
    assert "Started the switchboard-test service." in on.stdout
    assert f"To pair a new client: sudo -H -u switchboard-test {vcm} pair --config {host.config}" in on.stdout

    status = host.run("status", mode="service")
    assert status.returncode == 0, status.stderr
    assert "State: on" in status.stdout
    assert f"Service: switchboard-test (active, port {host.port})" in status.stdout
    assert "Pairing window" in status.stdout or "pairing" in status.stdout.lower()

    again = host.run("on", mode="service")
    assert again.returncode == 0, again.stderr
    assert "already running" in again.stdout
    assert ["systemctl", "restart", "switchboard-test"] not in host.calls("sudo")

    # A changed address (for example a renamed tailnet node) restarts the unit for the new publicUrl.
    data = json.loads(host.config.read_text(encoding="utf-8"))
    host.config.write_text(json.dumps({**data, "publicUrl": "https://old.example.com"}), encoding="utf-8")
    moved = host.run("on", mode="service")
    assert moved.returncode == 0, moved.stderr
    assert "Restarting the switchboard-test service" in moved.stdout
    assert ["systemctl", "restart", "switchboard-test"] in host.calls("sudo")
    assert healthz(host.port)

    off = host.run("off", mode="service")
    assert off.returncode == 0, off.stderr
    assert "Funnel closed." in off.stdout
    assert ["systemctl", "stop", "switchboard-test"] in host.calls("sudo")
    assert host.funnel() is None
    deadline = time.monotonic() + 10
    while healthz(host.port) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not healthz(host.port)
    assert "State: off" in host.run("status", mode="service").stdout


def test_service_mode_stops_the_unit_when_the_funnel_fails(host: Host) -> None:
    host.ts_mode("approval")
    on = host.run("on", mode="service")
    assert on.returncode == 1
    assert ["systemctl", "stop", "switchboard-test"] in host.calls("sudo")


def test_service_mode_refuses_to_run_as_the_hub_user(host: Host) -> None:
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    out = host.run("on", mode="service", SB_USER=me)
    assert out.returncode == 1
    assert "Run sb-linux as yourself" in out.stderr
    assert host.calls() == []


def test_service_mode_reports_a_port_mismatch(host: Host) -> None:
    out = host.run("on", mode="service", SB_PORT=str(free_port()), SB_START_SECONDS="3")
    assert out.returncode == 1
    assert "did not answer /healthz" in out.stderr
    assert "within 3 s (is SB_PORT the port in its config?)" in out.stderr
    assert ["systemctl", "stop", "switchboard-test"] in host.calls("sudo"), "the unit is not left running"
    assert host.funnel() is None


def test_on_and_off_wait_for_no_one_while_another_run_holds_the_lock(host: Host) -> None:
    run_dir = host.home / ".local" / "state" / "sb-linux"
    run_dir.mkdir(parents=True)
    with open(run_dir / "sb-linux.lock", "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        for command in ("on", "off"):
            out = host.run(command)
            assert out.returncode == 1
            assert "Another sb-linux on/off is running" in out.stderr
    assert host.calls() == []


# --- arguments and lint ---------------------------------------------------------------------------


def test_usage_and_bad_arguments(host: Host) -> None:
    helped = host.run("--help")
    assert helped.returncode == 0
    assert helped.stdout.startswith("sb-linux: run the Switchboard hub")
    assert "SB_TAILSCALE" in helped.stdout
    assert host.run().returncode == 2
    assert host.run("frobnicate").returncode == 2
    extra = host.run("on", "now")
    assert extra.returncode == 1
    assert "it takes no arguments" in extra.stderr
    bad = host.run("status", mode="system")
    assert bad.returncode == 2
    assert "SB_MODE must be service or user" in bad.stderr


def test_the_script_is_executable_bash() -> None:
    assert os.access(SB_LINUX, os.X_OK)
    assert SB_LINUX.read_text(encoding="utf-8").startswith("#!/bin/bash\n")
    subprocess.run(["bash", "-n", str(SB_LINUX)], check=True, timeout=10)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed (CI runs it)")
def test_shellcheck_is_clean() -> None:
    subprocess.run(["shellcheck", str(SB_LINUX)], check=True, timeout=60)
