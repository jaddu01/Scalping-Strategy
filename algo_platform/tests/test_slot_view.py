"""Today's slots table: entry / exit spot, points and P&L per slot."""
from __future__ import annotations

from app.web.slot_view import slot_trade_view


def test_ce_points_and_pnl():
    v = slot_trade_view({"direction": "CE_BUY", "signal_spot": 24943.70, "exit_trigger_spot": 24945.95,
                         "quantity": 325, "lot_size": 65, "entry_price": 102.4, "exit_price": 104.1,
                         "gross_pnl": 552.5, "net_pnl": 400.0, "status": "CLOSED"})
    assert v["entry_spot"] == 24943.70 and v["exit_spot"] == 24945.95
    assert v["points"] == 2.25 and v["pnl"] == 552.5 and v["legs"] == [] and not v["live"]


def test_pe_points_are_entry_minus_exit():
    v = slot_trade_view({"direction": "PE_BUY", "signal_spot": 24900.0, "exit_trigger_spot": 24912.5,
                         "quantity": 325, "lot_size": 65, "status": "CLOSED", "gross_pnl": -900.0})
    assert v["points"] == -12.5


def test_tp_legs_weighted_points():
    v = slot_trade_view({"direction": "CE_BUY", "signal_spot": 100.0, "exit_trigger_spot": 104.0, "quantity": 325,
                         "lot_size": 65, "entry_price": 50.0, "exit_price": 56.0, "gross_pnl": 1950.0,
                         "status": "CLOSED", "partials": [
                             {"label": "tp1", "filled": 130, "price": 60.0, "spot": 120.0},
                             {"label": "tp2", "filled": 130, "price": 65.0, "spot": 130.0}]})
    assert [(x["label"], x["lots"], x["points"]) for x in v["legs"]] == [("TP1", 2, 20.0), ("TP2", 2, 30.0)]
    assert v["points"] == round((20 * 130 + 30 * 130 + 4 * 65) / 325, 2)  # 20.8


def test_open_trade_uses_live_prices():
    v = slot_trade_view({"direction": "CE_BUY", "signal_spot": 100.0, "quantity": 325, "lot_size": 65,
                         "entry_price": 50.0, "status": "OPEN",
                         "partials": [{"label": "tp1", "filled": 130, "price": 60.0, "spot": 110.0}]},
                        {"spot": 108.0, "premium": 58.0, "open_qty": 195})
    assert v["live"] and v["exit_spot"] == 108.0
    assert v["pnl"] == 10 * 130 + 8 * 195
    assert v["points"] == round((10 * 130 + 8 * 195) / 325, 2)


def test_no_trade_and_missing_exit_spot():
    assert slot_trade_view(None) == {}
    v = slot_trade_view({"direction": "CE_BUY", "signal_spot": 100.0, "quantity": 65, "status": "CLOSED"})
    assert v["points"] is None  # e.g. a manual exit at the broker: no exit spot recorded


def test_slots_partial_renders():
    from app.web.render import templates

    slot = {"no": 2, "ref_start": "10:00", "ref_end": "10:05", "entry_end": "10:14:58", "exit_at": "10:14:58",
            "state": "DONE", "ref_high": 24943.70, "ref_low": 24919.60, "long_trigger": 24945.95,
            "short_trigger": 24917.35, "direction": "CE_BUY", "trade_id": "T000123", "result": "timed_exit"}
    tp = {**slot, "no": 3, "trade_id": "T000124"}
    views = {"T000123": slot_trade_view({"direction": "CE_BUY", "signal_spot": 24946.10, "exit_trigger_spot": 24947.60,
                                         "quantity": 325, "lot_size": 65, "entry_price": 102.4, "exit_price": 104.1,
                                         "gross_pnl": 552.5, "status": "CLOSED"}),
             "T000124": slot_trade_view({"direction": "CE_BUY", "signal_spot": 100.0, "exit_trigger_spot": 104.0,
                                         "quantity": 325, "lot_size": 65, "entry_price": 50.0, "exit_price": 56.0,
                                         "gross_pnl": 1950.0, "status": "CLOSED", "partials": [
                                             {"label": "tp1", "filled": 130, "price": 60.0, "spot": 120.0}]})}
    html = templates.get_template("partials/slots.html").render(
        rt={"strategy": {"slots": [slot, tp, {**slot, "no": 4, "trade_id": None, "direction": None,
                                              "result": "no_breakout"}]}},
        views=views, totals={"points": 1.5, "pnl": 552.5, "trades": 2})
    assert "10:00–10:05" in html and "10:05–10:14:58" in html
    assert "24,946.10" in html and "24,947.60" in html and "+1.50" in html
    assert "102.40 → 104.10" in html and "552.50" in html
    assert "TP1 2 lots @ spot 120.00 (+20.00 pts), premium 60.00" in html
    assert "Today: 2 trades" in html and "no_breakout" in html
