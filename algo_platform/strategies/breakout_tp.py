"""TP1 / TP2 variants of both breakout strategies.

Exact same entry, spot stop-loss and timed-exit rules as the originals; the only difference is the exit:
  TP1: sell TP1 lots when the option's price is up tp1 % from the entry fill (default +20 %)
  TP2: sell TP2 lots when it is up tp2 % (default +30 %)
  the remaining lots exit on the spot SL or at the slot's timed exit.
Lots per target: 0 = automatic, 40 % / 40 % / rest (5 lots -> 2/2/1, 10 lots -> 4/4/2).
TP can also be measured on NIFTY spot instead: in points, or as a multiple of the reference candle's
range (a 45-day study of Sep–Nov 2026 data found 1.0× / 1.25× range the best of the spot options).
"""
from __future__ import annotations

from datetime import time
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from brokers.base import Candle
from strategies.base import Direction, PartialTarget, StrategySettings, TakeProfitMode
from strategies.nifty_early_breakout import EARLY_REF_STARTS
from strategies.nifty_ref_breakout import BreakoutBaseSettings, NiftyRefBreakout, NiftyRefBreakoutSettings
from strategies.registry import register


class TPSettings(BreakoutBaseSettings):
    """TP1 / TP2 fields and checks; combined with any breakout schedule (see the classes below)."""

    tp_enabled: bool = Field(
        True, title="Use TP1 / TP2",
        description="On: sell TP1 lots and TP2 lots at the targets, the rest at the spot SL or timed exit. "
                    "Off: the whole position exits on the spot SL or timed exit, exactly like the original "
                    "strategy. A change applies from the next trade, never to an open one")
    tp_basis: Literal["premium_pct", "range_multiple", "points"] = Field(
        "premium_pct", title="TP1/TP2 measured in",
        description="premium_pct: % gain on the option's entry fill price · range_multiple: × the reference "
                    "candle's range (high − low) on NIFTY spot · points: NIFTY spot points from the entry spot")
    tp1_value: float = Field(20.0, gt=0, le=1000, title="TP1 distance",
                             description="e.g. 20 = +20 % premium (premium_pct), 1.0 = one candle range "
                                         "(range_multiple) or 20 = 20 points (points)")
    tp1_lots: int = Field(0, ge=0, le=200, title="TP1 lots",
                          description="Lots sold at TP1; 0 = automatic (40 % of Lots: 5 -> 2, 10 -> 4)")
    tp2_value: float = Field(30.0, gt=0, le=1000, title="TP2 distance", description="Must be beyond TP1")
    tp2_lots: int = Field(0, ge=0, le=200, title="TP2 lots",
                          description="Lots sold at TP2; 0 = automatic (40 % of Lots: 5 -> 2, 10 -> 4)")

    @model_validator(mode="after")
    def _tp_checks(self) -> "TPSettings":
        if not self.tp_enabled:
            return self  # targets are ignored while switched off
        if self.take_profit_mode is not TakeProfitMode.NONE:
            raise ValueError("set Take-profit mode to none: this strategy exits with TP1, TP2 and the timed exit")
        if self.tp2_value <= self.tp1_value:
            raise ValueError("TP2 distance must be greater than TP1 distance")
        if (self.tp1_lots == 0) != (self.tp2_lots == 0):
            raise ValueError("set both TP1 lots and TP2 lots to 0 (automatic split) or both to a number of lots")
        tp1, tp2 = self.split()
        if tp1 + tp2 >= self.lots:
            raise ValueError(f"TP1 lots + TP2 lots ({tp1 + tp2}) must be less than Lots "
                             f"({self.lots}) so at least one lot runs to the timed exit")
        return self

    def split(self) -> tuple[int, int]:
        """(TP1 lots, TP2 lots); automatic = 40 % each, rounded down (5 -> 2/2/1, 10 -> 4/4/2)."""
        if self.tp1_lots == 0 and self.tp2_lots == 0:
            n = self.lots * 2 // 5
            return n, n
        return self.tp1_lots, self.tp2_lots


class NiftyRefBreakoutTPSettings(TPSettings, NiftyRefBreakoutSettings):
    """Original schedule + TP1/TP2."""


class NiftyEarlyBreakoutTPSettings(NiftyRefBreakoutTPSettings):
    reference_candle_starts: list[time] = Field(
        default_factory=lambda: [time.fromisoformat(t) for t in EARLY_REF_STARTS],
        title="Reference candle start times (IST)",
        description="Fixed schedule of reference candles, comma separated HH:MM",
    )
    lots: int = Field(10, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")


def tp_levels(settings: TPSettings, direction: Direction, entry_spot: float,
              ref: Candle) -> list[PartialTarget]:
    lots1, lots2 = settings.split()
    out = []
    for label, value, lots in (("tp1", settings.tp1_value, lots1), ("tp2", settings.tp2_value, lots2)):
        if lots <= 0:
            continue  # too few lots to scale out (e.g. 1-2 lots on the automatic split)
        if settings.tp_basis == "premium_pct":
            out.append(PartialTarget(label, None, "above", lots, premium_pct=value))
            continue
        sign = 1 if direction is Direction.CE_BUY else -1
        side = "above" if direction is Direction.CE_BUY else "below"
        unit = (ref.high - ref.low) if settings.tp_basis == "range_multiple" else 1.0
        out.append(PartialTarget(label, round(entry_spot + sign * value * unit, 2), side, lots))
    return out


@register
class NiftyRefBreakoutTP(NiftyRefBreakout):
    template_id: ClassVar[str] = "nifty_ref_breakout_tp"
    display_name: ClassVar[str] = "Index 5-min Reference Candle Breakout + TP1/TP2"
    description: ClassVar[str] = ("Original breakout schedule and rules (5 lots); sells 2 lots at TP1 (+20 % premium) "
                                  "and 2 at TP2 (+30 %); the last lot exits on the spot SL or the timed exit.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyRefBreakoutTPSettings

    def partial_targets(self, direction: Direction, entry_spot: float, ref: Candle) -> list[PartialTarget]:
        assert isinstance(self.settings, TPSettings)
        if not self.settings.tp_enabled:
            return []
        return tp_levels(self.settings, direction, entry_spot, ref)


@register
class NiftyEarlyBreakoutTP(NiftyRefBreakoutTP):
    template_id: ClassVar[str] = "nifty_early_breakout_tp"
    display_name: ClassVar[str] = "Index 5-min Early-Slot Breakout + TP1/TP2"
    description: ClassVar[str] = ("Early-slot schedule (09:30–14:55) and rules (10 lots); sells 4 lots at TP1 (+20 % "
                                  "premium) and 4 at TP2 (+30 %); the last 2 lots exit on the spot SL or the timed exit.")
    settings_model: ClassVar[type[StrategySettings]] = NiftyEarlyBreakoutTPSettings
