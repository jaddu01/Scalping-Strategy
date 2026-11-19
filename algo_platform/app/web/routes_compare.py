"""Strategy comparison: one row per strategy instance, side by side, for a chosen period."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.auth.access import owner_choices, owner_filter, usernames
from app.auth.deps import SessionInfo, current_user
from app.web.render import render
from app.web.shared import lots_of
from storage.models import BrokerAccount, StrategyInstance, Trade

router = APIRouter()

SL_REASONS = {"spot_stop_loss", "protective_sl", "trailing_stop"}
TP_REASONS = {"take_profit", "tp1", "tp2", "tp3", "tp4", "tp5"}
TIME_REASONS = {"timed_exit", "square_off"}
PERIODS = {"today": 0, "7d": 6, "30d": 29, "90d": 89}
TEMPLATE_SHORT = {"chandelier_exit": "Chandelier", "smc_playbook": "SMC", "nifty_ref_breakout": "Original", "nifty_early_breakout": "Early-Slot",
                  "nifty_ref_breakout_tp": "Original + TP", "nifty_early_breakout_tp": "Early-Slot + TP",
                  "nifty_two_entry": "2-Entry", "nifty_two_entry_tp": "2-Entry + TP", "ema_rsi_breakout": "EMA + RSI"}


@dataclass
class Row:
    id: int
    name: str
    template: str
    mode: str
    broker: str
    enabled: bool
    status: str
    lots: Any
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross: float = 0.0
    charges: float = 0.0
    net: float = 0.0
    win_sum: float = 0.0
    loss_sum: float = 0.0
    best: float | None = None
    worst: float | None = None
    max_dd: float = 0.0
    exits_sl: int = 0
    exits_tp: int = 0
    exits_time: int = 0
    exits_other: int = 0
    tp_hits: int = 0
    open_qty: int = 0
    unrealized: float = 0.0
    owner: str = "–"
    _curve: list[float] = field(default_factory=list, repr=False)

    @property
    def win_pct(self) -> float | None:
        return round(100 * self.wins / self.trades, 1) if self.trades else None

    @property
    def avg_net(self) -> float | None:
        return round(self.net / self.trades, 2) if self.trades else None

    @property
    def avg_win(self) -> float | None:
        return round(self.win_sum / self.wins, 2) if self.wins else None

    @property
    def avg_loss(self) -> float | None:
        return round(self.loss_sum / self.losses, 2) if self.losses else None

    @property
    def profit_factor(self) -> float | None:
        if not self.losses:
            return None
        return round(self.win_sum / abs(self.loss_sum), 2) if self.loss_sum else None


def template_label(template_id: str, params: dict[str, Any] | None) -> str:
    label = TEMPLATE_SHORT.get(template_id, template_id)
    if template_id.endswith("_tp") and (params or {}).get("tp_enabled") is False:
        label += " (TP off)"
    return label


def compare_rows(instances: list[StrategyInstance], trades: list[Trade], accounts: dict[int, str],
                 runtime_status: dict[int, str], open_positions: dict[int, tuple[int, float]]) -> list[Row]:
    """Pure aggregation (unit tested). Trades are counted on their closing P&L (net after est. charges)."""
    rows: dict[int, Row] = {}
    for i in instances:
        rows[i.id] = Row(id=i.id, name=i.name, template=template_label(i.template_id, i.params), mode=i.mode,
                         broker=accounts.get(i.broker_account_id or 0, "–"), enabled=i.enabled,
                         status=runtime_status.get(i.id, i.status or "STOPPED"), lots=lots_of(i.params))
    closed = sorted((t for t in trades if t.status == "CLOSED" and t.instance_id in rows),
                    key=lambda t: (t.exit_time or t.entry_time or t.signal_time, t.id))
    for t in closed:
        r = rows[t.instance_id]  # type: ignore[index]
        gross, net = t.gross_pnl or 0.0, (t.net_pnl if t.net_pnl is not None else (t.gross_pnl or 0.0))
        r.trades += 1
        r.gross += gross
        r.charges += t.charges or 0.0
        r.net += net
        if net > 0:
            r.wins += 1
            r.win_sum += net
        elif net < 0:
            r.losses += 1
            r.loss_sum += net
        r.best = net if r.best is None else max(r.best, net)
        r.worst = net if r.worst is None else min(r.worst, net)
        reason = t.exit_reason or ""
        if reason in SL_REASONS:
            r.exits_sl += 1
        elif reason in TP_REASONS:
            r.exits_tp += 1
        elif reason in TIME_REASONS:
            r.exits_time += 1
        else:
            r.exits_other += 1
        r.tp_hits += sum(1 for p in ((t.meta or {}).get("partials") or []) if p.get("filled"))
        r._curve.append(r.net)
    for r in rows.values():
        peak = 0.0
        for v in r._curve:
            peak = max(peak, v)
            r.max_dd = max(r.max_dd, peak - v)
        r.gross, r.charges, r.net = round(r.gross, 2), round(r.charges, 2), round(r.net, 2)
        r.max_dd = round(r.max_dd, 2)
        if r.id in open_positions:
            r.open_qty, r.unrealized = open_positions[r.id][0], round(open_positions[r.id][1], 2)
    return sorted(rows.values(), key=lambda r: (-r.net, r.name))


def _period(request: Request) -> tuple[str, date, date]:
    q = request.query_params
    today = date.today()
    period = q.get("period") or "today"
    if period == "custom":
        try:
            return period, date.fromisoformat(q.get("day_from") or ""), date.fromisoformat(q.get("day_to") or "")
        except ValueError:
            period = "today"
    if period == "all":
        return period, date(2000, 1, 1), today
    return (period if period in PERIODS else "today"), today - timedelta(days=PERIODS.get(period, 0)), today


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    period, d_from, d_to = _period(request)
    show_all = request.query_params.get("show") == "all"
    mode = request.query_params.get("mode") or ""
    owner_val, owner_id = owner_filter(request, user)
    db = request.app.state.db
    with db.session() as s:
        stmt = select(StrategyInstance)
        if not show_all:
            stmt = stmt.where(StrategyInstance.enabled.is_(True))
        if mode in ("paper", "live"):
            stmt = stmt.where(StrategyInstance.mode == mode)
        if owner_id is not None:
            stmt = stmt.where(StrategyInstance.owner_user_id == owner_id)
        instances = list(s.scalars(stmt))
        ids = [i.id for i in instances]
        trades = list(s.scalars(select(Trade).where(Trade.trade_date >= d_from, Trade.trade_date <= d_to,
                                                    Trade.instance_id.in_(ids)))) if ids else []
        accounts = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
        for obj in instances + trades:
            s.expunge(obj)
    p = request.app.state.platform
    status: dict[int, str] = {}
    open_pos: dict[int, tuple[int, float]] = {}
    if p is not None:
        status = {iid: rt.status for iid, rt in p.strategies.runtimes.items()}
        for lt in list(p.trades.live.values()):
            if lt.instance_id is None:
                continue
            q, u = open_pos.get(lt.instance_id, (0, 0.0))
            open_pos[lt.instance_id] = (q + lt.open_qty, u + p.trades.open_pnl(lt))
    rows = compare_rows(instances, trades, accounts, status, open_pos)
    names = usernames(db)
    owner_of = {i.id: i.owner_user_id for i in instances}
    for r in rows:
        r.owner = names.get(owner_of.get(r.id) or 0, "–")
    traded = [r for r in rows if r.trades]
    totals = {"trades": sum(r.trades for r in rows), "gross": round(sum(r.gross for r in rows), 2),
              "charges": round(sum(r.charges for r in rows), 2), "net": round(sum(r.net for r in rows), 2),
              "unrealized": round(sum(r.unrealized for r in rows), 2), "open_qty": sum(r.open_qty for r in rows),
              "wins": sum(r.wins for r in rows)}
    best_id = max(traded, key=lambda r: r.net).id if len(traded) > 1 else None
    return render(request, "compare.html", {"rows": rows, "totals": totals, "period": period, "d_from": d_from,
                                            "d_to": d_to, "show_all": show_all, "mode": mode, "best_id": best_id,
                                            "owner": owner_val, "owners": owner_choices(db)})
