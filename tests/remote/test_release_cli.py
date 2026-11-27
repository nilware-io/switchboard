"""CLI: `release build`, `release sign`, `release verify`, `release publish`, `release list` and
`remote-update` (temporary keys, the fake ssh running an installed dispatcher copy)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from tests.remote.helpers import RemoteWorld, machine
from tests.remote.release_helpers import fingerprint, install_machine, needs_openssh, new_key, signers_line
from tests.remote.test_cli_remote import write_config
from tests.test_cli import run_cli
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import release as r
from voice_coding_mcp.remote.audit import read_audit

pytestmark = needs_openssh


def _source(tmp: Path, version: str) -> Path:
    """A copy of deploy/remote with the dispatcher's VERSION set to `version`."""
    source = tmp / "source"
    source.mkdir()
    for target, data in r.source_files().items():
        content = data
        if target == d.UPDATE_PRODUCT:
            content = data.replace(f'VERSION = "{d.VERSION}"'.encode(), f'VERSION = "{version}"'.encode(), 1)
        (source / target).write_bytes(content)
    return source


async def test_build_sign_verify_publish_list(tmp_path: Path) -> None:
    key = new_key(tmp_path, "release")
    source = _source(tmp_path, "0.13.1")
    out = tmp_path / "out"
    out.mkdir()
    code, io = await run_cli("release", "build", "--source", str(source), "--out", str(out))
    assert code == 0, io.stderr
    built = out / "switchboard-remote-0.13.1"
    manifest = json.loads((built / "manifest.json").read_text())
    assert manifest["version"] == "0.13.1"
    assert [f["target"] for f in manifest["files"]] == list(d.UPDATE_TARGETS)
    code, io = await run_cli("release", "build", "--source", str(source), "--out", str(out))
    assert code == 1
    assert "already exists" in io.stderr
    code, io = await run_cli("release", "sign", "--key", str(key), str(built))
    assert code == 0, io.stderr
    bundle = out / "switchboard-remote-0.13.1.bundle"
    assert fingerprint(key) in io.stdout
    signers = tmp_path / "release_signers"
    signers.write_text(signers_line(key))
    code, io = await run_cli("release", "verify", "--signers", str(signers), str(bundle))
    assert code == 0, io.stderr
    assert io.stdout.startswith("Good signature: switchboard-remote 0.13.1")
    other = new_key(tmp_path, "other")
    signers.write_text(signers_line(other))
    code, io = await run_cli("release", "verify", "--signers", str(signers), str(bundle))
    assert code == 1
    assert "not valid for these signers" in io.stderr
    world = RemoteWorld.create(tmp_path)
    config = write_config(tmp_path, world, [machine("dev", world=world)])
    code, io = await run_cli("release", "list", "--config", config)
    assert (code, io.stdout) == (0, "No release is published.\n")
    code, io = await run_cli("release", "publish", str(bundle), "--config", config)
    assert code == 0, io.stderr
    stored = tmp_path / "state" / "releases" / "switchboard-remote-0.13.1.bundle"
    assert stored.read_bytes() == bundle.read_bytes()
    assert stored.stat().st_mode & 0o777 == 0o600
    code, io = await run_cli("release", "list", "--config", config)
    assert code == 0
    assert io.stdout.startswith("switchboard-remote 0.13.1 (files switchboard-remote, switchboard-connect")
    # The same bytes again are fine; other bytes under a published version are refused.
    assert (await run_cli("release", "publish", str(bundle), "--config", config))[0] == 0
    tampered = tmp_path / "tampered.bundle"
    tampered.write_bytes(bundle.read_bytes().replace(b'"chunk":0', b'"chunk":1', 1))
    code, io = await run_cli("release", "publish", str(tampered), "--config", config)
    assert code == 1
    assert "Error:" in io.stderr


async def test_sign_needs_the_key_file(tmp_path: Path) -> None:
    code, io = await run_cli("release", "sign", "--key", str(tmp_path / "missing"), str(tmp_path))
    assert code == 1
    assert "no key file" in io.stderr


async def test_remote_update_shows_then_installs_with_yes(tmp_path: Path) -> None:
    key = new_key(tmp_path, "release")
    world = RemoteWorld.create(tmp_path, forced=["--sessions", "fpga", "--allow-self-update"])
    dispatcher = install_machine(Path(world.home), key)
    world.configure(dispatcherPath=str(dispatcher))
    ssh_key = tmp_path / "ssh-key"
    ssh_key.write_text("k")
    ssh_key.chmod(0o600)
    config = write_config(tmp_path, world, [machine("dev", world=world, identity=str(ssh_key))])
    source = _source(tmp_path, "0.13.1")
    files = r.source_files(str(source))
    built = r.write_build(str(tmp_path), files, r.build_manifest(files, created=r.utc_now()))
    path, _ = r.sign_build(built, str(key), run=_quiet_sign)
    code, io = await run_cli("release", "publish", path, "--config", config)
    assert code == 0, io.stderr
    env = {"PATH": os.environ["PATH"]}
    code, io = await run_cli("remote-update", "dev", "--config", config, env=env)
    assert code == 0, io.stderr
    assert io.stdout.startswith(f"dev: switchboard-remote {d.VERSION} -> switchboard-remote 0.13.1")
    assert io.stdout.endswith("Nothing was changed. Run again with --yes to install it.\n")
    assert dispatcher.read_bytes() == r.source_files()[d.UPDATE_PRODUCT]
    code, io = await run_cli("remote-update", "dev", "--yes", "--config", config, env=env)
    assert code == 0, io.stderr
    assert io.stdout.endswith("Installed 0.13.1 on dev.\n")
    assert dispatcher.read_bytes() == files[d.UPDATE_PRODUCT]
    audit = read_audit(str(tmp_path / "state"))[0]
    assert (audit[-1]["event"], audit[-1]["client"], audit[-1]["outcome"]) == ("remote_update", "cli", "sent")
    incidents = (tmp_path / "state" / "incidents.jsonl").read_text()
    assert '"code":"update_installed"' in incidents
    code, io = await run_cli("remote-update", "dev", "--config", config, env=env)
    assert code == 0
    assert "is not older than the newest release" in io.stdout


def _quiet_sign(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), capture_output=True, check=False, stdin=subprocess.DEVNULL).returncode
