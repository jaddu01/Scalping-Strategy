"""Index 5-minute Early-Slot Breakout (ATM option buying) — "strategy part 1".

Same rules as the reference-candle breakout (strategies/nifty_ref_breakout.py), on its own schedule:
  reference candles 09:30, 09:45, 10:00, 10:30, 11:00, 11:30, 11:55, 12:30, 13:00, 13:30, 13:55, 14:20, 14:55
  spot > ref_high + 2.25  ->  BUY ATM CE   (spot SL: exit if spot <= ref_low)
  spot < ref_low  - 2.25  ->  BUY ATM PE   (spot SL: exit if spot >= ref_high)
  entry window = the two 5-min candles after the reference candle; hard exit = reference start + 15 min
  (e.g. 09:30 -> 09:45, 14:55 -> 15:10); 10 lots.

It is a separate template, so it runs as its own instance next to the original strategy. Each
strategy trades independently: on slots both share (09:45, 10:00, 14:55 by default) both take the
trade, so the position is doubled (e.g. 5 + 10 lots).
"""
from __future__ import annotations

from datetime import time
from typing import ClassVar

from pydantic import Field

from strategies.base import StrategySettings
from strategies.nifty_ref_breakout import NiftyRefBreakout, NiftyRefBreakoutSettings
from strategies.registry import register

EARLY_REF_STARTS = ["09:30", "09:45", "10:00", "10:30", "11:00", "11:30", "11:55", "12:30",
                    "13:00", "13:30", "13:55", "14:20", "14:55"]


class NiftyEarlyBreakoutSettings(NiftyRefBreakoutSettings):
    reference_candle_starts: list[time] = Field(
        default_factory=lambda: [time.fromisoformat(t) for t in EARLY_REF_STARTS],
        title="Reference candle start times (IST)",
        description="Fixed schedule of reference candles, comma separated HH:MM",
    )
    lots: int = Field(10, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")


@register
class NiftyEarlyBreakout(NiftyRefBreakout):
    template_id: ClassVar[str] = "nifty_early_breakout"
    display_name: ClassVar[str] = "Index 5-min Early-Slot Breakout (ATM option buying)"
    description: ClassVar[str] = ("Strategy part 1: reference candles 09:30-14:55 (15 min before the original "
                                  "schedule), 10 lots; buy ATM CE above high+buffer or ATM PE below low−buffer; "
                                  "spot SL at the opposite end of the candle; hard exit at reference start + 15 min.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyEarlyBreakoutSettings
