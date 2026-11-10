"""Runs each enabled strategy instance as an isolated asyncio task.

An exception inside one instance is caught, logged, alerted and marks only that instance as
ERROR; its open position (if any) keeps being managed by the ExitManager. Instances can be
enabled/disabled and re-parameterised at runtime without restarting the app.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, Tick
from core.events import Category, Level
from core.settings_schema import InstanceRiskLimits
from engine.market_data import series_of
from storage.models import StrategyInstance
from strategies.base import EntrySignal, Strategy, TradeInfo
from strategies.registry import get_template

if TYPE_CHECKING:
    from engine.platform import Platform

log = logging.getLogger("algo.strategies")

STOPPED, STARTING, WAITING, RUNNING, IN_TRADE, STOPPING, ERROR = (
    "STOPPED", "STARTING", "WAITING", "RUNNING", "IN_TRADE", "STOPPING", "ERROR")


class InstanceRuntime:
    """One running strategy instance. Implements the StrategyContext protocol."""

    def __init__(self, platform: "Platform", row: StrategyInstance) -> None:
        self.p = platform
        self.instance_id = row.id
        self.name = row.name
        self.template_id = row.template_id
        self.mode = row.mode
        self.account_id = row.broker_account_id
        self.risk_limits = InstanceRiskLimits.model_validate(row.risk_limits or {})
        cls = get_template(row.template_id)
        self.strategy: Strategy = cls(cls.settings_model.model_validate(row.params or {}), self)
        self.status = STOPPED
        self.status_message: str | None = None
        self.stop_after_trade = row.stop_after_trade
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = False
        self._ticks: dict[str, Tick] = {}
        self._candles: list[tuple[Instrument, Candle]] = []
        self._trade_updates: list[TradeInfo] = []
        self._series_inst: dict[str, Instrument] = {}
        self._subscribed: list[InstrumentKey] = []
        self._market_sig: tuple[tuple[str, ...], tuple[int, ...]] = ((), ())
        self.started_at: datetime | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ StrategyContext
    def now(self) -> datetime:
        return self.p.clock.now()

    def emit(self, event_type: str, message: str, level: Level = Level.INFO, category: Category = Category.STRATEGY,
             trade_id: str | None = None, **payload: Any) -> None:
        acc = self.p.accounts.get(self.account_id) if self.account_id else None
        self.p.bus.emit(category, event_type, message, level, instance_id=self.instance_id, instance_name=self.name,
                        account_id=self.account_id, account_label=acc.label if acc else None, trade_id=trade_id,
                        **payload)

    def resolve(self, key: InstrumentKey) -> Instrument:
        return self.p.market.resolve(key)

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        acc = self.p.accounts.get(self.account_id) if self.account_id else None
        if acc is not None:
            try:
                adapter, _ = acc.order_route(self.mode)
                exp = adapter.option_expiries(underlying, exchange)
                if exp:
                    return exp
            except Exception:
                pass
        return self.p.market.option_expiries(underlying, exchange)

    def ltp(self, instrument: Instrument) -> float | None:
        return self.p.market.ltp(instrument)

    async def get_candle(self, instrument: Instrument, start: datetime, timeframe_min: int) -> Candle | None:
        return await self.p.market.get_candle(instrument, start, timeframe_min)

    async def submit_entry(self, signal: EntrySignal) -> TradeInfo | None:
        if self.stop_after_trade or self.status == STOPPING or self._stop:
            self.emit("SIGNAL_REJECTED", f"dir={signal.direction.value} reason=instance_stopping", Level.WARNING)
            return None
        return await self.p.trades.open_trade(self, signal)

    def has_open_position(self) -> bool:
        return self.p.trades.open_trade_count(instance_id=self.instance_id) > 0

    def square_off_time(self) -> time:
        return self.p.settings.market.final_square_off

    async def history(self, instrument: Instrument, timeframe_min: int, count: int) -> list[Candle]:
        return await self.p.market.history(instrument, timeframe_min, count)

    async def request_exit(self, trade_id: str, reason: str, partial: str | None = None) -> None:
        # runs in the background: the strategy keeps processing ticks while the exit order fills
        asyncio.get_running_loop().create_task(
            self.p.trades.exit_by_ref(trade_id, reason, instance_id=self.instance_id, partial=partial),
            name=f"strategy-exit-{trade_id}")

    def _day_state_key(self, day: date) -> str:
        return f"slots:{self.instance_id}:{day.isoformat()}"

    def load_day_state(self, day: date) -> dict[str, Any]:
        from storage.repo import load_paper_state

        return load_paper_state(self.p.db, self._day_state_key(day))

    def save_day_state(self, day: date, state: dict[str, Any]) -> None:
        from storage.repo import save_paper_state

        save_paper_state(self.p.db, self._day_state_key(day), state)

    # ------------------------------------------------------------------ inputs
    def _on_tick(self, series: str, tick: Tick) -> None:
        inst = self._series_inst.get(series)
        if inst is not None and inst.token != tick.token:
            tick = tick.model_copy(update={"token": inst.token, "exchange": inst.exchange})
        self._ticks[series] = tick
        self._wake.set()

    def _on_candle(self, series: str, candle: Candle) -> None:
        inst = self._series_inst.get(series)
        if inst is not None:
            self._candles.append((inst, candle))
            self._wake.set()

    def post_trade_update(self, info: TradeInfo) -> None:
        self._trade_updates.append(info)
        self._wake.set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.get_running_loop().create_task(self._main(), name=f"instance-{self.name}")

    async def stop(self, reason: str = "stopped") -> None:
        self._stop = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self._task = None
        if self.status != ERROR:
            self._set_status(STOPPED, reason)

    def _set_status(self, status: str, message: str | None = None) -> None:
        if status == self.status and message == self.status_message:
            return
        self.status, self.status_message = status, message
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, self.instance_id)
            if row is not None:
                row.status, row.status_message = status, message
                row.stop_after_trade = self.stop_after_trade

    async def _wait_for_account(self) -> bool:
        acc = self.p.accounts.get(self.account_id) if self.account_id else None
        if acc is None:
            raise RuntimeError("instance has no broker account (or the account was deleted)")
        if self.mode == "live" and acc.is_paper_account:
            raise RuntimeError(f"{acc.label} is a paper account; live mode needs a real broker account")
        warned = False
        while not self._stop:
            if acc.ready.is_set():
                return True
            if not warned:
                self.emit("WAITING_FOR_ACCOUNT", f"waiting for {acc.label} to connect and reconcile", Level.INFO)
                warned = True
            self._set_status(STARTING, f"waiting for {acc.label}")
            try:
                await asyncio.wait_for(acc.ready.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        return False

    async def _wait_for_feed(self) -> bool:
        warned = False
        while not self._stop:
            if self.p.market.feed is not None or await self.p.market.ensure_feed():
                return True
            if not warned:
                self.emit("WAITING_FOR_FEED", "no market data feed account is connected", Level.WARNING)
                warned = True
            self._set_status(STARTING, "waiting for market data feed")
            await asyncio.sleep(2)
        return False

    async def _main(self) -> None:
        self.started_at = self.now()
        self._set_status(STARTING)
        try:
            if not await self._wait_for_account() or not await self._wait_for_feed():
                return
            await self._subscribe_market()
            await self.strategy.on_start()
            self.strategy.restore(self.p.trades.today_trade_infos(self.instance_id))
            self.emit("INSTANCE_STARTED", f"started in {self.mode.upper()} mode",
                      params=self.strategy.settings.model_dump(mode="json"))
            last_timer: datetime | None = None
            while not self._stop:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                updates, self._trade_updates = self._trade_updates, []
                for info in updates:
                    await self.strategy.on_trade_update(info)
                now = self.now()
                if self.p.calendar.in_session_window(now):
                    ticks, self._ticks = list(self._ticks.values()), {}
                    for t in ticks:
                        await self.strategy.on_tick(t)
                    candles, self._candles = self._candles, []
                    for inst, c in candles:
                        await self.strategy.on_candle(inst, c)
                    if last_timer is None or (now - last_timer).total_seconds() >= 1.0:
                        last_timer = now
                        await self.strategy.on_timer(now)
                else:
                    self._ticks.clear()
                    self._candles.clear()
                if self._market_signature() != self._market_sig:
                    await self._resubscribe_market()
                self._refresh_status(now)
                if self.stop_after_trade and not self.has_open_position():
                    self.emit("INSTANCE_STOPPED", "current trade finished; stopping as requested")
                    asyncio.get_running_loop().create_task(self.p.strategies.finish_stop(self.instance_id))
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = repr(exc)
            self._set_status(ERROR, repr(exc))
            self.p.bus.exception("INSTANCE_ERROR", f"instance {self.name} crashed", exc, instance_id=self.instance_id,
                                 instance_name=self.name)
            self.p.bus.emit(Category.STRATEGY, "INSTANCE_ERROR",
                            f"{self.name} stopped with an error; open positions are still managed by the exit manager",
                            Level.CRITICAL, instance_id=self.instance_id, instance_name=self.name, error=repr(exc))
        finally:
            await self._unsubscribe_market()
            try:
                await self.strategy.on_stop()
            except Exception:
                pass

    # ------------------------------------------------------------------ market subscriptions
    def _market_signature(self) -> tuple[tuple[str, ...], tuple[int, ...]]:
        return (tuple(series_of(k) for k in self.strategy.subscriptions()),
                tuple(sorted(self.strategy.candle_timeframes())))

    async def _subscribe_market(self) -> None:
        for key in self.strategy.subscriptions():
            inst = await self.p.market.subscribe(key, self._on_tick)
            if inst is not None:
                self._series_inst[series_of(key)] = inst
            self._subscribed.append(key)
            for tf in self.strategy.candle_timeframes():
                self.p.market.add_candle_listener(key, tf, self._on_candle)
        self._market_sig = self._market_signature()

    async def _unsubscribe_market(self) -> None:
        for key in self._subscribed:
            try:
                await self.p.market.unsubscribe(key, self._on_tick)
                self.p.market.remove_candle_listener(key, self._on_candle)
            except Exception:
                pass
        self._subscribed.clear()
        self._series_inst.clear()
        self._ticks.clear()
        self._candles.clear()

    async def _resubscribe_market(self) -> None:
        """New settings changed what the strategy watches (e.g. NIFTY -> SENSEX). Switch the
        market data and restart the strategy's day for the new instrument; slots that already
        traded today stay done (restored from the stored trades). Only reached when idle."""
        old = ", ".join(k.label() for k in self._subscribed) or "-"
        await self._unsubscribe_market()
        await self._subscribe_market()
        await self.strategy.on_start()
        self.strategy.restore(self.p.trades.today_trade_infos(self.instance_id))
        new = ", ".join(k.label() for k in self._subscribed) or "-"
        self.emit("INSTRUMENTS_CHANGED", f"market data switched: {old} -> {new}; today's schedule rebuilt",
                  Level.WARNING, old=old, new=new)

    def _refresh_status(self, now: datetime) -> None:
        if self.status == ERROR:
            return
        if self.stop_after_trade:
            self._set_status(STOPPING, "finishing current trade")
            return
        if not self.p.calendar.is_trading_day(now.date()):
            self._set_status(WAITING, f"no trading today ({self.p.calendar.holiday_name(now.date()) or 'weekend'})")
            return
        if self.has_open_position():
            self._set_status(IN_TRADE)
            return
        phase = self.strategy.phase()
        self._set_status(RUNNING if phase in ("WATCHING", "IN_TRADE") else WAITING)

    def snapshot(self) -> dict[str, Any]:
        st: dict[str, Any] = {}
        try:
            st = self.strategy.status()
        except Exception as exc:
            st = {"error": repr(exc)}
        return {"id": self.instance_id, "name": self.name, "status": self.status, "message": self.status_message,
                "mode": self.mode, "account_id": self.account_id, "strategy": st,
                "pending_settings": self.strategy.has_pending_settings}


class StrategyManager:
    def __init__(self, platform: "Platform") -> None:
        self.p = platform
        self.runtimes: dict[int, InstanceRuntime] = {}
        platform.trades.on_trade_event = self._on_trade_event

    def _on_trade_event(self, instance_id: int, info: TradeInfo) -> None:
        rt = self.runtimes.get(instance_id)
        if rt is not None:
            rt.post_trade_update(info)

    def _row(self, instance_id: int) -> StrategyInstance | None:
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            if row is not None:
                s.expunge(row)
            return row

    async def start_enabled(self) -> None:
        from sqlalchemy import select

        with self.p.db.session() as s:
            ids = list(s.scalars(select(StrategyInstance.id).where(StrategyInstance.enabled.is_(True))))
        for iid in ids:
            await self.start_instance(iid)

    async def start_instance(self, instance_id: int) -> InstanceRuntime | None:
        row = self._row(instance_id)
        if row is None:
            return None
        old = self.runtimes.get(instance_id)
        if old is not None:
            await old.stop("restarting")
        try:
            rt = InstanceRuntime(self.p, row)
        except Exception as exc:
            self.p.bus.exception("INSTANCE_ERROR", f"could not create instance {row.name}", exc, instance_id=row.id)
            with self.p.db.session() as s:
                r = s.get(StrategyInstance, instance_id)
                r.status, r.status_message = ERROR, repr(exc)
            return None
        self.runtimes[instance_id] = rt
        rt.start()
        return rt

    async def enable(self, instance_id: int, by: str) -> None:
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            row.enabled, row.stop_after_trade = True, False
            name = row.name
        self.p.bus.emit(Category.STRATEGY, "INSTANCE_ENABLED", f"enabled by {by}", instance_id=instance_id,
                        instance_name=name, by=by)
        await self.start_instance(instance_id)

    async def disable(self, instance_id: int, by: str, on_open_position: str = "exit_now") -> str:
        """on_open_position: exit_now | finish_trade. Returns the resulting status."""
        rt = self.runtimes.get(instance_id)
        open_pos = self.p.trades.open_trade_count(instance_id=instance_id) > 0
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            name = row.name
            if open_pos and on_open_position == "finish_trade":
                row.stop_after_trade = True
            else:
                row.enabled, row.stop_after_trade = False, False
        if open_pos and on_open_position == "finish_trade":
            if rt is not None:
                rt.stop_after_trade = True
                rt._set_status(STOPPING, "finishing current trade")
                rt._wake.set()
            self.p.bus.emit(Category.STRATEGY, "INSTANCE_DISABLED", f"disable requested by {by}: finishing current trade",
                            instance_id=instance_id, instance_name=name, by=by, mode="finish_trade")
            return STOPPING
        if open_pos:
            await self.p.trades.exit_all("instance_disabled", instance_id=instance_id, by=by)
        if rt is not None:
            await rt.stop("disabled")
            self.runtimes.pop(instance_id, None)
        self.p.bus.emit(Category.STRATEGY, "INSTANCE_DISABLED", f"disabled by {by}", instance_id=instance_id,
                        instance_name=name, by=by, mode=on_open_position)
        self._set_db_status(instance_id, STOPPED, f"disabled by {by}")
        return STOPPED

    async def finish_stop(self, instance_id: int) -> None:
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            if row is not None:
                row.enabled, row.stop_after_trade = False, False
        rt = self.runtimes.pop(instance_id, None)
        if rt is not None:
            rt.stop_after_trade = False
            await rt.stop("stopped after trade")
        self._set_db_status(instance_id, STOPPED, "stopped after trade")

    def _set_db_status(self, instance_id: int, status: str, msg: str | None) -> None:
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            if row is not None:
                row.status, row.status_message = status, msg

    async def kill(self, instance_id: int, by: str) -> None:
        self.p.bus.emit(Category.RISK, "KILL_SWITCH", f"instance kill switch used by {by}", Level.CRITICAL,
                        instance_id=instance_id, by=by, scope="instance")
        await self.p.trades.exit_all("kill_switch", instance_id=instance_id, by=by)
        with self.p.db.session() as s:
            row = s.get(StrategyInstance, instance_id)
            if row is not None:
                row.enabled, row.stop_after_trade = False, False
        rt = self.runtimes.pop(instance_id, None)
        if rt is not None:
            await rt.stop("kill switch")
        self._set_db_status(instance_id, STOPPED, f"kill switch by {by}")

    async def update_params(self, instance_id: int, params: dict[str, Any]) -> bool:
        """Queue new parameters on a running instance (applied at its next idle moment).
        Returns True if applied immediately."""
        rt = self.runtimes.get(instance_id)
        if rt is None:
            return True
        new = rt.strategy.settings_model.model_validate(params)
        return rt.strategy.request_settings_update(new)

    def reload_limits(self, instance_id: int) -> None:
        rt = self.runtimes.get(instance_id)
        row = self._row(instance_id)
        if rt is not None and row is not None:
            rt.risk_limits = InstanceRiskLimits.model_validate(row.risk_limits or {})

    async def stop_all(self) -> None:
        for rt in list(self.runtimes.values()):
            await rt.stop("shutdown")
        self.runtimes.clear()

    def limits(self) -> dict[int, tuple[str, InstanceRiskLimits]]:
        return {iid: (rt.name, rt.risk_limits) for iid, rt in self.runtimes.items()}
