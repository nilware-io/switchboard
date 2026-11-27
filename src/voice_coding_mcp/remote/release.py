"""Signed releases of the machine programs (docs/SWITCHBOARD.md, "Self-update").

A release bundle carries `switchboard-remote`, `switchboard-connect` and `switchboard-hook` with a
manifest (version, minimum installed version, creation time, and per file its size and SHA-256)
signed by the owner's OpenSSH release key (`ssh-keygen -Y sign`, namespace `switchboard-release`).
The owner builds and signs it on their own computer (`release build`, `release sign`), then
publishes it into the hub's state directory (`release publish`); the hub only stores it and hands
it to a machine's dispatcher (`update`), which checks everything again against its own pinned
signers file. The hub never signs and never runs anything of a bundle.

The bundle format and every check are the dispatcher's own (`dispatcher.read_bundle`,
`parse_manifest`, `parse_signature`, `verify_signature`), so hub, CLI and machines read a bundle the
same way.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import dispatcher as d

HERE = Path(__file__).resolve().parent
SOURCE_FILES: dict[str, Path] = {
    "switchboard-remote": HERE / "dispatcher.py",
    "switchboard-connect": HERE / "connector.py",
    "switchboard-hook": HERE / "hook.py",
}
"""The bundled programs, byte-identical to `deploy/remote/<target>` (the tests check it)."""
RELEASES_DIR = "releases"
"""Under `stateDir`: one `switchboard-remote-<version>.bundle` per published release (0600)."""
BUNDLE_SUFFIX = ".bundle"
MANIFEST_NAME = "manifest.json"
FIRST_UPDATABLE = "0.13.0"
"""The first dispatcher with the `update` verb: the default `minVersion` of a new bundle."""
SSH_KEYGEN = "ssh-keygen"
"""`release sign` runs the owner's own ssh-keygen from PATH (on the owner's computer)."""


Runner = Callable[[Sequence[str]], int]


class ReleaseError(Exception):
    """A bundle that cannot be built, signed, read or published; `code` is a fixed word (the
    dispatcher's refusal code when one of its checks said no)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Release:
    """A bundle as read on the hub. Its signature is not verified here (the hub cannot make the
    machines trust anything); `fingerprint` is the key that signed it, for the read-back."""

    path: str
    version: str
    min_version: str
    created: str
    fingerprint: str
    key_type: str
    sha256: str
    """Of the whole bundle file: a confirmation is bound to exactly these bytes."""
    size: int
    targets: tuple[str, ...]


def source_files(source: str | None = None) -> dict[str, bytes]:
    """The three programs from `source` (a directory with files named like the targets, such as
    `deploy/remote`), or the copies bundled in this package."""
    files: dict[str, bytes] = {}
    for target, bundled in SOURCE_FILES.items():
        path = Path(source) / target if source is not None else bundled
        try:
            files[target] = path.read_bytes()
        except OSError as err:
            raise ReleaseError("missing", f"cannot read {path}: {err.strerror or err}") from None
    return files


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_manifest(files: Mapping[str, bytes], *, created: str, min_version: str = FIRST_UPDATABLE) -> bytes:
    """The manifest of `files`: its version is the dispatcher's own `VERSION`, so a bundle cannot
    name another version than the file it carries."""
    version = d.file_version(files.get(d.UPDATE_PRODUCT, b""))
    if version is None:
        raise ReleaseError("bad_manifest", "the dispatcher has no VERSION line")
    manifest = {
        "format": d.UPDATE_FORMAT,
        "product": d.UPDATE_PRODUCT,
        "version": version,
        "minVersion": min_version,
        "created": created,
        "files": [
            {"target": target, "size": len(files[target]), "sha256": hashlib.sha256(files[target]).hexdigest()}
            for target in d.UPDATE_TARGETS
            if target in files
        ],
    }
    raw = (json.dumps(manifest, indent=2) + "\n").encode("ascii")
    _check(lambda: d.parse_manifest(raw))
    return raw


def bundle_bytes(manifest: bytes, signature: str, files: Mapping[str, bytes]) -> bytes:
    """The bundle's JSON lines: the head (manifest as base64, the armored signature), each file's
    chunks in the manifest's order, `{"done": true}`."""
    entries = json.loads(manifest)["files"]
    lines = [
        json.dumps(
            {"bundle": d.UPDATE_FORMAT, "manifest": base64.b64encode(manifest).decode("ascii"), "signature": signature},
            separators=(",", ":"),
        )
    ]
    for entry in entries:
        target = entry["target"]
        data = files[target]
        for index in range(-(-len(data) // d.UPDATE_CHUNK_BYTES)):
            part = data[index * d.UPDATE_CHUNK_BYTES : (index + 1) * d.UPDATE_CHUNK_BYTES]
            chunk = {"file": target, "chunk": index, "data": base64.b64encode(part).decode("ascii")}
            lines.append(json.dumps(chunk, separators=(",", ":")))
    lines.append(json.dumps({"done": True}))
    return ("\n".join(lines) + "\n").encode("ascii")


def _check[T](step: Callable[[], T]) -> T:
    """Runs one of the dispatcher's checks; its refusal becomes a ReleaseError with the same code."""
    try:
        return step()
    except d.RefusedError as refused:
        raise ReleaseError(refused.code, REFUSALS.get(refused.code, refused.code)) from None


REFUSALS: dict[str, str] = {
    "too_large": "the bundle is larger than a machine accepts",
    "bad_bundle": "this is not a release bundle (or it is cut or altered)",
    "unknown_target": "the bundle names a file that is none of switchboard-remote, switchboard-connect, switchboard-hook",
    "bad_signature": "the signature is not valid for these signers",
    "wrong_namespace": "the signature was made for another purpose (namespace is not switchboard-release)",
    "no_openssh": "ssh-keygen was not found",
    "old_openssh": "ssh-keygen is older than OpenSSH 8.1 (no -Y verify)",
    "bad_manifest": "the manifest is not valid",
    "file_mismatch": "a file does not match its size or SHA-256 in the signed manifest",
}


def read_release(path: str) -> Release:
    """A published or built bundle, read and checked as far as the hub can: its form, the
    signature's namespace and key, the manifest, every file's size and SHA-256. Raises
    ReleaseError."""
    data = _read_file(path, d.MAX_UPDATE_INPUT_BYTES)
    return _release_of(path, data)


def _release_of(path: str, data: bytes) -> Release:
    manifest_raw, signature, files = _check(lambda: d.read_bundle(data))
    signed = _check(lambda: d.parse_signature(signature))
    if signed["namespace"] != d.UPDATE_NAMESPACE:
        raise ReleaseError("wrong_namespace", REFUSALS["wrong_namespace"])
    manifest = _check(lambda: d.parse_manifest(manifest_raw))
    entries = manifest["files"]
    assert isinstance(entries, list)
    listed = {str(entry["target"]): entry for entry in entries}
    if set(listed) != set(files) or any(
        len(files[t]) != e["size"] or hashlib.sha256(files[t]).hexdigest() != e["sha256"] for t, e in listed.items()
    ):
        raise ReleaseError("file_mismatch", REFUSALS["file_mismatch"])
    return Release(
        path=path,
        version=str(manifest["version"]),
        min_version=str(manifest["minVersion"]),
        created=str(manifest["created"]),
        fingerprint=signed["fingerprint"],
        key_type=signed["keyType"],
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        targets=tuple(listed),
    )


def _read_file(path: str, limit: int) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as err:
        raise ReleaseError("missing", f"cannot open {path}: {err.strerror or err}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseError("missing", f"{path} is not a regular file")
        if info.st_size > limit:
            raise ReleaseError("too_large", REFUSALS["too_large"])
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ReleaseError("too_large", REFUSALS["too_large"])
    return data


def verify_release(path: str, signers: str, *, keygen: str = d.SSH_KEYGEN) -> Release:
    """`read_release` plus the machines' own signature check (`ssh-keygen -Y verify` against the
    allowed-signers file `signers`, principal and namespace `switchboard-release`)."""
    data = _read_file(path, d.MAX_UPDATE_INPUT_BYTES)
    release = _release_of(path, data)
    signers_bytes = _read_file(signers, d.MAX_SIGNERS_BYTES)
    manifest_raw, signature, _ = _check(lambda: d.read_bundle(data))
    problem = d.openssh_problem(keygen)
    if problem is not None:
        raise ReleaseError(problem, REFUSALS[problem])
    _check(lambda: d.verify_signature(keygen, signers_bytes, manifest_raw, signature))
    return release


def write_build(out_dir: str, files: Mapping[str, bytes], manifest: bytes) -> str:
    """`<out_dir>/switchboard-remote-<version>/` with `manifest.json` and the files (to read and
    sign); refused when it exists."""
    version = json.loads(manifest)["version"]
    directory = os.path.join(out_dir, f"{d.UPDATE_PRODUCT}-{version}")
    try:
        os.makedirs(directory, mode=0o755)
    except FileExistsError:
        raise ReleaseError("exists", f"{directory} already exists; remove it or choose another --out") from None
    for name, data in (*files.items(), (MANIFEST_NAME, manifest)):
        mode = 0o644 if name == MANIFEST_NAME else 0o755
        fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    return directory


def sign_build(directory: str, key: str, *, keygen: str = SSH_KEYGEN, run: Runner | None = None) -> tuple[str, Release]:
    """Signs `<directory>/manifest.json` with the owner's key (`ssh-keygen -Y sign -f <key> -n
    switchboard-release`, which asks for the key's passphrase on the terminal itself; it is never
    seen here), checks the files against the manifest and writes `<directory>.bundle`. Returns its
    path and what it holds."""
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    signature_path = manifest_path + ".sig"
    if not os.path.isfile(key):
        raise ReleaseError("missing", f"no key file {key}")
    manifest = _read_file(manifest_path, d.MAX_MANIFEST_BYTES)
    parsed = _check(lambda: d.parse_manifest(manifest))
    entries = parsed["files"]
    assert isinstance(entries, list)
    files = {
        str(e["target"]): _read_file(os.path.join(directory, str(e["target"])), d.MAX_UPDATE_TOTAL_BYTES)
        for e in entries
    }
    with contextlib.suppress(FileNotFoundError):
        os.unlink(signature_path)
    argv = [keygen, "-Y", "sign", "-f", key, "-n", d.UPDATE_NAMESPACE, manifest_path]
    code = (run or _run_interactive)(argv)
    if code != 0:
        raise ReleaseError("sign_failed", f"ssh-keygen -Y sign failed (exit {code})")
    signature = _read_file(signature_path, d.MAX_SIGNATURE_BYTES).decode("ascii", "replace")
    bundle = bundle_bytes(manifest, signature, files)
    out = directory.rstrip("/") + BUNDLE_SUFFIX
    release = _release_of(out, bundle)
    _write_new(out, bundle, 0o644)
    return out, release


def _run_interactive(argv: Sequence[str]) -> int:
    """ssh-keygen on the owner's terminal (stdin, stdout and stderr inherited, for its passphrase
    prompt), from argv without a shell."""
    return subprocess.run(list(argv), check=False).returncode


def _write_new(path: str, data: bytes, mode: int) -> None:
    """Writes `data` as a new file `path` atomically (refused when it exists with other bytes)."""
    with contextlib.suppress(FileNotFoundError):
        if Path(path).read_bytes() == data:
            return
        raise ReleaseError("exists", f"{path} already exists with other contents")
    temporary = f"{path}.tmp-{secrets.token_hex(6)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise ReleaseError("exists", f"{path} already exists") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def releases_dir(state_dir: str) -> str:
    return os.path.join(state_dir, RELEASES_DIR)


def publish(path: str, state_dir: str) -> Release:
    """Copies a signed bundle into `stateDir/releases/switchboard-remote-<version>.bundle` (0600)
    after `read_release`. A version already published with other bytes is refused."""
    data = _read_file(path, d.MAX_UPDATE_INPUT_BYTES)
    release = _release_of(path, data)
    directory = releases_dir(state_dir)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    target = os.path.join(directory, f"{d.UPDATE_PRODUCT}-{release.version}{BUNDLE_SUFFIX}")
    _write_new(target, data, 0o600)
    return _release_of(target, data)


def published(state_dir: str) -> tuple[list[Release], list[str]]:
    """Every published release, oldest version first, and the files that are not valid bundles."""
    directory = releases_dir(state_dir)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return [], []
    except OSError as err:
        return [], [f"{directory}: {err.strerror or err}"]
    found: list[Release] = []
    problems: list[str] = []
    for name in names:
        if not name.endswith(BUNDLE_SUFFIX) or name.startswith("."):
            continue
        try:
            found.append(read_release(os.path.join(directory, name)))
        except ReleaseError as err:
            problems.append(f"{name}: {err}")
    found.sort(key=lambda release: d.version_tuple(release.version) or (0, 0, 0))
    return found, problems


def latest(state_dir: str) -> Release | None:
    releases, _ = published(state_dir)
    return releases[-1] if releases else None


def load_bundle(release: Release) -> bytes:
    """The bundle's bytes again, refused (`changed`) unless they are exactly the ones read back."""
    data = _read_file(release.path, d.MAX_UPDATE_INPUT_BYTES)
    if hashlib.sha256(data).hexdigest() != release.sha256:
        raise ReleaseError("changed", "the published bundle changed since it was read back")
    return data


def newer(release: Release, installed: str | None) -> bool:
    """The release is newer than an installed dispatcher version (unknown: not newer)."""
    have, offer = d.version_tuple(installed), d.version_tuple(release.version)
    return have is not None and offer is not None and offer > have
