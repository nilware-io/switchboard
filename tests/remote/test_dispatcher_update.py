"""The dispatcher's `update` verb and `--rollback` (docs/SWITCHBOARD.md, "Self-update"): signatures
made and checked with real `ssh-keygen` and temporary keys, every refusal (tampering, wrong key,
wrong namespace, unsigned, replay and downgrade, unknown targets, cut and oversized bundles, the
signers file's permissions, OpenSSH too old, the policy flag), the atomic install with `.prev`
copies and the rollback."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.remote.release_helpers import (
    allowed,
    bundle,
    fingerprint,
    install_machine,
    needs_openssh,
    new_key,
    programs,
    sign,
)
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote import release as r

pytestmark = needs_openssh


class Machine:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = new_key(root, "release")
        self.home = root / "home"
        self.dispatcher = install_machine(self.home, self.key)
        self.bin = self.dispatcher.parent
        self.env = {"HOME": str(self.home)}
        self.before = {path.name: path.read_bytes() for path in self.bin.iterdir()}

    def bundle(self, **options: object) -> bytes:
        return bundle(self.key, self.root / "work", **options)  # type: ignore[arg-type]

    def update(self, data: bytes, policy: d.Policy | None = None) -> dict[str, object]:
        try:
            return d.verb_update(policy or allowed(), self.env, data, self_path=str(self.dispatcher))
        except d.RefusedError as refused:
            return {"error": refused.code}

    def unchanged(self) -> bool:
        return {path.name: path.read_bytes() for path in self.bin.iterdir()} == self.before


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path)


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skips the self test (a few seconds per update); the tests that check it do not use this."""
    monkeypatch.setattr(d, "_self_test", lambda target, path, version, env: True)


# --- accepted ------------------------------------------------------------------------------------


def test_a_signed_newer_bundle_is_installed_atomically_with_prev_copies(machine: Machine) -> None:
    data = machine.bundle()
    out = machine.update(data)
    assert out == {
        "updated": True,
        "from": d.VERSION,
        "to": "0.13.1",
        "files": ["switchboard-hook", "switchboard-connect", "switchboard-remote"],
        "skipped": [],
        "signer": fingerprint(machine.key),
    }
    new = programs("0.13.1")
    for target, content in new.items():
        installed = machine.bin / target
        assert installed.read_bytes() == content
        assert installed.stat().st_mode & 0o777 == 0o755
        assert (machine.bin / f"{target}.prev").read_bytes() == machine.before[target]
    # No staged file is left behind.
    assert sorted(p.name for p in machine.bin.iterdir()) == sorted(
        [*d.UPDATE_TARGETS, *(f"{t}.prev" for t in d.UPDATE_TARGETS)]
    )
    state = json.loads((machine.home / d.UPDATE_STATE_DIR / d.UPDATE_STATE_FILE).read_text())
    assert state["highest"] == "0.13.1"
    assert state["previous"] == d.VERSION
    assert (machine.home / d.UPDATE_STATE_DIR).stat().st_mode & 0o777 == 0o700


def test_files_that_are_not_installed_are_skipped(machine: Machine, quick: None) -> None:
    (machine.bin / "switchboard-hook").unlink()
    out = machine.update(machine.bundle())
    assert out["files"] == ["switchboard-connect", "switchboard-remote"]
    assert out["skipped"] == ["switchboard-hook"]
    assert not (machine.bin / "switchboard-hook").exists()


def test_a_bundle_with_only_the_dispatcher_leaves_the_others(machine: Machine, quick: None) -> None:
    files = {d.UPDATE_PRODUCT: programs("0.13.1")[d.UPDATE_PRODUCT]}
    out = machine.update(machine.bundle(files=files))
    assert out["files"] == ["switchboard-remote"]
    assert (machine.bin / "switchboard-connect").read_bytes() == machine.before["switchboard-connect"]


# --- signature refusals --------------------------------------------------------------------------


