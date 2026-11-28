"""CLI: `release build`, `release sign`, `release verify`, `release publish`, `release list` and
`remote-update` (temporary keys, the fake ssh running an installed dispatcher copy)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.remote.helpers import RemoteWorld, machine
from tests.remote.release_helpers import (
    bundle as make_bundle,
)
from tests.remote.release_helpers import (
    fingerprint,
    hub_signers,
    install_machine,
    needs_openssh,
    new_key,
    programs,
    sign,
    signers_line,
    trust_own_uid,
)
from tests.remote.test_cli_remote import write_config
from tests.test_cli import run_cli
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import release as r
from voice_coding_mcp.remote.audit import read_audit

pytestmark = needs_openssh


@pytest.fixture(autouse=True)
def _trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    trust_own_uid(monkeypatch)


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
    config = write_config(
        tmp_path, world, [machine("dev", world=world)], releaseSignersFile=str(hub_signers(tmp_path, key))
    )
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
    config = write_config(
        tmp_path,
        world,
        [machine("dev", world=world, identity=str(ssh_key))],
        releaseSignersFile=str(hub_signers(tmp_path, key)),
    )
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


class Hub:
    """A hub config with its own pinned signers file, and bundles to publish."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = new_key(root, "release")
        self.signers = hub_signers(root, self.key)
        world = RemoteWorld.create(root)
        self.config = write_config(root, world, [machine("dev", world=world)], releaseSignersFile=str(self.signers))

    def write(self, name: str, data: bytes) -> str:
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def releases(self) -> list[str]:
        directory = self.root / "state" / "releases"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def events(self) -> str:
        path = self.root / "state" / "incidents.jsonl"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path)


async def test_publish_verifies_on_the_hub(hub: Hub) -> None:
    good = hub.write("good.bundle", make_bundle(hub.key, hub.root / "work"))
    code, io = await run_cli("release", "publish", good, "--config", hub.config)
    assert code == 0, io.stderr
    assert io.stdout.rstrip().endswith(", verified on the hub")
    assert fingerprint(hub.key) in io.stdout
    assert hub.releases() == ["switchboard-remote-0.13.1.bundle"]


def _cases(hub: Hub) -> dict[str, tuple[bytes, str]]:
    work = hub.root / "work"
    files = programs("0.13.1")
    manifest = r.build_manifest(files, created=r.utc_now())
    signature = sign(manifest, hub.key, work)
    forged = manifest.replace(b'"minVersion": "0.13.0"', b'"minVersion": "0.13.1"')
    tampered = dict(files)
    tampered["switchboard-hook"] = files["switchboard-hook"] + b"\n# extra\n"
    return {
        "tampered manifest": (r.bundle_bytes(forged, signature, files), "not valid for these signers"),
        "tampered file": (r.bundle_bytes(manifest, signature, tampered), "does not match its size or SHA-256"),
        "wrong key": (make_bundle(new_key(hub.root, "intruder"), work), "not valid for these signers"),
        "wrong namespace": (make_bundle(hub.key, work, namespace="git"), "namespace is not switchboard-release"),
    }


async def test_publish_refuses_what_does_not_verify(hub: Hub) -> None:
    for name, (data, why) in _cases(hub).items():
        code, io = await run_cli("release", "publish", hub.write(f"{name}.bundle", data), "--config", hub.config)
        assert code == 1, name
        assert why in io.stderr, (name, io.stderr)
    assert hub.releases() == []
    assert hub.events().count('"code":"release_refused"') == 4


async def test_publish_needs_a_trusted_signers_file(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    good = hub.write("good.bundle", make_bundle(hub.key, hub.root / "work"))
    _chmod(hub.signers, 0o664)
    code, io = await run_cli("release", "publish", good, "--config", hub.config)
    assert (code, "owned by root and not writable by group or others" in io.stderr) == (1, True)
    _chmod(hub.signers, 0o644)
    _chmod(hub.signers.parent, 0o775)
    assert (await run_cli("release", "publish", good, "--config", hub.config))[0] == 1
    _chmod(hub.signers.parent, 0o755)
    monkeypatch.setattr(r, "SIGNERS_OWNERS", (0,))
    code, io = await run_cli("release", "publish", good, "--config", hub.config)
    assert (code, "owned by root" in io.stderr) == (1, True)
    trust_own_uid(monkeypatch)
    _unlink(hub.signers)
    code, io = await run_cli("release", "publish", good, "--config", hub.config)
    assert (code, "does not exist" in io.stderr) == (1, True)
    assert hub.releases() == []


def _chmod(path: Path, mode: int) -> None:
    path.chmod(mode)


def _unlink(path: Path) -> None:
    path.unlink()
