"""Sideways (chop) filter settings and check, shared by every strategy (rules in core/regime.py).

A strategy's settings inherit `RegimeSettings`; before an entry the strategy calls `regime_check`, which runs
`check_regime` on the last 300 completed candles of the index and returns what is saved with the signal
(reasons, metrics, would_block). "log_only" (the default) only records it; "block" skips the entry.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from brokers.base import Candle, Instrument
from core.regime import RegimeConfig, check_regime, summarize
from strategies.base import StrategySettings

REGIME_CANDLES = 300  # completed candles the check looks at (enough for its EMA / ATR to settle)

REGIME_FIELDS = ("regime_filter", "regime_min_fails", "regime_min_slope_atr", "regime_min_adx", "regime_max_chop",
                 "regime_max_crosses", "regime_min_range_atr", "regime_block_against_slope")


class RegimeSettings(StrategySettings):
    FORM_LAST: ClassVar[tuple[str, ...]] = REGIME_FIELDS

    regime_filter: Literal["off", "log_only", "block"] = Field(
        "log_only", title="Sideways filter",
        description="log_only: record whether the market looked sideways next to every signal and trade, never "
                    "block · block: skip the entry when it does · off: not computed")
    regime_min_fails: int = Field(2, ge=1, le=5, title="Sideways: block when at least N checks fail",
                                  description="Checks: flat EMA, weak ADX, high Choppiness, many EMA crosses, tight box")
    regime_min_slope_atr: float = Field(0.3, ge=0, le=10, title="Sideways: min EMA slope (× ATR over 5 candles)")
    regime_min_adx: float = Field(20, ge=0, le=100, title="Sideways: min ADX(14)")
    regime_max_chop: float = Field(61.8, gt=0, le=100, title="Sideways: max Choppiness Index(14)")
    regime_max_crosses: int = Field(3, ge=0, le=20, title="Sideways: max EMA crosses in the last 20 candles")
    regime_min_range_atr: float = Field(2.5, ge=0, le=50, title="Sideways: min 1-hour box height (× ATR)")
    regime_block_against_slope: bool = Field(
        False, title="Sideways: also block trades against the EMA slope",
        description="CE only when the EMA slopes up, PE only when it slopes down (recorded either way)")

    def regime_config(self) -> RegimeConfig:
        return RegimeConfig(ema_len=getattr(self, "ema_length", 21), min_fails=self.regime_min_fails,
                            min_slope_atr=self.regime_min_slope_atr, min_adx=self.regime_min_adx,
                            max_chop=self.regime_max_chop, max_crosses=self.regime_max_crosses,
                            min_range_atr=self.regime_min_range_atr)


def regime_of(settings: RegimeSettings, candles: list[Candle], direction: str) -> dict[str, Any] | None:
    """The filter on these completed candles (oldest first); None when it is off."""
    if settings.regime_filter == "off":
        return None
    r = check_regime(candles[-REGIME_CANDLES:], settings.regime_config())
    return summarize(r, direction, settings.regime_block_against_slope, settings.regime_filter)  # type: ignore[arg-type]


async def regime_check(ctx: Any, settings: RegimeSettings, spot: Instrument, timeframe_min: int,
                       direction: str) -> dict[str, Any] | None:
    """The filter on the index's latest completed candles, from the engine's shared buffer when there is one."""
    if settings.regime_filter == "off":
        return None
    fn = getattr(ctx, "recent_candles", None)
    candles = await fn(spot, timeframe_min, REGIME_CANDLES) if fn else await ctx.history(spot, timeframe_min, REGIME_CANDLES)
    return regime_of(settings, list(candles), direction)


def blocks(settings: RegimeSettings, regime: dict[str, Any] | None) -> bool:
    return bool(regime and regime["would_block"] and settings.regime_filter == "block")
