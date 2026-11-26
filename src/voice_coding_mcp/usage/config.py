"""The `usage` config section (docs/SWITCHBOARD.md, "Config", `usage`)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel

CURRENCY_RE = re.compile(r"[A-Z]{3}")
PRICE_PREFIX_RE = re.compile(r"[A-Za-z0-9._:@\[\]-]{1,64}")
MAX_PRICE_ENTRIES = 64
MAX_PRICE = 10_000.0
"""USD per million tokens; far above any published price, so a typo in thousands is caught."""


def _integral(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Input should be a valid integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _number(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Input should be a valid number")
    if isinstance(value, int):
        return float(value)
    return value


Int = Annotated[int, BeforeValidator(_integral)]
Price = Annotated[float, BeforeValidator(_number), Field(ge=0, le=MAX_PRICE, allow_inf_nan=False)]


def _currency(value: str) -> str:
    if not CURRENCY_RE.fullmatch(value):
        raise ValueError("must be a three-letter currency code in capitals, such as EUR")
    return value


def _prices(value: dict[str, PriceConfig]) -> dict[str, PriceConfig]:
    for prefix in value:
        if not PRICE_PREFIX_RE.fullmatch(prefix):
            raise ValueError(f"model prefix {prefix!r} must be 1 to 64 letters, digits or ._:@[]-")
    return value


class _Section(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )


class PriceConfig(_Section):
    """USD per million tokens. Cache prices left out follow the API's multipliers of `input`
    (5-minute write 1.25x, 1-hour write 2x, read 0.1x)."""

    input: Price
    output: Price
    cache_write5m: Price | None = Field(default=None, alias="cacheWrite5m")
    cache_write1h: Price | None = Field(default=None, alias="cacheWrite1h")
    cache_read: Price | None = None


class UsageConfig(_Section):
    idle_gap_minutes: Annotated[Int, Field(ge=1, le=240)] = 15
    hourly_rate: Annotated[Price, Field(le=100_000)] = 40.0
    currency: Annotated[str, AfterValidator(_currency)] = "EUR"
    overlap: Literal["union", "sum"] = "union"
    max_transcript_megabytes: Annotated[Int, Field(ge=1, le=2048)] = 512
    prices: Annotated[dict[str, PriceConfig], Field(max_length=MAX_PRICE_ENTRIES), AfterValidator(_prices)] = Field(
        default_factory=dict
    )