def test_a_tampered_manifest_is_refused(machine: Machine, quick: None) -> None:
    files = programs("0.13.1")
    manifest = r.build_manifest(files, created=r.utc_now())
    signature = sign(manifest, machine.key, machine.root / "work")
    forged = manifest.replace(b'"minVersion": "0.13.0"', b'"minVersion": "0.13.1"')
    assert forged != manifest
    assert machine.update(r.bundle_bytes(forged, signature, files)) == {"error": "bad_signature"}
    assert machine.unchanged()


def test_a_tampered_file_is_refused(machine: Machine, quick: None) -> None:
    files = programs("0.13.1")
    manifest = r.build_manifest(files, created=r.utc_now())
    signature = sign(manifest, machine.key, machine.root / "work")
    files["switchboard-hook"] = files["switchboard-hook"] + b"\nimport os; os.system('id')\n"
    assert machine.update(r.bundle_bytes(manifest, signature, files)) == {"error": "file_mismatch"}
    assert machine.unchanged()


def test_a_signature_for_another_namespace_is_refused(machine: Machine, quick: None) -> None:
    assert machine.update(machine.bundle(namespace="git")) == {"error": "wrong_namespace"}
    assert machine.unchanged()


def test_another_namespace_relabelled_still_fails_in_ssh_keygen(machine: Machine, quick: None) -> None:
    """ssh-keygen checks the namespace itself: a blob claiming switchboard-release whose signature
    was made for `git` does not verify (the early check is only for a clear refusal)."""
    files = programs("0.13.1")
    manifest = r.build_manifest(files, created=r.utc_now())
    signature = sign(manifest, machine.key, machine.root / "work", namespace="file")
    assert machine.update(r.bundle_bytes(manifest, signature, files)) == {"error": "wrong_namespace"}
    signers = (machine.home / d.SIGNERS_FILE).read_bytes()
    with pytest.raises(d.RefusedError) as refused:
        d.verify_signature(d.SSH_KEYGEN, signers, manifest, signature)
    assert refused.value.code == "bad_signature"


def test_a_bundle_signed_by_another_key_is_refused(machine: Machine, tmp_path: Path, quick: None) -> None:
    other = new_key(tmp_path, "intruder")
    data = bundle(other, tmp_path / "work")
    assert machine.update(data) == {"error": "bad_signature"}
    assert machine.unchanged()


def test_an_unsigned_or_garbled_signature_is_refused(machine: Machine, quick: None) -> None:
    files = programs("0.13.1")
    manifest = r.build_manifest(files, created=r.utc_now())
    for signature in ("", "not a signature", "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"):
        assert machine.update(r.bundle_bytes(manifest, signature, files)) == {"error": "bad_signature"}
    assert machine.unchanged()


def test_a_key_that_is_not_allowed_for_the_namespace_is_refused(tmp_path: Path, quick: None) -> None:
    key = new_key(tmp_path, "release")
    home = tmp_path / "home"
    line = Path(f"{key}.pub").read_text().split()
    dispatcher = install_machine(home, signers=f'switchboard-release namespaces="git" {line[0]} {line[1]}\n')
    data = bundle(key, tmp_path / "work")
    with pytest.raises(d.RefusedError) as refused:
        d.verb_update(allowed(), {"HOME": str(home)}, data, self_path=str(dispatcher))
    assert refused.value.code == "bad_signature"


# --- versions ------------------------------------------------------------------------------------


def test_the_same_or_an_older_version_is_refused(machine: Machine, quick: None) -> None:
    assert machine.update(machine.bundle(version=d.VERSION)) == {"error": "not_newer"}
    assert machine.update(machine.bundle(version="0.12.9", min_version="0.1.0")) == {"error": "not_newer"}
    assert machine.unchanged()


def test_a_replayed_bundle_and_an_older_signed_one_are_refused_after_an_update(machine: Machine, quick: None) -> None:
    older = machine.bundle(version="0.13.1")
    newest = machine.bundle(version="0.13.2")
    assert machine.update(newest)["to"] == "0.13.2"
    # The running dispatcher is still 0.13.0 here (in process): only the recorded highest version
    # stops the older signed bundle and the replay.
    assert machine.update(older) == {"error": "not_newer"}
    assert machine.update(newest) == {"error": "not_newer"}


