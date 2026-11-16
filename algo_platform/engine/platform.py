"""Platform: owns every engine component and exposes the operations the web app needs.

Data flow:  broker tick stream → MarketDataService (one subscription per symbol, candles) →
InstanceRuntime → Strategy → EntrySignal → TradeManager → RiskManager (instance/account/global)
→ OrderManager → broker adapter of the instance's own account. Order updates and positions
flow back through BrokerSync (stream + polling) into TradeManager and the UI.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from brokers.base import Exchange, InstrumentKey, Tick
from brokers.paper import PaperBroker
from core.clock import Clock, combine
from core.config import AppConfig
from core.events import Category, ConsoleSink, EventBus, JsonlFileSink, Level
from core.settings_schema import GlobalSettings
from engine.accounts import AccountRuntime
from engine.calendar import MarketCalendar
from engine.recorder import TickRecorder
from engine.charges import option_charges
from engine.exit_manager import ExitManager
from engine.market_data import FeedCandidate, MarketDataService, series_of
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager
from engine.strategy_manager import StrategyManager
from engine.trades import TradeManager
from engine.ui_hub import UiHub
from notify.telegram import TelegramNotifier
from storage.db import Database
from storage.event_sink import DbEventSink
from storage.models import BrokerAccount, StrategyInstance, Trade, User
from storage.repo import get_secret, holidays, load_global_settings

log = logging.getLogger("algo.platform")


class Platform:
    def __init__(self, config: AppConfig, db: Database, clock: Clock | None = None,
                 db_events: bool = True, file_events: bool = True, record_ticks: bool = True) -> None:
        self.config = config
        self.record_ticks = record_ticks
        self.db = db
        self.clock = clock or Clock()
        self.bus = EventBus()
        self.hub = UiHub()
        self.settings: GlobalSettings = load_global_settings(db)
        self.db_sink = DbEventSink(db) if db_events else None
        self.file_sink = JsonlFileSink(config.log_dir, self.settings.logging.retention_days) if file_events else None
        self.console_sink = ConsoleSink(Level(self.settings.logging.level))
        for sink in (self.db_sink, self.file_sink, self.console_sink, self.hub.event_sink):
            if sink is not None:
                self.bus.add_sink(sink)
        self.telegram = TelegramNotifier(lambda: self.settings.notifications, lambda: get_secret(db, "telegram_bot_token"))
        self.bus.add_sink(self.telegram)
        self.calendar = MarketCalendar(self.settings, holidays(db))
        self.market = MarketDataService(self.bus, self.clock, self.calendar, lambda: self.settings)
        self.market.candidates = self._feed_candidates
        self.recorder = TickRecorder(self.market, self.calendar, self.clock, self.bus, lambda: self.settings,
                                     config.data_dir / "recordings")
        self.orders = OrderManager(db, self.bus, self.clock)
        self.exits = ExitManager(self.clock, lambda: self.settings.sync.mtm_snapshot_sec)
        self.risk = RiskManager(self.bus, self.clock, lambda: self.settings)
        self.risk.on_global_square_off = self._global_breach_square_off
        self.accounts: dict[int, AccountRuntime] = {}
        self.trades = TradeManager(db, self.bus, self.clock, lambda: self.settings, self.market, self.orders, self.exits,
                                   self.risk, self.accounts.get)
        self.strategies = StrategyManager(self)
        self.market.add_raw_listener(self.exits.on_tick)
        self.market.add_raw_listener(self._paper_tick)
        self._tasks: list[asyncio.Task] = []
        self._paper_inst_cache: dict[tuple[int, str], Any] = {}
        self._day_flags: dict[str, date] = {}
        self._banners_reported: set[str] = set()
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.bus.emit(Category.SYSTEM, "APP_START", "platform starting")
        if not self.calendar.holidays:
            self.bus.emit(Category.SYSTEM, "HOLIDAYS_EMPTY", "NSE holiday list is empty — import it in Global Settings",
                          Level.WARNING)
        else:
            self.bus.emit(Category.SYSTEM, "HOLIDAYS_LOADED", f"{len(self.calendar.holidays)} holidays loaded",
                          count=len(self.calendar.holidays))
        self.trades.load_today()
        self.telegram.start()
        self.market.start()
        self.exits.start()
        if self.record_ticks:
            self.recorder.start()
        self.risk.start(self.strategies.limits, lambda: list(self.accounts.values()))
        with self.db.session() as s:
            rows = list(s.scalars(select(BrokerAccount).where(BrokerAccount.enabled.is_(True))))
            for r in rows:
                s.expunge(r)
        for row in rows:
            self._add_runtime(row)
        for acc in list(self.accounts.values()):
            self._tasks.append(asyncio.get_running_loop().create_task(self.connect_account(acc.id)))
        await self.strategies.start_enabled()
        self._tasks.append(asyncio.get_running_loop().create_task(self._scheduler(), name="scheduler"))
        self._tasks.append(asyncio.get_running_loop().create_task(self._publisher(), name="ui-publisher"))
        self.started = True

    async def stop(self) -> None:
        self.bus.emit(Category.SYSTEM, "APP_STOP", "platform stopping (open positions keep their broker-side stops)")
        for t in self._tasks:
            t.cancel()
        await self.strategies.stop_all()
        await self.exits.stop()
        await self.recorder.stop()
        await self.risk.stop()
        await self.market.stop()
        for acc in list(self.accounts.values()):
            await acc.close()
        await self.telegram.stop()
        if self.db_sink:
            self.db_sink.close()
        if self.file_sink:
            self.file_sink.close()
        self.started = False

    # ------------------------------------------------------------------ accounts
    def sync_poll_interval(self, sync: Any) -> float:
        """How often a broker account's order/trade/position backup poll runs (order updates also arrive
        on the broker's websocket; this is the safety net):
          market hours + an open live trade or working order -> poll_interval_sec (3 s)
          market hours, flat                                -> poll_interval_flat_sec (12 s)
          outside market hours, weekends, holidays          -> poll_interval_closed_sec (60 s)
        Simulated (paper) brokers are local and always use the fast interval."""
        s = self.settings.sync
        if getattr(sync.adapter, "is_paper", False):
            return s.poll_interval_sec
        now = self.clock.now()
        m = self.settings.market
        open_at = combine(now.date(), m.pre_open)
        close_at = combine(now.date(), m.market_close) + timedelta(minutes=5)
        if not (self.calendar.is_trading_day(now.date()) and open_at <= now <= close_at):
            return s.poll_interval_closed_sec
        busy = any(lt.account_id == sync.account_id and lt.mode == "live" for lt in list(self.trades.live.values()))
        busy = busy or any(o.status.is_working for o in list(sync.orders.values()))
        return s.poll_interval_sec if busy else s.poll_interval_flat_sec

    def _add_runtime(self, row: BrokerAccount) -> AccountRuntime | None:
        try:
            acc = AccountRuntime(row, self.db, self.bus, self.clock, lambda: self.settings.sync.poll_interval_sec,
                                 self.config.cache_dir, sync_interval=self.sync_poll_interval,
                                 rate_alert_sec=lambda: self.settings.sync.rate_limit_alert_sec)
        except Exception as exc:
            self.bus.exception("ACCOUNT_LOAD_FAILED", f"account {row.label}", exc, account_id=row.id)
            return None
        acc.price_source = lambda exch, tok, a=acc: self._paper_price(a, exch, tok)
        if isinstance(acc.adapter, PaperBroker):
            acc.adapter.price_source = acc.price_source
        for sync in acc.all_syncs():
            self._wire_sync(sync)
        original_shadow = acc.shadow

        def shadow_with_wiring() -> Any:
            had = acc.paper_sync is not None
            res = original_shadow()
            if not had and acc.paper_sync is not None:
                self._wire_sync(acc.paper_sync)
            return res

        acc.shadow = shadow_with_wiring  # type: ignore[method-assign]
        self.accounts[row.id] = acc
        return acc

    def _wire_sync(self, sync: Any) -> None:
        sync.order_listeners.append(self.trades.on_order)
        sync.positions_listeners.append(self.trades.on_positions)

    async def connect_account(self, account_id: int) -> bool:
        acc = self.accounts.get(account_id)
        if acc is None:
            return False
        acc.ready.clear()
        ok = await acc.connect()
        if not ok:
            return False
        await self.market.ensure_feed()
        self._refresh_paper_delegates()
        acc.start_sync()
        try:
            await self.trades.reconcile_account(acc)
        except Exception as exc:
            self.bus.exception("RECONCILE_FAILED", f"{acc.label}", exc, account_id=acc.id)
        acc.ready.set()
        return True

    async def reload_account(self, account_id: int) -> None:
        """(Re)create the runtime after the account was added/edited in the UI."""
        old = self.accounts.pop(account_id, None)
        if old is not None:
            if any(lt.account_id == account_id for lt in self.trades.live.values()):
                self.accounts[account_id] = old  # keep the running one; open positions depend on it
                raise RuntimeError("account has open positions; close them before editing credentials")
            await old.close()
        with self.db.session() as s:
            row = s.get(BrokerAccount, account_id)
            if row is None or not row.enabled:
                return
            s.expunge(row)
        acc = self._add_runtime(row)
        if acc is not None:
            asyncio.get_running_loop().create_task(self.connect_account(account_id))

    async def remove_account(self, account_id: int) -> None:
        acc = self.accounts.pop(account_id, None)
        if acc is not None:
            await acc.close()
        if self.market.feed_account_id == account_id:
            await self.market.ensure_feed()

    def _feed_candidates(self) -> list[FeedCandidate]:
        return [FeedCandidate(a.id, a.label, a.adapter, a.id == self.settings.general.data_feed_account_id)
                for a in self.accounts.values() if a.status == "CONNECTED"]

    def _refresh_paper_delegates(self) -> None:
        for acc in self.accounts.values():
            a = acc.adapter
            if isinstance(a, PaperBroker) and a.market is None and self.market.feed is not a:
                a.delegate = self.market.feed

    def _paper_price(self, acc: AccountRuntime, exchange: Exchange, token: str) -> float | None:
        brokers = [acc.paper_shadow, acc.adapter]
        for b in brokers:
            if b is None:
                continue
            inst = b.find_instrument(exchange, token)
            if inst is not None:
                return self.market.ltp(inst)
        return None

    def _paper_tick(self, series: str, key: InstrumentKey, tick: Tick) -> None:
        for acc in self.accounts.values():
            for pb in (acc.paper_shadow, acc.adapter if isinstance(acc.adapter, PaperBroker) else None):
                if pb is None or pb is self.market.feed or not pb.orders:
                    continue
                ck = (id(pb), series)
                inst = self._paper_inst_cache.get(ck)
                if inst is None:
                    try:
                        inst = pb.resolve_symbol(key)
                    except Exception:
                        continue
                    self._paper_inst_cache[ck] = inst
                pb.on_market_tick(Tick(exchange=inst.exchange, token=inst.token, ltp=tick.ltp, ts=tick.ts))

    async def test_connection(self, account_id: int) -> dict[str, Any]:
        acc = self.accounts.get(account_id)
        if acc is None:
            with self.db.session() as s:
                row = s.get(BrokerAccount, account_id)
                if row is None:
                    return {"ok": False, "message": "account not found"}
                s.expunge(row)
            acc = self._add_runtime(row)
            if acc is None:
                return {"ok": False, "message": "could not load account"}
        ok = await self.connect_account(account_id)
        if not ok:
            return {"ok": False, "message": acc.last_error or "login failed"}
        try:
            prof = await acc.adapter.get_profile()
            funds = await acc.adapter.get_funds()
        except Exception as exc:
            return {"ok": True, "message": f"logged in, but profile/funds failed: {exc}"}
        return {"ok": True, "message": f"connected as {prof.client_id} {prof.name}".strip(),
                "funds": funds.available_cash}

    # ------------------------------------------------------------------ kill switches
    async def kill_global(self, by: str) -> dict[str, int]:
        self.risk.global_kill = True
        self.bus.emit(Category.RISK, "KILL_SWITCH", f"GLOBAL kill switch activated by {by}", Level.CRITICAL,
                      scope="global", by=by)
        exited = await self.trades.exit_all("kill_switch", by=by)
        cancelled = flattened = 0
        for acc in self.accounts.values():
            if acc.status == "CONNECTED":
                cancelled += await self.trades.cancel_all_orders(acc, by)
                flattened += await self.trades.flatten_account(acc, by)
        for iid in list(self.strategies.runtimes):
            await self.strategies.kill(iid, by)
        with self.db.session() as s:
            for row in s.scalars(select(StrategyInstance).where(StrategyInstance.enabled.is_(True))):
                row.enabled = False
        return {"exited": exited, "cancelled": cancelled, "flattened": flattened}

    async def kill_account(self, account_id: int, by: str) -> dict[str, int]:
        acc = self.accounts.get(account_id)
        if acc is None:
            return {"exited": 0, "cancelled": 0, "flattened": 0}
        acc.kill_switch = True
        with self.db.session() as s:
            row = s.get(BrokerAccount, account_id)
            if row:
                row.kill_switch = True
            inst_ids = list(s.scalars(select(StrategyInstance.id).where(StrategyInstance.broker_account_id == account_id)))
        self.bus.emit(Category.RISK, "KILL_SWITCH", f"account kill switch on {acc.label} by {by}", Level.CRITICAL,
                      scope="account", account_id=account_id, account_label=acc.label, by=by)
        exited = await self.trades.exit_all("kill_switch", account_id=account_id, by=by)
        cancelled = await self.trades.cancel_all_orders(acc, by)
        flattened = await self.trades.flatten_account(acc, by)
        for iid in inst_ids:
            if iid in self.strategies.runtimes:
                await self.strategies.kill(iid, by)
        return {"exited": exited, "cancelled": cancelled, "flattened": flattened}

    async def reset_kill(self, by: str, account_id: int | None = None) -> None:
        if account_id is None:
            self.risk.global_kill = False
            self.bus.emit(Category.RISK, "KILL_SWITCH_RESET", f"global kill switch reset by {by}", Level.WARNING, by=by)
            return
        acc = self.accounts.get(account_id)
        if acc:
            acc.kill_switch = False
        with self.db.session() as s:
            row = s.get(BrokerAccount, account_id)
            if row:
                row.kill_switch = False
        self.bus.emit(Category.RISK, "KILL_SWITCH_RESET", f"account kill switch reset by {by}", Level.WARNING,
                      account_id=account_id, by=by)

    async def _global_breach_square_off(self, why: str) -> None:
        self.bus.emit(Category.RISK, "SQUARE_OFF_ALL", f"squaring off all positions: {why}", Level.CRITICAL)
        await self.trades.exit_all("kill_switch", by="risk")

    # ------------------------------------------------------------------ settings
    def apply_settings(self, new: GlobalSettings) -> None:
        self.settings = new
        self.calendar.update(settings=new)
        self.console_sink.min_level = Level(new.logging.level)
        if self.file_sink:
            self.file_sink.set_retention(new.logging.retention_days)
        if self.started:
            asyncio.get_running_loop().create_task(self.market.ensure_feed())

    def reload_holidays(self) -> None:
        self.calendar.update(holidays=holidays(self.db))
        self.bus.emit(Category.SYSTEM, "HOLIDAYS_LOADED", f"{len(self.calendar.holidays)} holidays loaded",
                      count=len(self.calendar.holidays))

    # ------------------------------------------------------------------ scheduler
    def _once(self, key: str, day: date) -> bool:
        if self._day_flags.get(key) == day:
            return False
        self._day_flags[key] = day
        return True

    async def _scheduler(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                await self._tick_schedule(self.clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler error")

    async def _tick_schedule(self, now: datetime) -> None:
        d, t, m = now.date(), now.time(), self.settings.market
        if self._once("day", d):
            self.trades.load_today()
            self.risk.reset_day()
            if not self.calendar.is_trading_day(d):
                name = self.calendar.holiday_name(d) or "weekend"
                self.bus.emit(Category.MARKET, "NO_TRADING_TODAY", f"no trading today: {name}", holiday=name)
        trading = self.calendar.is_trading_day(d)
        if trading and m.daily_relogin <= t < m.market_open and self._once("relogin", d):
            for acc in list(self.accounts.values()):
                if acc.auto_relogin and not acc.stub and not acc.is_paper_account:
                    asyncio.get_running_loop().create_task(self.connect_account(acc.id))
        for key, at, etype in (("pre_open", m.pre_open, "PRE_OPEN"), ("open", m.market_open, "MARKET_OPEN"),
                               ("square_off", m.final_square_off, "FINAL_SQUARE_OFF"),
                               ("close", m.market_close, "MARKET_CLOSE")):
            if trading and t >= at and (datetime.combine(d, t) - datetime.combine(d, at)) < timedelta(minutes=5) \
                    and self._once(key, d):
                self.bus.emit(Category.MARKET, etype, f"{etype.replace('_', ' ').lower()} {at:%H:%M}")
                if key == "square_off":
                    n = await self.trades.exit_all("square_off", by="schedule")
                    if n:
                        self.bus.emit(Category.EXIT, "SQUARE_OFF_DONE", f"{n} positions squared off")
        if self._once("purge", d) and self.db_sink is not None:
            try:
                n = await asyncio.to_thread(self.db_sink.purge_older_than, self.settings.logging.retention_days)
                if n:
                    self.bus.emit(Category.SYSTEM, "LOGS_PURGED", f"{n} events older than "
                                  f"{self.settings.logging.retention_days} days removed", Level.DEBUG)
            except Exception:
                log.exception("log purge failed")
        if self.calendar.is_market_open(now):
            stale_after = self.settings.sync.order_feed_stale_sec
            for acc in self.accounts.values():
                key = f"order_stale:{acc.id}"
                h = acc.live_sync.health
                stale = acc.status == "CONNECTED" and h.last_poll_at is not None and \
                    (now - h.last_poll_at).total_seconds() > stale_after
                if stale and key not in self._banners_reported:
                    self._banners_reported.add(key)
                    self.bus.emit(Category.SYSTEM, "ORDER_SYNC_STALE",
                                  f"{acc.label}: no successful order sync for {stale_after}s", Level.ERROR,
                                  account_id=acc.id, account_label=acc.label)
                elif not stale:
                    self._banners_reported.discard(key)

    async def _publisher(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                if self.hub.client_count:
                    self.hub.publish("state", self.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state publisher error")

    # ------------------------------------------------------------------ snapshot for the UI
    def banners(self) -> list[dict[str, str]]:
        out = []
        now = self.clock.now()
        if self.risk.global_kill:
            out.append({"level": "red", "text": "GLOBAL KILL SWITCH ACTIVE — no new entries"})
        if self.risk.global_blocked_reason:
            out.append({"level": "red", "text": f"New entries blocked: {self.risk.global_blocked_reason}"})
        if self.market.is_stale():
            out.append({"level": "red", "text": f"Market data feed is stale ({self.market.feed_label})"})
        if self.calendar.is_market_open(now) and self.market.feed is None and self.strategies.runtimes:
            out.append({"level": "red", "text": "No market data feed connected"})
        for acc in self.accounts.values():
            if f"order_stale:{acc.id}" in self._banners_reported:
                out.append({"level": "red", "text": f"{acc.label}: order sync is stale"})
            if acc.status == "ERROR":
                out.append({"level": "amber", "text": f"{acc.label}: {acc.last_error or 'connection error'}"})
            if acc.kill_switch:
                out.append({"level": "red", "text": f"{acc.label}: account kill switch active"})
        if not self.calendar.holidays:
            out.append({"level": "amber", "text": "NSE holiday list is empty — import it in Global Settings"})
        return out

    def day_summary(self, day: date | None = None) -> dict[str, Any]:
        """P&L per instance / account for one trading day; open positions count only for today."""
        today = self.clock.now().date()
        day = day or today
        with self.db.session() as s:
            rows = list(s.scalars(select(Trade).where(Trade.trade_date == day, Trade.status != "FAILED")))
        per_inst: dict[int, dict[str, float]] = {}
        per_acc: dict[int, dict[str, float]] = {}
        for r in rows:
            for bucket, k in ((per_inst, r.instance_id or 0), (per_acc, r.account_id or 0)):
                b = bucket.setdefault(k, {"trades": 0, "realized": 0.0, "net": 0.0, "unrealized": 0.0})
                b["trades"] += 1
                b["realized"] += r.gross_pnl or 0.0
                b["net"] += r.net_pnl or 0.0
        for lt in (self.trades.live.values() if day == today else ()):
            u = self.trades.unrealized(lt)
            ch = option_charges(self.settings.charges, lt.entry_price * lt.open_qty,
                                (self.market.ltp(lt.option_series) or lt.entry_price) * lt.open_qty).total
            for bucket, k in ((per_inst, lt.instance_id or 0), (per_acc, lt.account_id)):
                b = bucket.setdefault(k, {"trades": 0, "realized": 0.0, "net": 0.0, "unrealized": 0.0})
                b["unrealized"] += u
                b["net"] += u - ch
        for bucket in (per_inst, per_acc):
            for b in bucket.values():
                b["gross"] = round(b["realized"] + b["unrealized"], 2)
                for k in ("realized", "net", "unrealized"):
                    b[k] = round(b[k], 2)
        total = {"gross": round(sum(b["gross"] for b in per_acc.values()), 2),
                 "net": round(sum(b["net"] for b in per_acc.values()), 2),
                 "trades": sum(int(b["trades"]) for b in per_acc.values())}
        return {"instances": per_inst, "accounts": per_acc, "total": total}

    def external_positions(self) -> list[dict[str, Any]]:
        out = []
        for acc in self.accounts.values():
            for sync in acc.all_syncs():
                mine = {lt.token for lt in self.trades.live.values()
                        if lt.account_id == acc.id and (lt.mode == "paper") == sync.paper}
                for p in sync.positions:
                    if p.net_quantity != 0 and p.token not in mine:
                        out.append({"account": acc.label, "account_id": acc.id, "paper": sync.paper, "symbol": p.tradingsymbol,
                                    "qty": p.net_quantity, "avg": p.average_price, "ltp": p.ltp, "pnl": p.pnl})
        return out

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        summary = self.day_summary()
        stale = self.market.is_stale()
        with self.db.session() as s:
            rows = list(s.scalars(select(StrategyInstance).order_by(StrategyInstance.name)))
            acc_rows = list(s.scalars(select(BrokerAccount)))
            accounts = {a.id: a.label for a in acc_rows}
            acc_owner = {a.id: a.owner_user_id for a in acc_rows}
            names = {u.id: u.username for u in s.scalars(select(User))}
        instances = []
        for r in rows:
            rt = self.strategies.runtimes.get(r.id)
            b = summary["instances"].get(r.id, {})
            instances.append({
                "id": r.id, "name": r.name, "template": r.template_id, "mode": r.mode, "enabled": r.enabled,
                "status": rt.status if rt else r.status, "message": rt.status_message if rt else r.status_message,
                "account": accounts.get(r.broker_account_id or 0, "-"), "trades": b.get("trades", 0),
                "gross": b.get("gross", 0.0), "net": b.get("net", 0.0),
                "pending_settings": bool(rt and rt.strategy.has_pending_settings),
                "owner_id": r.owner_user_id, "owner": names.get(r.owner_user_id or 0, "-"),
            })
        accs = []
        for a in self.accounts.values():
            d = a.snapshot()
            d["indicator"] = a.indicator(stale and self.market.feed_account_id == a.id)
            d["is_feed"] = self.market.feed_account_id == a.id
            d["last_tick_at"] = self.market.last_tick_at.isoformat() if d["is_feed"] and self.market.last_tick_at else None
            d["pnl"] = summary["accounts"].get(a.id, {})
            d["owner_id"] = acc_owner.get(a.id)
            d["owner"] = names.get(acc_owner.get(a.id) or 0, "-")
            accs.append(d)
        return {
            "now": now.isoformat(), "market_open": self.calendar.is_market_open(now),
            "trading_day": self.calendar.is_trading_day(now.date()),
            "global_kill": self.risk.global_kill, "banners": self.banners(), "feed": self.market.status(),
            "instances": instances, "accounts": accs, "positions": self.trades.live_view(),
            "external_positions": self.external_positions(), "totals": summary["total"],
        }

    def instance_detail(self, instance_id: int) -> dict[str, Any] | None:
        rt = self.strategies.runtimes.get(instance_id)
        return rt.snapshot() if rt else None
