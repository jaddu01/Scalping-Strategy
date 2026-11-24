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


def test_sl_view_points_left_and_percent():
    from core.spot_sl import sl_view

    # 23-Nov slot 1: PE at spot 25644.40, SL 25694.70 (above) -> risk 50.30
    v = sl_view("PE_BUY", 25644.40, 25694.70, "above", 25669.55)
    assert (v["sl_risk"], v["sl_left"], v["sl_left_pct"], v["running"]) == (50.3, 25.15, 50, -25.15)
    assert sl_view("PE_BUY", 25644.40, 25694.70, "above", 25695.55)["sl_left"] == -0.85  # through the SL
    ce = sl_view("CE_BUY", 25324.30, 25297.20, "below", 25350.0)  # in profit: more than 100 % of the risk left
    assert (ce["sl_left"], ce["sl_left_pct"], ce["running"]) == (52.8, 195, 25.7)
    assert sl_view("CE_BUY", 100.0, 90.0, None, None) == {"sl": 90.0, "sl_side": "below", "sl_risk": 10.0,
                                                          "sl_left": None, "sl_left_pct": None, "running": None}
    assert sl_view("CE_BUY", 100.0, None, "below", 95.0) == {"sl": None}


def test_slot_view_sl_live_closed_and_trailed():
    base = {"direction": "PE_BUY", "signal_spot": 25443.75, "quantity": 650, "lot_size": 65, "entry_price": 50.45,
            "spot_sl": 25468.20, "spot_sl_side": "above"}
    live = slot_trade_view({**base, "status": "OPEN"}, {"spot": 25460.0, "premium": 42.0, "open_qty": 650})
    assert live["live"] and live["sl_left"] == 8.2 and live["sl_left_pct"] == 34 and live["running"] == -16.25
    trailed = slot_trade_view({**base, "status": "OPEN"}, {"spot": 25430.0, "premium": 60.0, "open_qty": 650,
                                                          "spot_sl": 25450.0, "spot_sl_side": "above"})
    assert trailed["sl"] == 25450.0 and trailed["sl_left"] == 20.0  # the live (moved) stop wins over the stored one
    closed = slot_trade_view({**base, "status": "CLOSED", "exit_reason": "spot_stop_loss",
                              "exit_trigger_spot": 25470.0, "gross_pnl": -8872.5})
    assert closed["sl_hit"] and closed["sl_left"] is None and closed["sl_risk"] == 24.45
    assert not slot_trade_view({**base, "status": "CLOSED", "exit_reason": "timed_exit"})["sl_hit"]


def test_slots_partial_shows_sl_left():
    from app.web.render import templates

    slot = {"no": 2, "ref_start": "13:30", "ref_end": "13:35", "entry_end": "15:15", "exit_at": "15:15",
            "state": "IN_TRADE", "ref_high": 25468.20, "ref_low": 25446.95, "long_trigger": 25470.45,
            "short_trigger": 25444.70, "direction": "PE_BUY", "trade_id": "T000952"}
    done = {**slot, "no": 1, "state": "DONE", "trade_id": "T000900", "result": "spot_stop_loss"}
    base = {"direction": "PE_BUY", "quantity": 650, "lot_size": 65, "spot_sl_side": "above"}
    views = {"T000952": slot_trade_view({**base, "signal_spot": 25443.75, "entry_price": 50.45, "spot_sl": 25468.20,
                                         "status": "OPEN"}, {"spot": 25460.0, "premium": 42.0, "open_qty": 650}),
             "T000900": slot_trade_view({**base, "signal_spot": 25644.40, "entry_price": 109.55, "spot_sl": 25694.70,
                                         "status": "CLOSED", "exit_reason": "spot_stop_loss",
                                         "exit_trigger_spot": 25695.55, "gross_pnl": -16217.5})}
    html = templates.get_template("partials/slots.html").render(
        rt={"strategy": {"slots": [done, slot]}}, views=views, totals={"points": 0, "pnl": 0, "trades": 2})
    assert ">SL</th>" in html and "25,468.20" in html
    assert "<b>8.20</b> pts left · 34%" in html and "running -16.25" in html and 'class="small nowrap neg"' in html
    assert '<span class="badge red">hit</span>' in html  # the closed slot-1 trade


def test_closed_day_note_and_reason():
    from datetime import date

    from app.web.render import templates
    from core.settings_schema import GlobalSettings
    from engine.calendar import MarketCalendar

    cal = MarketCalendar(GlobalSettings(), {date(2026, 11, 24): "", date(2026, 11, 25): "Guru Nanak Jayanti"})
    assert cal.closed_reason(date(2026, 11, 24)) == "holiday"  # unnamed holiday: no longer "weekend"
    assert cal.closed_reason(date(2026, 11, 25)) == "Guru Nanak Jayanti"
    assert cal.closed_reason(date(2026, 11, 28)) == "weekend"
    html = templates.get_template("partials/slots.html").render(
        rt=None, views={}, totals={}, closed={"reason": "holiday", "day": date(2026, 11, 23)})
    assert "No trading today (holiday). Showing the last trading day, <b>Mon 23 Nov</b>." in html
