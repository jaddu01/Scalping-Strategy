"""Exit monitoring for open trades: spot stop-loss, runner floor, take-profit, timed exit, final square-off.

`evaluate_exit` is a pure function shared by the live engine and the backtester, as are the runner-floor
helpers (`runner_from_plan`, `runner_on_fill`, `runner_on_premium`, `runner_hit`). The
ExitManager feeds it every tick of the trade's spot and option series and a 250 ms timer,
and asks the TradeManager to exit when it returns a reason. Exits keep working even if the
strategy instance that opened the trade crashes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from brokers.base import InstrumentKey, Tick
from brokers.instruments import round_to_tick
from core.clock import Clock

if TYPE_CHECKING:
    from engine.trades import TradeManager

log = logging.getLogger("algo.exits")

RUNNER_LOG_STEP = 0.005  # RUNNER_FLOOR_RAISED is logged when the floor is up >= 0.5 % since the last log line


def band_pct(bands: Any, entry_price: float) -> float:
    """The % of the premium band the entry fill falls in: below the first bound, up to and including the second,
    ... (e.g. ((80, 20), (150, 15), (inf, 8)): 79.95 -> 20, 80 -> 15, 150 -> 15, 150.05 -> 8)."""
    first = True
    for bound, pct in bands:
        if (entry_price < bound) if first else (entry_price <= bound):
            return float(pct)
        first = False
    return float(bands[-1][1])


@dataclass
class RunnerState:
    """Runner protection: a premium floor under the lots left after the start TP has FILLED.

    floor = max(step floor, peak since the start × (1 − trail %)), rounded down to the tick; it only rises.
    Platform-managed like the spot SL: no order is sent to the broker for it. One object is shared by the live
    trade and its ExitState (like `partials`), and stored on the trade's meta so a restart keeps it."""

    start: str  # label of the TP whose fill starts the floor ("tp2", "tp3", ...)
    # "off" | "one_behind": the level of the TP before the last filled one (or the entry) | "tp1": TP1's level
    step: str = "one_behind"
    trail_pct: float = 0.0  # 0 = no trailing floor (picked from the entry fill's premium band)
    tick: float = 0.05
    buffer_pct: float = 0.0  # exit when premium <= floor x (1 - buffer %): a little below the level, against noise
    timeout_min: int = 0  # 0 = off; else exit the rest this many minutes after TP1 filled (once the floor is on)
    active: bool = False
    peak: float | None = None
    step_floor: float | None = None
    step_label: str | None = None  # whose level the step floor is: "tp1", "tp2", ... or "entry"
    floor: float | None = None
    floor_source: str | None = None  # "step" or "trail": which part set the floor in force
    logged_floor: float | None = None  # floor of the last ARMED / RAISED line
    tp1_at: datetime | None = None  # when TP1's sell filled (the timeout's clock)

    _KEYS = ("start", "step", "trail_pct", "tick", "buffer_pct", "timeout_min", "active", "peak", "step_floor",
             "step_label", "floor", "floor_source", "logged_floor", "tp1_at")

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self._KEYS}
        d["tp1_at"] = self.tp1_at.isoformat() if self.tp1_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> RunnerState | None:
        if not d or not d.get("start"):
            return None
        kw = {k: d[k] for k in cls._KEYS if k in d}
        if kw.get("tp1_at"):
            kw["tp1_at"] = datetime.fromisoformat(kw["tp1_at"])
        return cls(**kw)

    @property
    def exit_level(self) -> float | None:
        """The premium at or below which the rest exits: the floor, less the buffer %."""
        if self.floor is None:
            return None
        if not self.buffer_pct:
            return self.floor
        return round_to_tick(self.floor * (1 - self.buffer_pct / 100), self.tick, "down")

    @property
    def timeout_at(self) -> datetime | None:
        if not self.timeout_min or self.tp1_at is None:
            return None
        return self.tp1_at + timedelta(minutes=self.timeout_min)

    def floor_reason(self) -> str:
        """Exit reason: runner_floor_fallback_tp2 / _tp1 / _entry when a TP level set the floor, runner_floor_trail
        when the trailing % did."""
        if self.floor_source == "trail" or not self.step_label:
            return "runner_floor_trail"
        return f"runner_floor_fallback_{self.step_label}"

    def text(self) -> str:
        """Dashboard line: 'Runner floor 197.55 (peak 213.85)' / 'Runner floor: waiting for TP3'."""
        if not self.active or self.floor is None:
            return f"Runner floor: waiting for {self.start.upper()}"
        extra = f"exit ≤ {self.exit_level:.2f}, " if self.buffer_pct else ""
        out = f"Runner floor {self.floor:.2f} ({extra}peak {self.peak:.2f})"
        if self.timeout_at is not None:
            out += f" · timeout {self.timeout_at:%H:%M:%S}"
        return out


