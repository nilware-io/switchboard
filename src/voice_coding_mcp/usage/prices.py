"""API-equivalent token prices (docs/SWITCHBOARD.md, "usage.prices").

The built-in table holds Anthropic's published first-party API prices in USD per million tokens,
as listed on 2026-11-03 (the cache writes are 1.25x and 2x of input, as published; the cache read
prices are the published ones, which are not 0.1x of input on every model). Claude Haiku 5.5 is
listed at its price for prompts up to 100K tokens. Models not in the table (older ones included)
are "unpriced" unless `usage.prices` names them: a price is never guessed.

`usage.prices` keys are model prefixes; the longest configured prefix that matches wins. A built-in
entry matches its model id exactly or with a date suffix (`claude-haiku-4-5-20251001`), so a future
`claude-opus-5-7` is not priced as `claude-opus-5`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .config import PriceConfig

PRICES_AS_OF = "2026-11-03"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


def _price(input_: float, output: float, cache_read: float) -> Price:
    return Price(input_, output, input_ * 1.25, input_ * 2, cache_read)


BUILT_IN: dict[str, Price] = {
    "claude-fable-5-2": _price(8, 40, 0.20),
    "claude-mythos-5-2": _price(8, 40, 0.20),
    "claude-fable-5-1": _price(10, 50, 0.25),
    "claude-mythos-5-1": _price(10, 50, 0.25),
    "claude-fable-5": _price(10, 50, 1.00),
    "claude-mythos-5": _price(10, 50, 1.00),
    "claude-opus-5-5": _price(4, 20, 0.20),
    "claude-opus-5": _price(5, 25, 0.50),
    "claude-opus-4-8": _price(5, 25, 0.50),
    "claude-opus-4-7": _price(5, 25, 0.50),
    "claude-opus-4-6": _price(5, 25, 0.50),
    "claude-sonnet-5-5": _price(2, 10, 0.10),
    "claude-sonnet-5": _price(2, 10, 0.20),
    "claude-sonnet-4-6": _price(3, 15, 0.30),
    "claude-haiku-5-5": _price(0.10, 0.50, 0.01),
    "claude-haiku-4-5": _price(1, 5, 0.10),
}

_SUFFIX_RE = re.compile(r"(?:-[0-9]{8})?(?:\[1m\])?")
"""What may follow a built-in model id: a snapshot date, Claude Code's `[1m]` context marker."""


def from_config(entry: PriceConfig) -> Price:
    return Price(
        input=entry.input,
        output=entry.output,
        cache_write_5m=entry.cache_write5m if entry.cache_write5m is not None else entry.input * 1.25,
        cache_write_1h=entry.cache_write1h if entry.cache_write1h is not None else entry.input * 2,
        cache_read=entry.cache_read if entry.cache_read is not None else entry.input * 0.1,
    )


class PriceTable:
    def __init__(self, configured: Mapping[str, PriceConfig] | None = None) -> None:
        self._configured = sorted(
            ((prefix, from_config(entry)) for prefix, entry in (configured or {}).items()),
            key=lambda item: -len(item[0]),
        )

    def price(self, model: str | None) -> Price | None:
        if not model:
            return None
        for prefix, price in self._configured:
            if model.startswith(prefix):
                return price
        for model_id, price in BUILT_IN.items():
            if model.startswith(model_id) and _SUFFIX_RE.fullmatch(model[len(model_id) :]):
                return price
        return None
