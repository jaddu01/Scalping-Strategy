"""Exit monitoring for open trades: spot stop-loss, take-profit, timed exit, final square-off.

`evaluate_exit` is a pure function shared by the live engine and the backtester. The
ExitManager feeds it every tick of the trade's spot and option series and a 250 ms timer,
and asks the TradeManager to exit when it returns a reason. Exits keep working even if the
strategy instance that opened the trade crashes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from brokers.base import InstrumentKey, Tick
from core.clock import Clock

if TYPE_CHECKING:
    from engine.trades import TradeManager

log = logging.getLogger("algo.exits")


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


def evaluate_exit(st: ExitState, spot: float | None, premium: float | None, now: datetime) -> str | None:
    """Return the exit reason, or None. Checked in priority order on every update."""
    if spot is not None and st.spot_sl is not None:
        if (st.spot_sl_side == "below" and spot <= st.spot_sl) or (st.spot_sl_side == "above" and spot >= st.spot_sl):
            return st.sl_reason
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
