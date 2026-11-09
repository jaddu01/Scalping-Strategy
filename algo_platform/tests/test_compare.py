"""Strategy comparison table: aggregation per instance."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.web.routes_compare import compare_rows, template_label
from storage.models import StrategyInstance, Trade

T0 = datetime(2026, 11, 9, 9, 50)


def inst(i, name, template="nifty_ref_breakout", params=None, enabled=True):
    return StrategyInstance(id=i, name=name, template_id=template, params=params or {"lots": 5}, mode="paper",
                            enabled=enabled, broker_account_id=1, status="STOPPED")


def trade(i, inst_id, net, reason, minutes, partials=None, status="CLOSED"):
    return Trade(id=i, instance_id=inst_id, status=status, gross_pnl=net + 50, charges=50, net_pnl=net,
                 exit_reason=reason, exit_time=T0 + timedelta(minutes=minutes), meta={"partials": partials or []})


def test_compare_rows():
    instances = [inst(1, "ORIG"), inst(2, "EARLY TP", "nifty_early_breakout_tp", {"lots": 10}), inst(3, "IDLE")]
    trades = [
        trade(1, 1, 1000, "timed_exit", 10), trade(2, 1, -400, "spot_stop_loss", 20),
        trade(3, 1, -800, "spot_stop_loss", 30), trade(4, 1, 600, "timed_exit", 40),
        trade(5, 2, 3000, "timed_exit", 15, [{"label": "tp1", "filled": 260}, {"label": "tp2", "filled": 260}]),
        trade(6, 2, 500, "tp2", 25, [{"label": "tp1", "filled": 260}, {"label": "tp2", "filled": 0}]),
        trade(7, 2, -9999, "timed_exit", 35, status="OPEN"),  # open trades are not counted
    ]
    rows = compare_rows(instances, trades, {1: "Angel A/c 1"}, {1: "RUNNING"}, {2: (130, 450.0)})
    assert [r.name for r in rows] == ["EARLY TP", "ORIG", "IDLE"]  # sorted by net
    early, orig, idle = rows
    assert (orig.trades, orig.wins, orig.losses, orig.win_pct) == (4, 2, 2, 50.0)
    assert orig.net == 400 and orig.gross == 600 and orig.charges == 200
    assert orig.avg_net == 100 and orig.avg_win == 800 and orig.avg_loss == -600
    assert orig.profit_factor == pytest.approx(1600 / 1200, rel=1e-2)
    assert orig.best == 1000 and orig.worst == -800
    assert orig.max_dd == 1200  # +1000 -> -200
    assert (orig.exits_sl, orig.exits_tp, orig.exits_time) == (2, 0, 2)
    assert orig.status == "RUNNING" and orig.broker == "Angel A/c 1" and orig.lots == 5
    assert early.trades == 2 and early.tp_hits == 3 and early.exits_tp == 1 and early.max_dd == 0
    assert early.profit_factor is None and early.open_qty == 130 and early.unrealized == 450
    assert idle.trades == 0 and idle.win_pct is None and idle.avg_net is None


def test_template_label():
    assert template_label("nifty_early_breakout", {}) == "Early-Slot"
    assert template_label("nifty_ref_breakout_tp", {"tp_enabled": False}) == "Original + TP (TP off)"
