"""Signed release bundles for the self-update tests: temporary OpenSSH keys (never a real key),
machines laid out in a temporary HOME, bundles built with `remote/release.py` and signed with
`ssh-keygen -Y sign`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import release as r

REPO = Path(__file__).resolve().parents[2]


def _openssh() -> tuple[int, int] | None:
    if not os.path.isfile(d.SSH_KEYGEN):
        return None
    try:
        done = subprocess.run(["/usr/bin/ssh", "-V"], capture_output=True, check=False, timeout=10)
    except OSError:
        return None
    found = re.search(r"OpenSSH_([0-9]+)\.([0-9]+)", (done.stderr + done.stdout).decode("utf-8", "replace"))
    return (int(found.group(1)), int(found.group(2))) if found else None


OPENSSH = _openssh()
needs_openssh = pytest.mark.skipif(
    OPENSSH is None or OPENSSH < d.MIN_OPENSSH,
    reason=f"needs OpenSSH 8.1 or newer at {d.SSH_KEYGEN} (ssh-keygen -Y sign and verify); found {OPENSSH}",
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def new_key(directory: Path, name: str) -> Path:
    """A fresh ed25519 key without a passphrase, for this test only."""
    path = directory / name
    subprocess.run(
        [d.SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", f"test {name}", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def signers_line(key: Path, namespaces: str = "switchboard-release") -> str:
    kind, blob = Path(f"{key}.pub").read_text().split()[:2]
    return f'switchboard-release namespaces="{namespaces}" {kind} {blob}\n'


def fingerprint(key: Path) -> str:
    done = subprocess.run([d.SSH_KEYGEN, "-lf", f"{key}.pub"], check=True, capture_output=True, text=True)
    return done.stdout.split()[1]


def programs(version: str = "0.13.1", *, dispatcher: bytes | None = None) -> dict[str, bytes]:
    """The three programs as bundled, the dispatcher's VERSION set to `version`."""
    files = r.source_files()
    source = files[d.UPDATE_PRODUCT] if dispatcher is None else dispatcher
    files[d.UPDATE_PRODUCT] = re.sub(rb'(?m)^VERSION = "[0-9.]+"$', f'VERSION = "{version}"'.encode(), source, count=1)
    return files


def sign(data: bytes, key: Path, work: Path, namespace: str = d.UPDATE_NAMESPACE) -> str:
    """`ssh-keygen -Y sign` of `data` (in a scratch file under `work`)."""
    work.mkdir(parents=True, exist_ok=True)
    target = work / f"signed-{os.urandom(4).hex()}"
    target.write_bytes(data)
    subprocess.run(
        [d.SSH_KEYGEN, "-Y", "sign", "-f", str(key), "-n", namespace, str(target)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return Path(f"{target}.sig").read_text(encoding="ascii")


def bundle(
    key: Path,
    work: Path,
    *,
    version: str = "0.13.1",
    files: dict[str, bytes] | None = None,
    namespace: str = d.UPDATE_NAMESPACE,
    min_version: str = r.FIRST_UPDATABLE,
    manifest: bytes | None = None,
) -> bytes:
    """A signed bundle of `files` (default: `programs(version)`)."""
    content = programs(version) if files is None else files
    raw = manifest if manifest is not None else r.build_manifest(content, created=r.utc_now(), min_version=min_version)
    return r.bundle_bytes(raw, sign(raw, key, work, namespace), content)


def install_machine(home: Path, *keys: Path, signers: str | None = None) -> Path:
    """A machine's HOME: the three programs (as bundled, 0.13.0) in ~/.local/bin and a signers file
    pinning `keys`. Returns the dispatcher's path."""
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o755)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for target, data in r.source_files().items():
        path = bin_dir / target
        path.write_bytes(data)
        path.chmod(0o755)
    config = home / ".config" / "switchboard-remote"
    config.mkdir(parents=True, exist_ok=True)
    (home / ".config").chmod(0o700)
    config.chmod(0o700)
    text = signers if signers is not None else "".join(signers_line(key) for key in keys)
    (config / "release_signers").write_text(text, encoding="ascii")
    (config / "release_signers").chmod(0o644)
    return bin_dir / d.UPDATE_PRODUCT


def allowed() -> d.Policy:
    policy = d.Policy()
    policy.sessions = ["fpga"]
    policy.allow_self_update = True
    return policy
