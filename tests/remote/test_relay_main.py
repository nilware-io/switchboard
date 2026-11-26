"""The relay never prints a traceback (stderr goes back to the machine)."""

import pytest

from voice_coding_mcp.remote import relay


def test_main_prints_a_fixed_line_on_an_unexpected_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_args: object, **_kw: object) -> int:
        raise ValueError("/opt/secret/path")

    monkeypatch.setattr(relay, "run", boom)
    assert relay.main() == 1
    err = capsys.readouterr().err
    assert err == "switchboard relay: internal error\n"
