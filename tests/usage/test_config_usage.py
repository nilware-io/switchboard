"""The `usage` config section."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voice_coding_mcp.config import ConfigError, parse_config

REPO = Path(__file__).resolve().parents[2]


ROOT: list[str] = []


@pytest.fixture(autouse=True)
def project_root(tmp_path: Path) -> None:
    (tmp_path / "projects").mkdir()
    ROOT[:] = [str(tmp_path / "projects"), str(tmp_path / "state")]


def parse(usage: Any) -> Any:
    return parse_config(
        {"publicUrl": "https://vcm.example.com", "projectRoots": ROOT[:1], "stateDir": ROOT[1], "usage": usage},
        home_dir="/nonexistent-home-dir",
    ).usage


def test_defaults() -> None:
    usage = parse({})
    assert (
        usage.idle_gap_minutes,
        usage.hourly_rate,
        usage.currency,
        usage.overlap,
        usage.max_transcript_megabytes,
        usage.prices,
    ) == (
        15,
        40.0,
        "EUR",
        "union",
        512,
        {},
    )


def test_prices_and_rate() -> None:
    usage = parse({"hourlyRate": 42.5, "prices": {"claude-opus-4-1": {"input": 15, "output": 75, "cacheWrite1h": 30}}})
    assert usage.hourly_rate == 42.5
    price = usage.prices["claude-opus-4-1"]
    assert (price.input, price.output, price.cache_write5m, price.cache_write1h, price.cache_read) == (
        15,
        75,
        None,
        30,
        None,
    )


@pytest.mark.parametrize(
    ("usage", "message"),
    [
        ({"idleGapMinutes": 0}, "usage.idleGapMinutes"),
        ({"idleGapMinutes": 241}, "usage.idleGapMinutes"),
        ({"hourlyRate": -1}, "usage.hourlyRate"),
        ({"hourlyRate": True}, "usage.hourlyRate"),
        ({"hourlyRate": "40"}, "usage.hourlyRate"),
        ({"currency": "eur"}, "usage.currency"),
        ({"currency": "EURO"}, "usage.currency"),
        ({"overlap": "max"}, "usage.overlap"),
        ({"maxTranscriptMegabytes": 4096}, "usage.maxTranscriptMegabytes"),
        ({"prices": {"claude opus": {"input": 1, "output": 1}}}, "usage.prices"),
        ({"prices": {"claude-opus": {"input": 1}}}, "usage.prices"),
        ({"prices": {"claude-opus": {"input": -1, "output": 1}}}, "usage.prices"),
        ({"prices": {"claude-opus": {"input": 1, "output": 1, "cache_read_extra": 1}}}, "usage.prices"),
        ({"prices": {"claude-opus": {"input": 20000, "output": 1}}}, "usage.prices"),
        ({"rate": 40}, "usage.rate"),
    ],
)
def test_rejections(usage: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError) as err:
        parse(usage)
    assert message in str(err.value)


def test_example_config_has_the_usage_defaults() -> None:
    data = json.loads((REPO / "config.example.json").read_text(encoding="utf-8"))
    assert data["usage"] == {
        "idleGapMinutes": 15,
        "hourlyRate": 40,
        "currency": "EUR",
        "overlap": "union",
        "maxTranscriptMegabytes": 512,
        "prices": {},
    }
