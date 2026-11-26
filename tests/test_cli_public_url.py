"""`voice-coding-mcp set-public-url`, which deploy/linux/sb-linux runs as the hub user to point
publicUrl at the Funnel address without editing the file itself."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

from tests.test_cli import PUBLIC_URL, Io, run_cli, write_config
from voice_coding_mcp.cli import EXIT_OK, EXIT_USAGE, parse_args
from voice_coding_mcp.config import load_config

FUNNEL_URL = "https://example-host.example-tailnet.ts.net"


def cli(*argv: str) -> tuple[int, Io]:
    return asyncio.run(run_cli(*argv))


def test_parses_the_url_argument() -> None:
    args = parse_args(["set-public-url", FUNNEL_URL, "--config", "/x.json"])
    assert (args.command, args.url, args.config) == ("set-public-url", FUNNEL_URL, "/x.json")


def test_rewrites_only_public_url_keeps_the_other_keys_in_order_and_mode_0600(real_tmp: Path) -> None:
    path = write_config(real_tmp, logPrompts=False, notify={"url": None})
    os.chmod(path, 0o640)
    before = json.loads(Path(path).read_text(encoding="utf-8"))
    code, io = cli("set-public-url", FUNNEL_URL + "/", "--config", path)
    assert code == EXIT_OK, io.stderr
    assert io.stdout == f"changed: publicUrl is now {FUNNEL_URL} (restart the server to apply it)\n"
    after = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(after) == list(before)
    assert after == {**before, "publicUrl": FUNNEL_URL}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_config(path).public_url == FUNNEL_URL
    assert [p.name for p in real_tmp.iterdir() if p.name.endswith(".tmp")] == []


def test_says_unchanged_and_leaves_the_file_alone(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    text = Path(path).read_text(encoding="utf-8")
    inode = os.stat(path).st_ino
    code, io = cli("--config", path, "set-public-url", PUBLIC_URL + "/")
    assert code == EXIT_OK
    assert io.stdout == f"unchanged: publicUrl is already {PUBLIC_URL}\n"
    assert Path(path).read_text(encoding="utf-8") == text
    assert os.stat(path).st_ino == inode


def test_refuses_a_url_the_config_would_reject_and_keeps_the_file(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    text = Path(path).read_text(encoding="utf-8")
    for bad in ("http://example-host.ts.net", "https://example-host.ts.net/mcp", "not a url"):
        code, io = cli("set-public-url", bad, "--config", path)
        assert code == EXIT_USAGE, bad
        assert "publicUrl" in io.stderr
        assert io.stdout == ""
    assert Path(path).read_text(encoding="utf-8") == text


def test_writes_through_a_symlinked_config(real_tmp: Path) -> None:
    target = write_config(real_tmp)
    link = real_tmp / "link.json"
    link.symlink_to(target)
    code, _io = cli("set-public-url", FUNNEL_URL, "--config", str(link))
    assert code == EXIT_OK
    assert link.is_symlink()
    assert load_config(target).public_url == FUNNEL_URL


def test_an_invalid_config_is_a_usage_error_before_anything_is_written(real_tmp: Path) -> None:
    path = real_tmp / "config.json"
    path.write_text('{"publicUrl": "https://a.example.com"}', encoding="utf-8")
    code, io = cli("set-public-url", FUNNEL_URL, "--config", str(path))
    assert code == EXIT_USAGE
    assert io.stdout == ""
    assert json.loads(path.read_text(encoding="utf-8")) == {"publicUrl": "https://a.example.com"}