def test_rollback_does_not_reopen_the_versions_it_left(machine: Machine, quick: None) -> None:
    data = machine.bundle(version="0.13.1")
    assert machine.update(data)["updated"] is True
    lines: list[str] = []
    assert d.rollback(str(machine.dispatcher), machine.env, lines.append) == 0
    assert machine.dispatcher.read_bytes() == machine.before[d.UPDATE_PRODUCT]
    assert (machine.bin / "switchboard-remote.prev").read_bytes() == programs("0.13.1")[d.UPDATE_PRODUCT]
    assert any("Updates up to 0.13.1 stay refused" in line for line in lines)
    assert machine.update(data) == {"error": "not_newer"}
    # Rolling back again goes forward again.
    assert d.rollback(str(machine.dispatcher), machine.env, lines.append) == 0
    assert machine.dispatcher.read_bytes() == programs("0.13.1")[d.UPDATE_PRODUCT]


def test_rollback_without_prev_files_or_under_sshd_does_nothing(machine: Machine) -> None:
    lines: list[str] = []
    assert d.rollback(str(machine.dispatcher), machine.env, lines.append) == 1
    assert "nothing to roll back" in lines[-1]
    assert d.rollback(str(machine.dispatcher), {**machine.env, "SSH_CONNECTION": "x"}, lines.append) == 1
    assert machine.unchanged()


def test_min_version_is_enforced(machine: Machine, quick: None) -> None:
    assert machine.update(machine.bundle(version="0.14.0", min_version="0.13.5")) == {"error": "min_version"}


def test_a_damaged_update_record_refuses_until_the_owner_removes_it(machine: Machine, quick: None) -> None:
    state = machine.home / d.UPDATE_STATE_DIR
    state.mkdir(parents=True, mode=0o700)
    (state / d.UPDATE_STATE_FILE).write_text("{not json")
    assert machine.update(machine.bundle()) == {"error": "bad_state"}


# --- form and size -------------------------------------------------------------------------------


def _lines(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode().split("\n") if line]


def _join(docs: list[dict[str, object]]) -> bytes:
    return ("\n".join(json.dumps(doc) for doc in docs) + "\n").encode()


def test_unknown_targets_are_refused_in_the_chunks_and_in_the_manifest(machine: Machine, quick: None) -> None:
    docs = _lines(machine.bundle())
    for name in ("../../.bashrc", "switchboard-remote.prev", "/etc/passwd", ".ssh/authorized_keys"):
        renamed = [docs[0], *({**doc, "file": name} if "file" in doc else doc for doc in docs[1:])]
        assert machine.update(_join(renamed)) == {"error": "unknown_target"}
    raw = json.dumps(
        {
            "format": 1,
            "product": "switchboard-remote",
            "version": "0.13.1",
            "minVersion": "0.13.0",
            "created": "2026-11-27T10:00:00Z",
            "files": [{"target": "../evil", "size": 1, "sha256": "0" * 64}],
        }
    ).encode()
    with pytest.raises(d.RefusedError) as refused:
        d.parse_manifest(raw)
    assert refused.value.code == "unknown_target"
    assert machine.unchanged()