def runner_from_plan(cfg: dict[str, Any] | None, entry_price: float, tick: float = 0.05) -> RunnerState | None:
    """The trade's runner state from the strategy's plan ({start, step, band1_max, band2_max, trail_pcts: [low, mid,
    high]}); the trail % is picked once, from the actual entry fill, with the same band bounds as the 4-TP targets."""
    if not cfg:
        return None
    low, mid, high = cfg["trail_pcts"]
    pct = band_pct(((cfg["band1_max"], low), (cfg["band2_max"], mid), (float("inf"), high)), entry_price)
    return RunnerState(start=cfg["start"], step=cfg.get("step", "one_behind"), trail_pct=pct, tick=tick,
                       buffer_pct=float(cfg.get("buffer_pct") or 0.0), timeout_min=int(cfg.get("timeout_min") or 0))


def _runner_recompute(r: RunnerState) -> None:
    trail = r.peak * (1 - r.trail_pct / 100) if r.trail_pct and r.peak else None
    cands = [x for x in (r.step_floor, trail) if x is not None]
    if not cands:
        return
    new = round_to_tick(max(cands), r.tick, "down")
    if r.floor is None or new > r.floor:
        r.floor = new  # only ever rises
        r.floor_source = "trail" if trail is not None and (r.step_floor is None or trail > r.step_floor) else "step"


def runner_on_fill(r: RunnerState | None, partials: list[dict[str, Any]], entry_price: float,
                   now: datetime | None = None) -> str | None:
    """A partial sell has been BOOKED (its fill is in `partials`). Arms the floor when the start TP has filled,
    or lifts the step floor when a later TP fills. Returns "armed", "raised" or None."""
    if r is None:
        return None
    filled = [i for i, p in enumerate(partials) if p.get("filled")]
    if not filled:
        return None
    if r.tp1_at is None and partials and partials[0].get("filled"):
        r.tp1_at = now  # the timeout counts from TP1's fill
    if not r.active:
        start = next((p for p in partials if p.get("label") == r.start), None)
        if start is None or not start.get("filled") or start.get("price") is None:
            return None  # triggered but not filled (rejected / timed out): not armed
        r.active, r.peak = True, float(start["price"])
        event = "armed"
    else:
        event = "raised"
    level, label = None, None
    if r.step == "one_behind":
        last = filled[-1]
        level, label = ((entry_price, "entry") if last == 0
                        else (partials[last - 1].get("level"), partials[last - 1].get("label")))
    elif r.step == "tp1" and partials:
        level, label = partials[0].get("level"), partials[0].get("label")
    if level is not None and (r.step_floor is None or level > r.step_floor):
        r.step_floor, r.step_label = float(level), label
    _runner_recompute(r)
    r.logged_floor = r.floor
    return event


def runner_on_premium(r: RunnerState | None, premium: float | None) -> tuple[bool, bool]:
    """Option tick: lift the peak and the trailing floor. Returns (floor changed, worth a RAISED log line)."""
    if r is None or not r.active or premium is None:
        return False, False
    before = r.floor
    if r.peak is None or premium > r.peak:
        r.peak = premium
        _runner_recompute(r)
    changed = r.floor != before
    big = changed and r.floor is not None and (
        r.logged_floor is None or r.floor >= r.logged_floor * (1 + RUNNER_LOG_STEP) - 1e-9)
    if big:
        r.logged_floor = r.floor
    return changed, big


def runner_hit(r: RunnerState | None, premium: float | None) -> bool:
    lvl = r.exit_level if r is not None else None
    return r is not None and r.active and lvl is not None and premium is not None and premium <= lvl


def runner_timed_out(r: RunnerState | None, now: datetime) -> bool:
    due = r.timeout_at if r is not None and r.active else None
    return due is not None and now >= due


@dataclass
class ExitState:
    trade_db_id: int
    trade_ref: str
    instance_id: int | None
    instance_name: str
    account_id: int
    mode: str
    direction: str
    spot_series: str | None
    option_series: str
    option_key: InstrumentKey
    spot_key: InstrumentKey | None
    entry_price: float
    quantity: int
    exit_at: datetime
    square_off_at: datetime | None = None
    spot_sl: float | None = None
    spot_sl_side: str = "below"
    tp_spot_level: float | None = None
    tp_spot_side: str = "above"
    tp_premium_level: float | None = None
    last_spot: float | None = None
    last_premium: float | None = None
    triggered: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # scale-out targets: dicts {label, level, side, qty, done, filled, price}; shared with the live trade
    partials: list[dict[str, Any]] = field(default_factory=list)
    sl_reason: str = "spot_stop_loss"  # "trailing_stop" once a strategy has moved the stop
    # runner protection (config, active, peak, floor); None = off. Shared with the live trade
    runner: RunnerState | None = None


