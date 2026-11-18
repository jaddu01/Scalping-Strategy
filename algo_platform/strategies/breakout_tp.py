"""TP1 / TP2 variants of both breakout strategies.

Exact same entry, spot stop-loss and timed-exit rules as the originals; the only difference is the exit:
  TP1: sell TP1 lots when the option's price is up tp1 % from the entry fill (default +20 %)
  TP2: sell TP2 lots when it is up tp2 % (default +30 %)
  the remaining lots exit on the spot SL or at the slot's timed exit.
Lots per target: 0 = automatic, 40 % / 40 % / rest (5 lots -> 2/2/1, 10 lots -> 4/4/2).
TP can also be measured on NIFTY spot instead: in points, or as a multiple of the reference candle's
range (a 45-day study of Sep–Nov 2026 data found 1.0× / 1.25× range the best of the spot options).
TP mode "four_by_premium": four targets whose % depends on the entry premium (the engine picks the band from the
actual fill), lots TP1-TP4 (automatic 3/3/2/2 of 10); they may sell the whole position.
  entry premium      TP1    TP2    TP3    TP4
  below 80           20 %   30 %   40 %   50 %
  80 - 150           15 %   22 %   30 %   40 %
  above 150           8 %   12 %   20 %   25 %

Protect the runner (runner_protect, off by default): once the start TP's sell has FILLED (auto = TP2 with two
targets, TP3 with four), the lots still open get a premium floor, managed by the platform like the spot SL (no
order at the broker):
  step floor     = the level of the TP before the last filled TP (the entry fill if only TP1 has filled);
                   it moves up each time another TP fills
                   (step floor "tp1": TP1's level, fixed, instead of "one behind")
  running floor  = highest premium since the floor started × (1 − trail %); trail % by the entry fill's band
                   (below 80: 20 %, 80-150: 15 %, above 150: 10 %; 0 = no trailing for that band)
  floor          = max(step, running), rounded down to the tick, and it never moves down.
An option tick at or below floor × (1 − buffer %) exits all remaining lots. Exit reason: runner_floor_fallback_tp2
(tp1, entry, ...: the TP level that set the floor) or runner_floor_trail (the trailing % set it). Optional timeout:
the rest exits (runner_timeout) N minutes after TP1 filled, once the floor is on; a TP reached on that tick sells
first. The spot SL is checked first; the spot SL and the hard exit stay in force, whichever is hit first wins.
Unfilled TPs (e.g. TP4) stay active.
Like the TP choice, a change applies from the next trade.
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
    tp_mode: Literal["two_targets", "four_by_premium"] = Field(
        "two_targets", title="TP mode",
        description="two_targets: TP1 / TP2 as above · four_by_premium: TP1-TP4 at the % of the entry premium's "
                    "band (table below; the band is picked from the actual entry fill)")
    tp_band1_max: float = Field(80.0, gt=0, le=10000, title="4 TPs: low band = entry premium below")
    tp_band2_max: float = Field(150.0, gt=0, le=10000, title="4 TPs: middle band = entry premium up to",
                                description="Above this is the high band")
    tp_pcts_low: list[float] = Field(default_factory=lambda: [20.0, 30.0, 40.0, 50.0],
                                     title="4 TPs: % for the low band (TP1, TP2, TP3, TP4)")
    tp_pcts_mid: list[float] = Field(default_factory=lambda: [15.0, 22.0, 30.0, 40.0],
                                     title="4 TPs: % for the middle band")
    tp_pcts_high: list[float] = Field(default_factory=lambda: [8.0, 12.0, 20.0, 25.0],
                                      title="4 TPs: % for the high band")
    tp3_lots: int = Field(0, ge=0, le=200, title="TP3 lots (4 TPs)",
                          description="4 TPs: TP3 and TP4 lots both 0 = automatic 30/30/20/20 % (10 -> 3/3/2/2; "
                                      "TP1 / TP2 lots then stay the two-target split). Own split: set all of "
                                      "TP1-TP4 lots")
    tp4_lots: int = Field(0, ge=0, le=200, title="TP4 lots (4 TPs)")
    runner_protect: bool = Field(
        False, title="Protect the runner",
        description="On: after the start TP has filled, the lots still open exit when the premium falls to a floor "
                    "(step: the TP before the last filled one; trailing: the peak minus the band's %). Needs TP on "
                    "and 'measured in' = premium_pct. A change applies from the next trade")
    runner_start: Literal["auto", "tp1", "tp2", "tp3"] = Field(
        "auto", title="Runner floor starts after",
        description="auto = the last TP before the runner: TP2 with two targets, TP3 with 4 TPs. The floor starts "
                    "when that TP's sell has filled")
    runner_step_floor: Literal["off", "one_behind", "tp1"] = Field(
        "one_behind", title="Runner step floor",
        description="one_behind: the level of the TP before the last filled TP (the entry fill after TP1); moves "
                    "up as more TPs fill · tp1: TP1's level, fixed · off: trailing floor only")
    runner_trail_pct_low: float = Field(20.0, ge=0, lt=100, title="Runner trail % (entry premium in the low band)",
                                        description="Floor = highest premium since the start × (1 − %); 0 = no "
                                                    "trailing. Bands as the 4 TPs (below 80 / 80-150 / above 150)")
    runner_trail_pct_mid: float = Field(15.0, ge=0, lt=100, title="Runner trail % (middle band)")
    runner_trail_pct_high: float = Field(10.0, ge=0, lt=100, title="Runner trail % (high band)")
    runner_buffer_pct: float = Field(0.0, ge=0, le=20, title="Runner floor buffer %",
                                     description="Exit when the premium is at or below floor × (1 − %), a little "
                                                 "below the level against noise; 0 = exactly at the floor")
    runner_timeout_min: int = Field(0, ge=0, le=375, title="Runner timeout (minutes after TP1 filled)",
                                    description="0 = off. Once the floor is on, the remaining lots exit this many "
                                                "minutes after TP1's sell filled (reason runner_timeout)")

    @model_validator(mode="after")
    def _runner_checks(self) -> "TPSettings":
        if not self.runner_protect:
            return self
        if not self.tp_enabled:
            raise ValueError("Protect the runner needs 'Use TP1 / TP2' on: the floor starts after a TP has filled")
        if self.tp_basis != "premium_pct":
            raise ValueError("Protect the runner needs 'TP1/TP2 measured in' = premium_pct (the floor is on the "
                             "option premium)")
        start = self.runner_start_label()
        lots = list(self.split4()) if self.tp_mode == "four_by_premium" else list(self.split())
        n = int(start[2])
        if n > len(lots):
            raise ValueError(f"Runner floor starts after {start.upper()}, but {self.tp_mode} has only TP1-TP{len(lots)}; "
                             f"choose auto or TP1-TP{len(lots)}")
        if lots[n - 1] <= 0:
            raise ValueError(f"Runner floor starts after {start.upper()}, but {start.upper()} gets no lots with "
                             f"{self.lots} lots; choose an earlier TP or more lots")
        if self.lots - sum(lots[:n]) < 1:
            raise ValueError(f"Protect the runner: no lot is left after {start.upper()} "
                             f"({'/'.join(map(str, lots[:n]))} of {self.lots} lots); choose an earlier TP")
        if self.runner_step_floor == "tp1" and n == 1:
            raise ValueError("Runner step floor tp1 with the floor starting after TP1 would sit at the price just "
                             "sold; start after TP2 / TP3 or use one_behind")
        if self.runner_step_floor == "off" and not any(
                (self.runner_trail_pct_low, self.runner_trail_pct_mid, self.runner_trail_pct_high)):
            raise ValueError("Protect the runner: with the step floor off, set a trail % for at least one band")
        return self

    def runner_start_label(self) -> str:
        if self.runner_start != "auto":
            return self.runner_start
        return "tp3" if self.tp_mode == "four_by_premium" else "tp2"

    def runner_plan(self) -> dict | None:
        """The runner config stored on each new trade (None = off); the engine picks the trail % from the fill."""
        if not (self.runner_protect and self.tp_enabled):
            return None
        return {"start": self.runner_start_label(), "step": self.runner_step_floor,
                "band1_max": self.tp_band1_max, "band2_max": self.tp_band2_max,
                "trail_pcts": [self.runner_trail_pct_low, self.runner_trail_pct_mid, self.runner_trail_pct_high],
                "buffer_pct": self.runner_buffer_pct, "timeout_min": self.runner_timeout_min}

    @model_validator(mode="after")
    def _tp_checks(self) -> "TPSettings":
        if not self.tp_enabled:
            return self  # targets are ignored while switched off
        if self.take_profit_mode is not TakeProfitMode.NONE:
            raise ValueError("set Take-profit mode to none: this strategy exits with TP1, TP2 and the timed exit")
        if self.tp_mode == "four_by_premium":
            return self._four_checks()
        if self.tp2_value <= self.tp1_value:
            raise ValueError("TP2 distance must be greater than TP1 distance")
        if (self.tp1_lots == 0) != (self.tp2_lots == 0):
            raise ValueError("set both TP1 lots and TP2 lots to 0 (automatic split) or both to a number of lots")
        tp1, tp2 = self.split()
        if tp1 + tp2 >= self.lots:
            raise ValueError(f"TP1 lots + TP2 lots ({tp1 + tp2}) must be less than Lots "
                             f"({self.lots}) so at least one lot runs to the timed exit")
        return self

    def _four_checks(self) -> "TPSettings":
        if self.tp_basis != "premium_pct":
            raise ValueError("4 TPs by entry premium need 'TP1/TP2 measured in' = premium_pct")
        if not 0 < self.tp_band1_max < self.tp_band2_max:
            raise ValueError("4 TPs: the middle band's upper premium must be above the low band's")
        for name, pcts in (("low", self.tp_pcts_low), ("middle", self.tp_pcts_mid), ("high", self.tp_pcts_high)):
            if len(pcts) != 4:
                raise ValueError(f"4 TPs: the {name} band needs 4 percentages (TP1, TP2, TP3, TP4)")
            if any(p <= 0 for p in pcts) or any(b <= a for a, b in zip(pcts, pcts[1:])):
                raise ValueError(f"4 TPs: the {name} band's percentages must be above 0 and rise TP1 < TP2 < TP3 < TP4")
        own = (self.tp1_lots, self.tp2_lots, self.tp3_lots, self.tp4_lots)
        if (self.tp3_lots or self.tp4_lots) and not all(own):
            raise ValueError("4 TPs: for your own split set each of TP1-TP4 lots (e.g. 3/3/2/2); "
                             "TP3 and TP4 lots both 0 = automatic 3/3/2/2 of 10")
        if sum(self.split4()) > self.lots:
            raise ValueError(f"4 TPs: TP1-TP4 lots ({sum(self.split4())}) are more than Lots ({self.lots})")
        return self

    def split4(self) -> tuple[int, int, int, int]:
        """TP1-TP4 lots: all four set = that split; TP3 and TP4 both 0 = automatic 30 / 30 / 20 / 20 % rounded down
        (10 -> 3/3/2/2, 5 -> 1/1/1/1) whatever the two-target TP1 / TP2 lots are."""
        own = (self.tp1_lots, self.tp2_lots, self.tp3_lots, self.tp4_lots)
        if all(own):
            return own  # type: ignore[return-value]
        a, b = max(1, self.lots * 3 // 10), max(1, self.lots * 2 // 10)
        out = [a, a, b, b]
        i = len(out) - 1
        while sum(out) > self.lots:  # tiny positions (1-3 lots): the last targets get no lots
            while out[i] == 0:
                i -= 1
            out[i] -= 1
        return tuple(out)  # type: ignore[return-value]

    def bands(self, i: int) -> tuple[tuple[float, float], ...]:
        """Premium bands of target i (0 = TP1) for the engine: ((low bound, %), (mid bound, %), (inf, %))."""
        return ((self.tp_band1_max, self.tp_pcts_low[i]), (self.tp_band2_max, self.tp_pcts_mid[i]),
                (float("inf"), self.tp_pcts_high[i]))

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
    if settings.tp_mode == "four_by_premium":
        return [PartialTarget(f"tp{i + 1}", None, "above", n, premium_bands=settings.bands(i))
                for i, n in enumerate(settings.split4()) if n > 0]
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

    def runner_plan(self) -> dict | None:
        assert isinstance(self.settings, TPSettings)
        return self.settings.runner_plan()


@register
class NiftyEarlyBreakoutTP(NiftyRefBreakoutTP):
    template_id: ClassVar[str] = "nifty_early_breakout_tp"
    display_name: ClassVar[str] = "Index 5-min Early-Slot Breakout + TP1/TP2"
    description: ClassVar[str] = ("Early-slot schedule (09:30–14:55) and rules (10 lots); sells 4 lots at TP1 (+20 % "
                                  "premium) and 4 at TP2 (+30 %); the last 2 lots exit on the spot SL or the timed exit.")
    settings_model: ClassVar[type[StrategySettings]] = NiftyEarlyBreakoutTPSettings
