"""Dhurandhar: hourly 15-minute reference candle breakout (ATM option buying), from dhurandhar.xlsx.

  reference candles  09:15, 10:15, 11:15, 12:15, 13:15, 14:15 (15-min candle, HH:15-HH:30)
  entry              any time after the reference candle closes until the slot's exit:
                     spot > ref high + 5  ->  BUY the ATM CE   (spot SL: exit if spot <= ref low)
                     spot < ref low  - 5  ->  BUY the ATM PE   (spot SL: exit if spot >= ref high)
  hard exit          the next HH:15 (reference start + 60 min: 09:15 -> 10:15, ..., 14:15 -> 15:15)

Exits are the same multiple TPs and runner protection as the other breakout TP strategies (strategies/breakout_tp.py):
by default 4 TPs whose % depends on the entry premium's band (the engine picks the band from the actual fill), 5 lots
sold 2 / 1 / 1 / 1, and the runner floor on after TP3 has filled (step one behind + trailing by band). The sheet's
spot targets (TP1 40 / TP2 50 points) can be used instead: TP mode two_targets, measured in points (the runner floor
needs premium targets).
"""
from __future__ import annotations

from datetime import time
from typing import ClassVar, Literal

from pydantic import Field

from strategies.base import StrategySettings
from strategies.breakout_tp import NiftyRefBreakoutTP, NiftyRefBreakoutTPSettings
from strategies.registry import register

DHURANDHAR_REF_STARTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]


class DhurandharSettings(NiftyRefBreakoutTPSettings):
    reference_candle_starts: list[time] = Field(
        default_factory=lambda: [time.fromisoformat(t) for t in DHURANDHAR_REF_STARTS],
        title="Reference candle start times (IST)",
        description="Fixed schedule of reference candles, comma separated HH:MM (sheet: every hour at HH:15)",
    )
    candle_timeframe_min: int = Field(15, ge=1, le=60, title="Candle timeframe (minutes)",
                                      description="Length of the reference candle (sheet: 15 min)")
    buffer_points: float = Field(5.0, ge=0, le=500, title="Buffer (points)",
                                 description="Added to ref high / subtracted from ref low to get the triggers")
    entry_window_candles: int = Field(3, ge=1, le=24, title="Entry window (candles)",
                                      description="Entries allowed from reference close for this many candles "
                                                  "(3 × 15 min = until the next HH:15 exit)")
    exit_offset_min: int = Field(60, ge=1, le=375, title="Hard exit offset (minutes)",
                                 description="Hard exit = reference START + this many minutes (the next HH:15)")
    lots: int = Field(5, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")
    tp_mode: Literal["two_targets", "four_by_premium"] = Field(
        "four_by_premium", title="TP mode",
        description="four_by_premium: TP1-TP4 at the % of the entry premium's band · two_targets: TP1 / TP2 "
                    "(e.g. the sheet's 40 / 50 spot points with 'measured in' = points)")
    tp1_value: float = Field(40.0, gt=0, le=1000, title="TP1 distance",
                             description="two_targets only: 40 = 40 points (points), +40 % (premium_pct), 40 × "
                                         "range (range_multiple)")
    tp1_lots: int = Field(2, ge=0, le=200, title="TP1 lots")
    tp2_value: float = Field(50.0, gt=0, le=1000, title="TP2 distance", description="Must be beyond TP1")
    tp2_lots: int = Field(1, ge=0, le=200, title="TP2 lots")
    tp3_lots: int = Field(1, ge=0, le=200, title="TP3 lots (4 TPs)")
    tp4_lots: int = Field(1, ge=0, le=200, title="TP4 lots (4 TPs)",
                          description="5 lots = 2 / 1 / 1 / 1: every lot has a target; the runner floor protects "
                                      "the last one after TP3")
    tp_pcts_low: list[float] = Field(default_factory=lambda: [15.0, 22.0, 30.0, 40.0],
                                     title="4 TPs: % for the low band (TP1, TP2, TP3, TP4)")
    tp_pcts_mid: list[float] = Field(default_factory=lambda: [15.0, 22.0, 30.0, 40.0],
                                     title="4 TPs: % for the middle band")
    tp_pcts_high: list[float] = Field(default_factory=lambda: [8.0, 12.0, 15.0, 20.0],
                                      title="4 TPs: % for the high band")
    runner_protect: bool = Field(
        True, title="Protect the runner",
        description="On: after the start TP has filled, the lots still open exit when the premium falls to a floor "
                    "(step: the TP before the last filled one; trailing: the peak minus the band's %). Needs TP on "
                    "and 'measured in' = premium_pct. A change applies from the next trade")


@register
class DhurandharBreakout(NiftyRefBreakoutTP):
    template_id: ClassVar[str] = "dhurandhar_breakout"
    display_name: ClassVar[str] = "Dhurandhar: hourly 15-min Breakout + multi-TP + runner"
    description: ClassVar[str] = ("dhurandhar.xlsx: 15-min reference candles at 09:15, 10:15 … 14:15; buy the ATM CE "
                                  "above high + 5 or the ATM PE below low − 5 until the next HH:15; spot SL at the "
                                  "candle's other end; 4 TPs by entry premium (2/1/1/1 of 5 lots) with runner "
                                  "protection; hard exit at the next HH:15.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = DhurandharSettings
