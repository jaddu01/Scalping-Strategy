"""TP1 / TP2 variants of both breakout strategies.

Exact same entry, stop-loss and timed-exit rules as the originals; the only difference is the exit:
  TP1: sell tp1_lots when spot moves tp1 in the trade's favour
  TP2: sell tp2_lots when spot moves tp2 in the trade's favour
  the remaining lots exit on the spot SL or at the slot's timed exit.
TP distances are measured from the spot at entry, either in NIFTY points or as a multiple of the
reference candle's range (high − low). Defaults (TP1 = 1.0× range, TP2 = 1.25× range) come from a
45-day study of NIFTY 1-minute data (Sep–Nov 2026): tighter fixed targets (+10/+20, +15/+25) cut the
average profit by 25–50%; these kept ~95% of it with a smaller drawdown.
"""
from __future__ import annotations

from datetime import time
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from brokers.base import Candle
from strategies.base import Direction, PartialTarget, StrategySettings, TakeProfitMode
from strategies.nifty_early_breakout import EARLY_REF_STARTS
from strategies.nifty_ref_breakout import NiftyRefBreakout, NiftyRefBreakoutSettings
from strategies.registry import register


class NiftyRefBreakoutTPSettings(NiftyRefBreakoutSettings):
    tp_enabled: bool = Field(
        True, title="Use TP1 / TP2",
        description="On: sell TP1 lots and TP2 lots at the targets, the rest at the spot SL or timed exit. "
                    "Off: the whole position exits on the spot SL or timed exit, exactly like the original "
                    "strategy. A change applies from the next trade, never to an open one")
    tp_basis: Literal["range_multiple", "points"] = Field(
        "range_multiple", title="TP1/TP2 measured in",
        description="range_multiple: × the reference candle's range (high − low) · points: NIFTY spot points. "
                    "Both are measured from the spot at entry")
    tp1_value: float = Field(1.0, gt=0, le=1000, title="TP1 distance",
                             description="e.g. 1.0 = one candle range (range_multiple) or 20 = 20 points (points)")
    tp1_lots: int = Field(2, ge=1, le=200, title="TP1 lots", description="Lots sold at TP1")
    tp2_value: float = Field(1.25, gt=0, le=1000, title="TP2 distance", description="Must be beyond TP1")
    tp2_lots: int = Field(2, ge=1, le=200, title="TP2 lots", description="Lots sold at TP2")

    @model_validator(mode="after")
    def _tp_checks(self) -> "NiftyRefBreakoutTPSettings":
        if not self.tp_enabled:
            return self  # targets are ignored while switched off
        if self.take_profit_mode is not TakeProfitMode.NONE:
            raise ValueError("set Take-profit mode to none: this strategy exits with TP1, TP2 and the timed exit")
        if self.tp2_value <= self.tp1_value:
            raise ValueError("TP2 distance must be greater than TP1 distance")
        if self.tp1_lots + self.tp2_lots >= self.lots:
            raise ValueError(f"TP1 lots + TP2 lots ({self.tp1_lots + self.tp2_lots}) must be less than Lots "
                             f"({self.lots}) so at least one lot runs to the timed exit")
        return self


class NiftyEarlyBreakoutTPSettings(NiftyRefBreakoutTPSettings):
    reference_candle_starts: list[time] = Field(
        default_factory=lambda: [time.fromisoformat(t) for t in EARLY_REF_STARTS],
        title="Reference candle start times (IST)",
        description="Fixed schedule of reference candles, comma separated HH:MM",
    )
    lots: int = Field(10, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")
    tp1_lots: int = Field(4, ge=1, le=200, title="TP1 lots", description="Lots sold at TP1")
    tp2_lots: int = Field(4, ge=1, le=200, title="TP2 lots", description="Lots sold at TP2")


def tp_levels(settings: NiftyRefBreakoutTPSettings, direction: Direction, entry_spot: float,
              ref: Candle) -> list[PartialTarget]:
    sign = 1 if direction is Direction.CE_BUY else -1
    side = "above" if direction is Direction.CE_BUY else "below"
    unit = (ref.high - ref.low) if settings.tp_basis == "range_multiple" else 1.0
    return [PartialTarget("tp1", round(entry_spot + sign * settings.tp1_value * unit, 2), side, settings.tp1_lots),
            PartialTarget("tp2", round(entry_spot + sign * settings.tp2_value * unit, 2), side, settings.tp2_lots)]


@register
class NiftyRefBreakoutTP(NiftyRefBreakout):
    template_id: ClassVar[str] = "nifty_ref_breakout_tp"
    display_name: ClassVar[str] = "Index 5-min Reference Candle Breakout + TP1/TP2"
    description: ClassVar[str] = ("Original breakout schedule and rules (5 lots); sells 2 lots at TP1 and 2 at TP2 "
                                  "(default 1.0× and 1.25× the reference candle range), last lot exits on the "
                                  "spot SL or the timed exit.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyRefBreakoutTPSettings

    def partial_targets(self, direction: Direction, entry_spot: float, ref: Candle) -> list[PartialTarget]:
        assert isinstance(self.settings, NiftyRefBreakoutTPSettings)
        if not self.settings.tp_enabled:
            return []
        return tp_levels(self.settings, direction, entry_spot, ref)


@register
class NiftyEarlyBreakoutTP(NiftyRefBreakoutTP):
    template_id: ClassVar[str] = "nifty_early_breakout_tp"
    display_name: ClassVar[str] = "Index 5-min Early-Slot Breakout + TP1/TP2"
    description: ClassVar[str] = ("Early-slot schedule (09:30–14:55) and rules (10 lots); sells 4 lots at TP1 and 4 "
                                  "at TP2 (default 1.0× and 1.25× the reference candle range), last 2 lots exit on "
                                  "the spot SL or the timed exit.")
    settings_model: ClassVar[type[StrategySettings]] = NiftyEarlyBreakoutTPSettings