def test_cut_reordered_or_padded_bundles_are_refused(machine: Machine, quick: None) -> None:
    files = programs("0.13.1")
    # A hook of three chunks, so chunks can go missing or change places inside one file.
    files["switchboard-hook"] = files["switchboard-hook"] + b"#" * (2 * d.UPDATE_CHUNK_BYTES) + b"\n"
    data = machine.bundle(files=files)
    docs = _lines(data)
    hook = [i for i, doc in enumerate(docs) if doc.get("file") == "switchboard-hook"]
    assert len(hook) == 3
    first, second = hook[0], hook[1]
    swapped = list(docs)
    swapped[first], swapped[second] = docs[second], docs[first]
    cases: dict[str, list[dict[str, object]]] = {
        "no done": docs[:-1],
        "missing chunk": [doc for i, doc in enumerate(docs) if i != second],
        "swapped chunks": swapped,
        "after done": [*docs, {"done": True}],
        "extra key": [{**docs[0], "x": 1}, *docs[1:]],
        "bundle true": [{**docs[0], "bundle": True}, *docs[1:]],
        "file twice": [*docs[:-1], docs[-2], docs[-1]],
    }
    for name, broken in cases.items():
        assert machine.update(_join(broken)) in [{"error": "bad_bundle"}, {"error": "file_mismatch"}], name
    assert machine.update(data[: len(data) // 2]) == {"error": "bad_bundle"}
    assert machine.update(b"") == {"error": "bad_bundle"}
    assert machine.unchanged()


def test_short_chunks_inside_a_file_are_refused() -> None:
    head = {"bundle": 1, "manifest": base64.b64encode(b"{}").decode(), "signature": "x"}
    short = base64.b64encode(b"a" * 10).decode()
    docs: list[dict[str, object]] = [
        head,
        {"file": "switchboard-remote", "chunk": 0, "data": short},
        {"file": "switchboard-remote", "chunk": 1, "data": short},
        {"done": True},
    ]
    with pytest.raises(d.RefusedError) as refused:
        d.read_bundle(_join(docs))
    assert refused.value.code == "bad_bundle"


def test_an_oversized_bundle_is_refused_before_anything_else(machine: Machine) -> None:
    assert machine.update(b"x" * (d.MAX_UPDATE_INPUT_BYTES + 1)) == {"error": "too_large"}
    big = programs("0.13.1")
    big["switchboard-hook"] = b"#" * (d.MAX_UPDATE_TOTAL_BYTES + 1)
    with pytest.raises(r.ReleaseError) as error:
        r.build_manifest(big, created=r.utc_now())
    assert error.value.code == "bad_manifest"


def test_duplicate_json_keys_are_refused() -> None:
    with pytest.raises(d.RefusedError):
        d.parse_manifest(b'{"format": 1, "format": 1}')
    with pytest.raises(d.RefusedError):
        d.read_bundle(b'{"bundle": 1, "bundle": 1}\n{}\n{"done": true}\n')


# --- the signers file, OpenSSH, the policy -------------------------------------------------------


def test_the_signers_file_must_be_private_to_the_user(machine: Machine, quick: None) -> None:
    data = machine.bundle()
    signers = machine.home / d.SIGNERS_FILE
    signers.chmod(0o664)
    assert machine.update(data) == {"error": "signers_unsafe"}
    signers.chmod(0o644)
    signers.parent.chmod(0o770)
    assert machine.update(data) == {"error": "signers_unsafe"}
    signers.parent.chmod(0o700)
    (machine.home / ".config").chmod(0o777)
    assert machine.update(data) == {"error": "signers_unsafe"}
    (machine.home / ".config").chmod(0o700)
    real = signers.with_name("real_signers")
    signers.rename(real)
    signers.symlink_to(real)
    assert machine.update(data) == {"error": "signers_unsafe"}
    signers.unlink()
    assert machine.update(data) == {"error": "no_signers"}
    signers.write_text("# nothing pinned yet\n")
    assert machine.update(data) == {"error": "no_signers"}
    assert machine.unchanged()


def test_an_openssh_older_than_8_1_is_refused_clearly(machine: Machine, tmp_path: Path) -> None:
    tools = tmp_path / "old-openssh"
    tools.mkdir()
    for name, script in (
        ("ssh", "#!/bin/sh\necho 'OpenSSH_7.9p1, OpenSSL 1.1.1' >&2\n"),
        ("ssh-keygen", "#!/bin/sh\necho 'usage: ssh-keygen [-q] [-b bits]' >&2\nexit 1\n"),
    ):
        (tools / name).write_text(script)
        (tools / name).chmod(0o755)
    env = {**machine.env, d.TEST_FLAG: "1", d.TEST_SSH_KEYGEN: str(tools / "ssh-keygen")}
    with pytest.raises(d.RefusedError) as refused:
        d.verb_update(allowed(), env, machine.bundle(), self_path=str(machine.dispatcher))
    assert refused.value.code == "old_openssh"
    (tools / "ssh").unlink()
    with pytest.raises(d.RefusedError) as refused:
        d.verb_update(allowed(), env, machine.bundle(), self_path=str(machine.dispatcher))
    assert refused.value.code == "old_openssh"
    env[d.TEST_SSH_KEYGEN] = str(tools / "missing")
    with pytest.raises(d.RefusedError) as refused:
        d.verb_update(allowed(), env, machine.bundle(), self_path=str(machine.dispatcher))
    assert refused.value.code == "no_openssh"
    # Without the test flag the override is ignored.
    assert d.ssh_keygen_path({d.TEST_SSH_KEYGEN: str(tools / "ssh-keygen")}) == d.SSH_KEYGEN


def test_without_the_policy_flag_update_is_not_allowed(machine: Machine) -> None:
    policy = allowed()
    policy.allow_self_update = False
    assert machine.update(machine.bundle(), policy) == {"error": "not_allowed"}
    assert machine.unchanged()


def test_the_flag_is_refused_from_the_hub_and_unknown_in_a_policy_file(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    hub = ["--policy-from-hub", "--sessions", "x", "--allow-self-update", "--", "version"]
    assert d.handle(hub, env)["error"] == "bad_policy"
    policy_file = tmp_path / d.POLICY_FILE
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(json.dumps({"sessions": ["x"], "allowSelfUpdate": True}))
    assert d.handle(["switchboard-remote", "version"], env)["error"] == "no_policy"


def test_version_reports_the_flag_and_the_verb(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "SSH_ORIGINAL_COMMAND": "switchboard-remote version"}
    on = d.handle(["--sessions", "x", "--allow-self-update"], env)
    off = d.handle(["--sessions", "x"], env)
    assert (on["allowSelfUpdate"], off["allowSelfUpdate"]) == (True, False)
    verbs = on["verbs"]
    assert isinstance(verbs, list)
    assert "update" in verbs
    assert d.handle(["--sessions", "x"], {**env, "SSH_ORIGINAL_COMMAND": "switchboard-remote update x"}) == {
        "protocol": 1,
        "ok": False,
        "error": "bad_request",
    }


# --- the install itself --------------------------------------------------------------------------


def test_a_dispatcher_not_installed_under_its_name_is_refused(machine: Machine, quick: None) -> None:
    other = machine.bin / "dispatcher.py"
    other.write_bytes(machine.dispatcher.read_bytes())
    with pytest.raises(d.RefusedError) as refused:
        d.verb_update(allowed(), machine.env, machine.bundle(), self_path=str(other))
    assert refused.value.code == "not_installed"


def test_unsafe_install_directories_and_files_are_refused(machine: Machine, quick: None) -> None:
    data = machine.bundle()
    machine.bin.chmod(0o775)
    assert machine.update(data) == {"error": "install_unsafe"}
    machine.bin.chmod(0o755)
    (machine.bin / "switchboard-hook").chmod(0o775)
    assert machine.update(data) == {"error": "install_unsafe"}
    (machine.bin / "switchboard-hook").chmod(0o755)
    hook = machine.bin / "switchboard-hook"
    hook.unlink()
    hook.symlink_to(machine.root / "elsewhere")
    assert machine.update(data) == {"error": "install_unsafe"}


def test_a_new_dispatcher_that_does_not_start_is_never_installed(machine: Machine) -> None:
    source = r.source_files()[d.UPDATE_PRODUCT].replace(
        b'if __name__ == "__main__":', b'raise SystemExit(3)\nif __name__ == "__main__":'
    )
    broken = programs("0.13.1", dispatcher=source)
    assert machine.update(machine.bundle(files=broken)) == {"error": "self_test_failed"}
    syntax = programs("0.13.1")
    syntax["switchboard-hook"] = syntax["switchboard-hook"] + b"\ndef (:\n"
    assert machine.update(machine.bundle(files=syntax)) == {"error": "self_test_failed"}
    assert machine.unchanged()


def test_a_failed_rename_puts_back_what_was_already_replaced(
    machine: Machine, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.rename

    def failing(source: str, target: str) -> None:
        if target.endswith("/switchboard-remote"):
            raise OSError(28, "No space left on device")
        real(source, target)

    monkeypatch.setattr(os, "rename", failing)
    assert machine.update(machine.bundle()) == {"error": "install_failed"}
    monkeypatch.setattr(os, "rename", real)
    for target in d.UPDATE_TARGETS:
        assert (machine.bin / target).read_bytes() == machine.before[target]
    assert not [p for p in machine.bin.iterdir() if ".update-" in p.name]


def test_a_second_update_at_the_same_time_is_locked(machine: Machine, quick: None) -> None:
    with d.update_lock(machine.env):
        assert machine.update(machine.bundle()) == {"error": "locked"}


# --- as a forced command ---------------------------------------------------------------------------


def test_update_over_the_forced_command_runs_the_installed_copy(machine: Machine) -> None:
    data = machine.bundle()
    env = {
        "HOME": str(machine.home),
        "PATH": "/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": "switchboard-remote update",
        "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22",
    }
    argv = [sys.executable, "-I", str(machine.dispatcher), "--sessions", "fpga", "--allow-self-update"]
    done = subprocess.run(argv, input=data, env=env, capture_output=True, check=False, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["to"] == "0.13.1"
    version = subprocess.run(
        [sys.executable, "-I", str(machine.dispatcher), "--sessions", "fpga"],
        env={**env, "SSH_ORIGINAL_COMMAND": "switchboard-remote version"},
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert json.loads(version.stdout)["version"] == "0.13.1"
    rolled = subprocess.run(
        [sys.executable, "-I", str(machine.dispatcher), "--rollback"],
        env={"HOME": str(machine.home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert rolled.returncode == 0, rolled.stdout
    assert "switchboard-remote: restored 0.13.0" in rolled.stdout


# --- serve mode (the agent transport) --------------------------------------------------------------


def test_serve_mode_answers_then_exits_77_so_the_connector_restarts(machine: Machine) -> None:
    data = machine.bundle()
    argv = [sys.executable, "-I", str(machine.dispatcher), "--serve", "--sessions", "fpga", "--allow-self-update"]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={"HOME": str(machine.home), "PATH": "/usr/bin:/bin"},
    )
    assert process.stdin is not None
    assert process.stdout is not None
    hello = json.loads(process.stdout.readline())
    assert hello["version"] == d.VERSION
    request = {"t": "req", "id": 1, "verb": "update", "args": [], "input": data.decode("ascii")}
    line = json.dumps(request).encode() + b"\n"
    assert len(line) <= d.MAX_HUB_FRAME_BYTES
    process.stdin.write(line)
    process.stdin.flush()
    answer = json.loads(process.stdout.readline())
    assert answer["t"] == "res"
    assert answer["doc"]["updated"] is True
    assert process.wait(30) == d.EXIT_UPDATED
    process.stdin.close()
    process.stdout.close()


def test_serve_mode_keeps_running_after_a_refused_update(machine: Machine) -> None:
    argv = [sys.executable, "-I", str(machine.dispatcher), "--serve", "--sessions", "fpga"]
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={"HOME": str(machine.home), "PATH": "/usr/bin:/bin"}
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdout.readline()
    request = {"t": "req", "id": 1, "verb": "update", "args": [], "input": "x"}
    process.stdin.write(json.dumps(request).encode() + b"\n")
    process.stdin.flush()
    assert json.loads(process.stdout.readline())["doc"] == {"protocol": 1, "ok": False, "error": "not_allowed"}
    process.stdin.write(b'{"t":"ping","id":2}\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline()) == {"t": "pong", "id": 2}
    process.stdin.close()
    assert process.wait(30) == 0
    process.stdout.close()
