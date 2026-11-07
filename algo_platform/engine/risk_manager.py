"""Pre-trade risk checks at three levels (instance, broker account, global) and a monitor that
warns on approaching limits and acts on breaches."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

from core.clock import Clock
from core.events import Category, EventBus, Level
from core.settings_schema import BreachAction, GlobalSettings, InstanceRiskLimits

if TYPE_CHECKING:
    from engine.accounts import AccountRuntime
    from engine.trades import TradeManager

log = logging.getLogger("algo.risk")


@dataclass
class RiskDecision:
    ok: bool
    level: str = ""
    reason: str = ""


OK = RiskDecision(True)


class RiskManager:
    def __init__(self, bus: EventBus, clock: Clock, settings: Callable[[], GlobalSettings]) -> None:
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.trades: "TradeManager | None" = None
        self.global_kill = False
        self.global_blocked_reason: str | None = None
        self.blocked_instances: dict[int, str] = {}
        self.blocked_accounts: dict[int, str] = {}
        self._warned: set[tuple[str, Any, str, date]] = set()
        self._breached: set[tuple[str, Any, str, date]] = set()
        self._task: asyncio.Task | None = None
        self.on_global_square_off: Callable[[str], Any] | None = None

    def reset_day(self) -> None:
        self.global_blocked_reason = None
        self.blocked_instances.clear()
        self.blocked_accounts.clear()

    # ------------------------------------------------------------------ pre-trade
    def check_entry(self, *, instance_id: int, limits: InstanceRiskLimits, account: "AccountRuntime", mode: str,
                    lots: int, quantity: int, premium: float | None) -> RiskDecision:
        assert self.trades is not None
        g = self.settings().risk
        today = self.clock.now().date()
        # ---- global
        if self.global_kill:
            return RiskDecision(False, "global", "global kill switch is active")
        if self.global_blocked_reason:
            return RiskDecision(False, "global", self.global_blocked_reason)
        if self.trades.open_trade_count() >= g.max_open_positions:
            return RiskDecision(False, "global", f"max open positions ({g.max_open_positions}) reached")
        gp = self.trades.daily_pnl(today)
        if g.max_daily_loss and gp <= -g.max_daily_loss:
            return RiskDecision(False, "global", f"global daily loss limit {g.max_daily_loss:.0f} reached ({gp:.2f})")
        if g.max_daily_profit and gp >= g.max_daily_profit:
            return RiskDecision(False, "global", f"global daily profit target {g.max_daily_profit:.0f} reached")
        # ---- account
        if account.kill_switch:
            return RiskDecision(False, "account", f"kill switch active on {account.label}")
        if account.id in self.blocked_accounts:
            return RiskDecision(False, "account", self.blocked_accounts[account.id])
        if account.status != "CONNECTED":
            return RiskDecision(False, "account", f"{account.label} is {account.status}")
        ar = account.risk
        if self.trades.open_trade_count(account_id=account.id) >= ar.max_open_positions:
            return RiskDecision(False, "account", f"account max open positions ({ar.max_open_positions}) reached")
        ap = self.trades.daily_pnl(today, account_id=account.id)
        if ar.max_daily_loss and ap <= -ar.max_daily_loss:
            return RiskDecision(False, "account", f"account daily loss limit {ar.max_daily_loss:.0f} reached")
        if self.trades.orders_today(account.id) >= ar.max_orders_per_day:
            return RiskDecision(False, "account", f"account max orders/day ({ar.max_orders_per_day}) reached")
        # ---- instance
        if instance_id in self.blocked_instances:
            return RiskDecision(False, "instance", self.blocked_instances[instance_id])
        if self.trades.open_trade_count(instance_id=instance_id) >= 1:
            return RiskDecision(False, "instance", "already_in_position")
        if limits.max_trades_per_day and self.trades.trades_today(instance_id) >= limits.max_trades_per_day:
            return RiskDecision(False, "instance", f"max trades/day ({limits.max_trades_per_day}) reached")
        if lots > limits.max_lots_per_order:
            return RiskDecision(False, "instance", f"{lots} lots exceeds max {limits.max_lots_per_order} per order")
        ip = self.trades.daily_pnl(today, instance_id=instance_id)
        if limits.max_daily_loss and ip <= -limits.max_daily_loss:
            return RiskDecision(False, "instance", f"instance daily loss limit {limits.max_daily_loss:.0f} reached")
        if limits.max_daily_profit and ip >= limits.max_daily_profit:
            return RiskDecision(False, "instance", f"instance daily profit target {limits.max_daily_profit:.0f} reached")
        if limits.max_premium_per_trade and premium and premium * quantity > limits.max_premium_per_trade:
            return RiskDecision(False, "instance", f"premium outlay {premium * quantity:.0f} exceeds "
                                                   f"{limits.max_premium_per_trade:.0f}")
        return OK

    # ------------------------------------------------------------------ monitoring
    def start(self, instance_limits: Callable[[], dict[int, tuple[str, InstanceRiskLimits]]],
              accounts: Callable[[], list["AccountRuntime"]]) -> None:
        self._instance_limits = instance_limits
        self._accounts = accounts
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="risk-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(2)
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("risk monitor error")

    def _warn_or_breach(self, scope: str, scope_id: Any, kind: str, value: float, limit: float, label: str,
                        **kw: Any) -> bool:
        """Returns True on a (new) breach."""
        if not limit:
            return False
        today = self.clock.now().date()
        g = self.settings().risk
        key = (scope, scope_id, kind, today)
        metric = -value if kind == "loss" else value
        if metric >= limit:
            if key not in self._breached:
                self._breached.add(key)
                self.bus.emit(Category.RISK, "RISK_BREACH",
                              f"{scope} {label}: daily {kind} {value:.2f} hit limit {limit:.2f}", Level.CRITICAL,
                              scope=scope, value=value, limit=limit, kind=kind, **kw)
                return True
            return False
        if metric >= limit * g.warn_at_pct / 100 and key not in self._warned:
            self._warned.add(key)
            self.bus.emit(Category.RISK, "RISK_WARNING",
                          f"{scope} {label}: daily {kind} {value:.2f} is {g.warn_at_pct:.0f}% of limit {limit:.2f}",
                          Level.WARNING, scope=scope, value=value, limit=limit, kind=kind, **kw)
        return False

    async def evaluate(self) -> None:
        if self.trades is None:
            return
        today = self.clock.now().date()
        g = self.settings().risk
        gp = self.trades.daily_pnl(today)
        for kind, limit in (("loss", g.max_daily_loss), ("profit", g.max_daily_profit)):
            if self._warn_or_breach("global", None, kind, gp, limit, "all instances"):
                self.global_blocked_reason = f"global daily {kind} limit reached"
                if g.on_breach is BreachAction.SQUARE_OFF and self.on_global_square_off is not None:
                    await self.on_global_square_off(f"global daily {kind} limit")
        for acc in self._accounts():
            if acc.risk.max_daily_loss:
                ap = self.trades.daily_pnl(today, account_id=acc.id)
                if self._warn_or_breach("account", acc.id, "loss", ap, acc.risk.max_daily_loss, acc.label,
                                        account_id=acc.id, account_label=acc.label):
                    self.blocked_accounts[acc.id] = f"account daily loss limit reached on {acc.label}"
        for iid, (name, lim) in self._instance_limits().items():
            ip = self.trades.daily_pnl(today, instance_id=iid)
            for kind, limit in (("loss", lim.max_daily_loss), ("profit", lim.max_daily_profit)):
                if self._warn_or_breach("instance", iid, kind, ip, limit, name, instance_id=iid, instance_name=name):
                    self.blocked_instances[iid] = f"instance daily {kind} limit reached"