def evaluate_exit(st: ExitState, spot: float | None, premium: float | None, now: datetime) -> str | None:
    """Return the exit reason, or None. Checked in priority order on every update."""
    if spot is not None and st.spot_sl is not None:
        if (st.spot_sl_side == "below" and spot <= st.spot_sl) or (st.spot_sl_side == "above" and spot >= st.spot_sl):
            return st.sl_reason
    if runner_hit(st.runner, premium):
        return st.runner.floor_reason()  # type: ignore[union-attr]
    if runner_timed_out(st.runner, now) and evaluate_partial(st, spot, premium) is None:
        return "runner_timeout"  # a TP reached on this same tick (e.g. TP4) sells first
    if spot is not None and st.tp_spot_level is not None:
        if (st.tp_spot_side == "above" and spot >= st.tp_spot_level) or (
                st.tp_spot_side == "below" and spot <= st.tp_spot_level):
            return "take_profit"
    if premium is not None and st.tp_premium_level is not None and premium >= st.tp_premium_level:
        return "take_profit"
    sq = st.square_off_at
    if sq is not None and sq < st.exit_at and now >= sq:
        return "square_off"
    if now >= st.exit_at:
        return "timed_exit"
    if sq is not None and now >= sq:
        return "square_off"
    return None


def evaluate_partial(st: ExitState, spot: float | None, premium: float | None = None) -> int | None:
    """Index of the first scale-out target (TP1, TP2, ...) that has been reached and is not done yet.
    Spot targets compare NIFTY spot with the level; premium targets compare the option's price."""
    for i, p in enumerate(st.partials):
        if p.get("done") or p.get("level") is None:
            continue
        if p.get("basis") == "premium":
            if premium is not None and premium >= p["level"]:
                return i
        elif spot is not None and ((p["side"] == "above" and spot >= p["level"])
                                   or (p["side"] == "below" and spot <= p["level"])):
            return i
    return None


class ExitManager:
    def __init__(self, clock: Clock, mtm_interval: Callable[[], float]) -> None:
        self.clock = clock
        self.mtm_interval = mtm_interval
        self.trades: "TradeManager | None" = None
        self.states: dict[int, ExitState] = {}
        self._by_series: dict[str, set[int]] = {}
        self._task: asyncio.Task | None = None
        self._last_mtm: dict[int, datetime] = {}

    def register(self, st: ExitState) -> None:
        self.states[st.trade_db_id] = st
        for series in (st.spot_series, st.option_series):
            if series:
                self._by_series.setdefault(series, set()).add(st.trade_db_id)

    def unregister(self, trade_db_id: int) -> ExitState | None:
        st = self.states.pop(trade_db_id, None)
        for ids in self._by_series.values():
            ids.discard(trade_db_id)
        self._last_mtm.pop(trade_db_id, None)
        return st

    def on_tick(self, series: str, key: InstrumentKey, tick: Tick) -> None:
        ids = self._by_series.get(series)
        if not ids:
            return
        now = self.clock.now()
        for tid in list(ids):
            st = self.states.get(tid)
            if st is None or st.triggered:
                continue
            if series == st.spot_series:
                st.last_spot = tick.ltp
            if series == st.option_series:
                st.last_premium = tick.ltp
                if st.runner is not None and self.trades is not None:
                    changed, big = runner_on_premium(st.runner, tick.ltp)
                    if changed:
                        self.trades.runner_moved(st, big)
            self._check(st, now)

    def _check(self, st: ExitState, now: datetime) -> None:
        if self.trades is None:
            return
        reason = evaluate_exit(st, st.last_spot, st.last_premium, now)
        if reason is None:
            # full exits (spot SL, timed exit) win over a target hit on the same tick
            idx = evaluate_partial(st, st.last_spot, st.last_premium)
            while idx is not None:
                st.partials[idx]["done"] = True
                asyncio.get_running_loop().create_task(
                    self.trades.exit_partial(st.trade_db_id, idx, spot=st.last_spot, premium=st.last_premium),
                    name=f"partial-{st.trade_ref}-{st.partials[idx]['label']}")
                idx = evaluate_partial(st, st.last_spot, st.last_premium)
            return
        st.triggered = True
        asyncio.get_running_loop().create_task(
            self.trades.exit_trade(st.trade_db_id, reason, spot=st.last_spot, premium=st.last_premium),
            name=f"exit-{st.trade_ref}")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="exit-manager")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _next_wait(self) -> float:
        """Sleep 250 ms, or less so a timed exit fires on its exact second (10:00:00, not 10:00:00.2)."""
        now = self.clock.now()
        due = [st.exit_at for st in self.states.values() if not st.triggered and st.exit_at > now]
        due += [st.square_off_at for st in self.states.values()
                if not st.triggered and st.square_off_at is not None and st.square_off_at > now]
        due += [t for st in self.states.values() if not st.triggered and st.runner is not None and st.runner.active
                and (t := st.runner.timeout_at) is not None and t > now]
        if not due:
            return 0.25
        return min(0.25, max(0.005, (min(due) - now).total_seconds()))

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._next_wait())
                now = self.clock.now()
                for st in list(self.states.values()):
                    if not st.triggered:
                        self._check(st, now)
                    last = self._last_mtm.get(st.trade_db_id)
                    if self.trades is not None and (last is None or (now - last).total_seconds() >= self.mtm_interval()):
                        self._last_mtm[st.trade_db_id] = now
                        self.trades.mtm_snapshot(st)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("exit manager loop error")
