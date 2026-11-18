"""NIFTY 2-Entry Breakout — "2 entry strategy_nifty.xlsx".

Two slots a day, each with its own hard exit:
  slot 1: reference candle 09:45-09:50, entry any time after 09:50 until 13:30, hard exit 13:30
  slot 2: reference candle 13:30-13:35, entry any time after 13:35 until 15:15, hard exit 15:15
Same rules as the other breakout strategies:
  spot > ref_high + 2.25  ->  BUY ATM CE   (spot SL: exit if spot <= ref_low)
  spot < ref_low  - 2.25  ->  BUY ATM PE   (spot SL: exit if spot >= ref_high)
  first breakout only, one trade per slot, no re-entry after the SL, one open position at a time; 10 lots.
The exits are at the sharp time (Exit early by = 0 s). The TP version also sells 40 % of the lots at
TP1 (+30 % premium) and 40 % at TP2 (+40 %); the rest exits on the spot SL or the hard exit.
"Protect the runner" (off by default) adds a premium floor under those last lots once TP2 (TP3 with 4 TPs) has
filled: the TP before the last filled one, or the peak minus 20 / 15 / 10 % by entry band, whichever is higher; it
only rises and exits the rest (reason runner_floor_fallback_tp2 / _tp1 / runner_floor_trail), optionally a buffer %
below the floor and after a timeout counted from TP1's fill (details in strategies/breakout_tp.py).
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any, ClassVar

from pydantic import Field, field_validator, model_validator

from core.clock import fmt_hm
from strategies.base import StrategySettings
from strategies.breakout_tp import NiftyRefBreakoutTP, TPSettings
from strategies.nifty_ref_breakout import (MARKET_CLOSE, BreakoutBaseSettings, NiftyRefBreakout,
                                           check_slot_basics, check_slot_exit)
from strategies.registry import register

DEFAULT_SLOTS = "09:45-13:30, 13:30-15:15"
HHMM = re.compile(r"^\d{1,2}:\d{2}$")
MAX_SLOTS = 20


def parse_slots(text: str) -> list[tuple[time, time]]:
    """'09:45-13:30, 13:30-15:15' -> [(09:45, 13:30), (13:30, 15:15)]; raises ValueError with a clear message."""
    out = []
    for part in (p.strip() for p in text.replace(";", ",").replace("\n", ",").split(",")):
        if not part:
            continue
        bits = [b.strip() for b in part.replace("–", "-").split("-")]
        if len(bits) != 2:
            raise ValueError(f"'{part}': write each slot as HH:MM-HH:MM (reference start - exit)")
        try:
            if not (HHMM.match(bits[0]) and HHMM.match(bits[1])):
                raise ValueError
            start, exit_ = (time.fromisoformat(b.zfill(5)) for b in bits)
        except ValueError:
            raise ValueError(f"'{part}': times must be HH:MM, e.g. 09:45-13:30") from None
        out.append((start, exit_))
    return out


class NiftyTwoEntrySettings(BreakoutBaseSettings):
    FORM_FIRST: ClassVar[tuple[str, ...]] = ("underlying", "exchange", "slots", "candle_timeframe_min",
                                             "buffer_points", "exit_early_sec", "lots")

    slots: str = Field(
        DEFAULT_SLOTS, title="Slots (reference start - hard exit)",
        description="Comma separated HH:MM-HH:MM. Each slot's reference candle starts at the first time; entry is "
                    "allowed from the reference close until the slot's hard exit (the second time)")
    lots: int = Field(10, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")
    exit_early_sec: int = Field(0, ge=0, le=60, title="Exit early by (seconds)",
                                description="0 = exit at the sharp time (13:30:00, 15:15:00). The entry window "
                                            "closes at the same moment")

    @field_validator("slots", mode="before")
    @classmethod
    def _join_list(cls, v: Any) -> Any:
        return ", ".join(v) if isinstance(v, (list, tuple)) else v

    @field_validator("slots")
    @classmethod
    def _normalise(cls, v: str) -> str:
        pairs = parse_slots(v)
        if not pairs:
            raise ValueError("at least one slot is required, e.g. 09:45-13:30")
        if len(pairs) > MAX_SLOTS:
            raise ValueError(f"at most {MAX_SLOTS} slots")
        if len({s for s, _ in pairs}) != len(pairs):
            raise ValueError("slot reference start times must be unique")
        return ", ".join(f"{fmt_hm(s)}-{fmt_hm(e)}" for s, e in sorted(pairs))

    @model_validator(mode="after")
    def _schedule_checks(self) -> "NiftyTwoEntrySettings":
        tf, early = self.candle_timeframe_min, timedelta(seconds=self.exit_early_sec)
        prev_exit: datetime | None = None
        day = datetime(2000, 1, 3)
        for start, exit_ in self.slot_pairs():
            check_slot_basics(start, tf)
            if exit_ > MARKET_CLOSE:
                raise ValueError(f"slot {fmt_hm(start)}: exit {fmt_hm(exit_)} is after market close 15:30")
            if exit_.second:
                raise ValueError(f"slot {fmt_hm(start)}: exit must be a whole minute")
            s_dt, e_dt = datetime.combine(day, start), datetime.combine(day, exit_)
            check_slot_exit(start, e_dt - s_dt, self.exit_early_sec, tf)
            if self.entry_trigger == "candle_close" and s_dt + timedelta(minutes=2 * tf) >= e_dt - early:
                raise ValueError(f"slot {fmt_hm(start)}: with candle_close entry at least one candle must close "
                                 f"between the reference close and the exit {fmt_hm(exit_)}")
            if prev_exit is not None and s_dt < prev_exit:
                raise ValueError(f"slot {fmt_hm(start)} starts before the previous slot's exit "
                                 f"{prev_exit:%H:%M:%S}; slots must not overlap (one position at a time)")
            prev_exit = e_dt - early
        return self

    def slot_pairs(self) -> list[tuple[time, time]]:
        return parse_slots(self.slots)

    def slot_times(self) -> list[tuple[time, timedelta, timedelta]]:
        out = []
        for start, exit_ in self.slot_pairs():
            off = datetime.combine(datetime(2000, 1, 3), exit_) - datetime.combine(datetime(2000, 1, 3), start)
            out.append((start, off, off))  # entry allowed until the hard exit
        return out


class NiftyTwoEntryTPSettings(NiftyTwoEntrySettings, TPSettings):
    tp1_value: float = Field(30.0, gt=0, le=1000, title="TP1 distance",
                             description="e.g. 30 = +30 % premium (premium_pct), 1.0 = one candle range "
                                         "(range_multiple) or 30 = 30 points (points)")
    tp2_value: float = Field(40.0, gt=0, le=1000, title="TP2 distance", description="Must be beyond TP1")


@register
class NiftyTwoEntry(NiftyRefBreakout):
    template_id: ClassVar[str] = "nifty_two_entry"
    display_name: ClassVar[str] = "NIFTY 2-Entry Breakout (09:45 → 13:30, 13:30 → 15:15)"
    description: ClassVar[str] = ("Two slots a day: reference 09:45-09:50 with entry until the 13:30 hard exit, and "
                                  "reference 13:30-13:35 with entry until the 15:15 hard exit; 10 lots; ATM CE above "
                                  "high+buffer / ATM PE below low−buffer; spot SL at the opposite end of the candle.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyTwoEntrySettings


@register
class NiftyTwoEntryTP(NiftyRefBreakoutTP):
    template_id: ClassVar[str] = "nifty_two_entry_tp"
    display_name: ClassVar[str] = "NIFTY 2-Entry Breakout + TP1/TP2"
    description: ClassVar[str] = ("2-Entry schedule and rules (10 lots); sells 4 lots at TP1 (+30 % premium) and 4 at "
                                  "TP2 (+40 %); the last 2 lots exit on the spot SL or the hard exit.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyTwoEntryTPSettings
