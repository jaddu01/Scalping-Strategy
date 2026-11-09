"""Web app smoke test with the engine running: create a paper account and an instance through the
forms, toggle it via the API, render every page, run a CSV backtest, export/import YAML, and
receive live state over the websocket."""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.config import AppConfig
from storage.crypto import Crypto
from storage.models import BrokerAccount, StrategyInstance
from tests.test_auth import PW, csrf_from, login, setup_admin


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'web.db'}",
                    master_key=Crypto.generate_key())
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()
    with TestClient(create_app(cfg, start_engine=True), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        login(c)
        yield c


def tok(c: TestClient) -> str:
    return csrf_from(c.get("/").text)


def test_full_ui_flow(client):
    t = tok(client)
    # --- broker account (paper, simulated market) via the schema-driven form
    r = client.post("/accounts/new?broker=paper", data={
        "csrf_token": t, "label": "Paper Sim", "enabled": "on", "auto_relogin": "on",
        "cred.starting_capital": "500000", "cred.slippage_ticks": "0", "cred.simulate_market": "on",
        "cred.sim_spot_start": "24000", "cred.sim_points_per_tick": "1.2", "cred.sim_iv": "0.14",
        "cred.sim_lot_size": "65", "cred.sim_tick_interval_sec": "0.5",
        "risk.max_daily_loss": "0", "risk.max_open_positions": "5", "risk.max_orders_per_day": "500"},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    with client.app.state.db.session() as s:
        acc = s.query(BrokerAccount).one()
        assert acc.credentials_enc and "starting_capital" not in acc.credentials_enc  # encrypted
        aid = acc.id
    # Angel One account: secrets are masked and blank secret fields keep the saved value
    r = client.post("/accounts/new?broker=angelone", data={
        "csrf_token": t, "label": "Angel 1", "cred.client_id": "A12345", "cred.api_key": "KEY123456",
        "cred.pin": "1234", "cred.totp_secret": "JBSWY3DPEHPK3PXP", "risk.max_open_positions": "5",
        "risk.max_daily_loss": "0", "risk.max_orders_per_day": "500"}, follow_redirects=False)
    assert r.status_code == 303
    with client.app.state.db.session() as s:
        angel = s.query(BrokerAccount).filter_by(label="Angel 1").one()
        angel_id = angel.id
    page = client.get(f"/accounts/{angel_id}/edit").text
    assert "KEY123456" not in page and "JBSWY3DPEHPK3PXP" not in page and "saved — leave blank" in page
    r = client.post(f"/accounts/{angel_id}/edit", data={
        "csrf_token": t, "label": "Angel 1", "cred.client_id": "A12345", "cred.api_key": "", "cred.pin": "",
        "cred.totp_secret": "", "risk.max_open_positions": "5", "risk.max_daily_loss": "0",
        "risk.max_orders_per_day": "500"}, follow_redirects=False)
    assert r.status_code == 303
    from storage.repo import account_credentials
    with client.app.state.db.session() as s:
        creds = account_credentials(s.get(BrokerAccount, angel_id))
    assert creds["api_key"] == "KEY123456" and creds["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert "Angel One" in client.get("/accounts").text
    client.post(f"/accounts/{angel_id}/delete", data={"csrf_token": t})

    # --- strategy instance via the auto-generated form, with an inline validation error first
    form = {"csrf_token": t, "name": "NIFTY Breakout – Paper", "broker_account_id": str(aid), "mode": "paper",
            "notes": "", "params.underlying": "NIFTY",
            "params.reference_candle_starts": "09:45, 10:00, 10:15, 10:45, 11:15, 11:45, 12:10, 12:45, 13:15, 13:45, 14:10, 14:35, 14:55",
            "params.candle_timeframe_min": "5", "params.buffer_points": "-1", "params.entry_window_candles": "2",
            "params.exit_offset_min": "15", "params.lots": "5", "params.strike_step": "50",
            "params.expiry": "nearest_weekly", "params.entry_trigger": "tick", "params.entry_order_type": "MARKET",
            "params.limit_slippage_pct": "1", "params.protective_sl_pct": "40", "params.take_profit_mode": "none",
            "params.take_profit_value": "0", "params.candle_finalization_delay_sec": "5",
            "risk.max_daily_loss": "0", "risk.max_daily_profit": "0", "risk.max_trades_per_day": "13",
            "risk.max_lots_per_order": "20", "risk.max_premium_per_trade": "0"}
    r = client.post("/strategies/new?template=nifty_ref_breakout", data=form)
    assert r.status_code == 400 and "greater than or equal to 0" in r.text
    form["params.buffer_points"] = "2.25"
    r = client.post("/strategies/new?template=nifty_ref_breakout", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    with client.app.state.db.session() as s:
        inst = s.query(StrategyInstance).one()
        iid = inst.id
        assert inst.params["buffer_points"] == 2.25 and inst.params["lots"] == 5
        assert inst.params["reference_candle_starts"][0] == "09:45" and not inst.enabled
    # live mode without confirmation is refused
    r = client.post(f"/strategies/{iid}/edit", data={**form, "mode": "live"})
    assert r.status_code == 400

    # --- toggle on/off via the JSON API (CSRF header)
    h = {"X-CSRF-Token": t}
    r = client.post(f"/api/instances/{iid}/enable", json={}, headers=h)
    assert r.status_code == 200, r.text
    assert iid in client.app.state.platform.strategies.runtimes
    r = client.post(f"/api/instances/{iid}/disable", json={}, headers=h)
    assert r.status_code == 200
    assert iid not in client.app.state.platform.strategies.runtimes
    client.post(f"/api/instances/{iid}/enable", json={}, headers=h)

    # --- every page renders
    for url in ["/", "/strategies", f"/strategies/{iid}", f"/strategies/{iid}/edit", f"/strategies/{iid}/slots",
                "/accounts", f"/accounts/{aid}/edit", "/settings", "/compare", "/compare?period=all&show=all", "/compare?period=custom&day_from=2026-01-01&day_to=2026-12-31", "/trades", "/orders", "/orders?external=1",
                "/logs", "/backtest", f"/backtest?instance_id={iid}", "/users", "/audit", "/profile",
                "/api/state", f"/api/instances/{iid}/detail", "/api/events?limit=50", "/trades.csv",
                "/api/events/export?fmt=csv", "/api/events/export?fmt=json", "/settings/export.yaml"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
    # every form on every page carries the session's CSRF token (regression: macro forms sent an empty one)
    import re as _re
    for url in ["/", "/strategies", f"/strategies/{iid}/edit", "/strategies/new", "/accounts", f"/accounts/{aid}/edit",
                "/accounts/new?broker=angelone", "/settings", "/backtest", "/users", "/profile"]:
        html = client.get(url).text
        for fhtml in _re.findall(r"<form[^>]*method=\"post\".*?</form>", html, _re.S):
            m = _re.search(r'name="csrf_token" value="([^"]*)"', fhtml)
            assert m and m.group(1) == t, (url, fhtml[:120])
    state = client.get("/api/state").json()
    assert state["instances"][0]["name"] == "NIFTY Breakout – Paper"
    assert state["accounts"][0]["label"] == "Paper Sim"

    # --- websocket delivers live state
    # the test client always connects websockets to host "testserver"
    cookie = {"cookie": f"algo_session={client.cookies.get('algo_session')}"}
    with client.websocket_connect("/ws", headers={"origin": "http://testserver", **cookie}) as ws:
        kinds = set()
        for _ in range(3):
            kinds.add(json.loads(ws.receive_text())["type"])
        assert "state" in kinds
    # cross-site websocket is refused
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example", **cookie}) as ws:
            ws.receive_text()

    # --- settings save with inline validation, holidays, YAML round trip
    yaml_text = client.get("/settings/export.yaml").text
    assert "KEY123456" not in yaml_text and "global_settings" in yaml_text
    r = client.post("/settings/holidays", data={"csrf_token": t, "text": "2026-11-24, Guru Nanak Jayanti"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert date(2026, 11, 24) in client.app.state.platform.calendar.holidays
    r = client.post("/settings/import", data={"csrf_token": t},
                    files={"file": ("s.yaml", yaml_text.encode(), "application/x-yaml")}, follow_redirects=False)
    assert r.status_code == 303 and "Imported" in r.headers["location"]

    # --- backtest from CSV
    rows = ["timestamp,open,high,low,close"]
    d = datetime(2026, 11, 3, 9, 15)
    px = 24000.0
    import math
    for i in range(375):
        o = px
        px = 24000 + 60 * math.sin(i / 9) + (i % 7) * 0.5
        rows.append(f"{(d + timedelta(minutes=i)).isoformat()},{o:.2f},{max(o, px) + 1:.2f},{min(o, px) - 1:.2f},{px:.2f}")
    files = {"csv_file": ("nifty.csv", "\n".join(rows).encode(), "text/csv")}
    bt = {k: v for k, v in form.items() if k.startswith("params.")}
    bt.update({"csrf_token": t, "day_from": "2026-11-03", "day_to": "2026-11-03", "source": "csv", "iv": "13",
               "lot_size": "65", "expiry_weekday": "1", "slippage_pct": "0.5"})
    r = client.post("/backtest?template=nifty_ref_breakout", data=bt, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    page = client.get(r.headers["location"]).text
    assert "Equity curve" in page and "Win rate" in page

    # --- audit has the important actions
    audit = client.get("/audit").text
    for action in ("account_created", "instance_created", "instance_enabled", "instance_disabled", "settings_imported",
                   "holidays_changed", "backtest_run"):
        assert action in audit, action
