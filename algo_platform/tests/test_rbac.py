"""Partners (RBAC): each partner owns their broker accounts and strategies; a strategy may only trade on
its owner's account; everyone sees everything, but only the owner (or an admin) can change, start, stop
or kill it."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from core.config import AppConfig
from storage.crypto import Crypto
from storage.models import BrokerAccount, StrategyInstance, User
from tests.test_auth import PW, csrf_from, login, setup_admin

STRATEGY_FORM = {
    "broker_account_id": "", "mode": "paper", "notes": "", "params.underlying": "NIFTY",
    "params.reference_candle_starts": "09:45, 10:00, 10:15, 10:45, 11:15, 11:45, 12:10, 12:45, 13:15, 13:45, 14:10, 14:35, 14:55",
    "params.candle_timeframe_min": "5", "params.buffer_points": "2.25", "params.entry_window_candles": "2",
    "params.exit_offset_min": "15", "params.lots": "5", "params.strike_step": "50",
    "params.expiry": "nearest_weekly", "params.entry_trigger": "tick", "params.entry_order_type": "MARKET",
    "params.limit_slippage_pct": "1", "params.protective_sl_pct": "40", "params.take_profit_mode": "none",
    "params.take_profit_value": "0", "params.candle_finalization_delay_sec": "5",
    "risk.max_daily_loss": "0", "risk.max_daily_profit": "0", "risk.max_trades_per_day": "13",
    "risk.max_lots_per_order": "20", "risk.max_premium_per_trade": "0"}
ACCOUNT_FORM = {"enabled": "on", "cred.starting_capital": "500000", "cred.slippage_ticks": "0",
                "cred.sim_spot_start": "24000", "cred.sim_points_per_tick": "1.2", "cred.sim_iv": "0.14",
                "cred.sim_lot_size": "65", "cred.sim_tick_interval_sec": "0.5",
                "risk.max_daily_loss": "0", "risk.max_open_positions": "5", "risk.max_orders_per_day": "500"}


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'web.db'}",
                    master_key=Crypto.generate_key())
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()
    with TestClient(create_app(cfg, start_engine=True), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        login(c)
        t = csrf_from(c.get("/").text)
        for name, role in (("bina", "trader"), ("chetan", "trader"), ("vik", "viewer")):
            r = c.post("/users/new", data={"csrf_token": t, "username": name, "password": PW, "role": role},
                       follow_redirects=False)
            assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
        with c.app.state.db.session() as s:
            for u in s.scalars(select(User)):
                u.must_change_password = False
        yield c


def as_user(c: TestClient, name: str) -> str:
    """Log in as `name` (fresh cookies) and return the CSRF token."""
    c.cookies.clear()
    routes_auth_hits_clear()
    r = login(c, name)
    assert r.status_code == 303, r.text
    return csrf_from(c.get("/").text)


def routes_auth_hits_clear() -> None:
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()


def uid(c: TestClient, name: str) -> int:
    with c.app.state.db.session() as s:
        return s.scalar(select(User.id).where(User.username == name))


def add_account(c: TestClient, t: str, label: str, **extra: str) -> int:
    r = c.post("/accounts/new?broker=paper", data={"csrf_token": t, "label": label, **ACCOUNT_FORM, **extra},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    with c.app.state.db.session() as s:
        return s.scalar(select(BrokerAccount.id).where(BrokerAccount.label == label))


def add_strategy(c: TestClient, t: str, name: str, account_id: int, **extra: str):
    return c.post("/strategies/new?template=nifty_ref_breakout",
                  data={**STRATEGY_FORM, "csrf_token": t, "name": name, "broker_account_id": str(account_id), **extra},
                  follow_redirects=False)


def strategy(c: TestClient, name: str) -> StrategyInstance:
    with c.app.state.db.session() as s:
        row = s.scalar(select(StrategyInstance).where(StrategyInstance.name == name))
        s.expunge(row)
        return row


def test_partner_owns_and_controls_only_their_own(client):
    c = client
    # Bina: her own account + strategy
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    assert add_strategy(c, tb, "Bina Breakout", acc_b).status_code == 303
    sb = strategy(c, "Bina Breakout")
    assert sb.owner_user_id == uid(c, "bina")
    with c.app.state.db.session() as s:
        assert s.get(BrokerAccount, acc_b).owner_user_id == uid(c, "bina")

    # Chetan: his own account; he cannot link a strategy to Bina's account
    tc = as_user(c, "chetan")
    acc_c = add_account(c, tc, "Chetan Angel")
    r = add_strategy(c, tc, "Chetan on Bina", acc_b)
    assert r.status_code == 400 and "belongs to another partner" in r.text
    assert add_strategy(c, tc, "Chetan Breakout", acc_c).status_code == 303
    # his strategy form only offers his own accounts
    form = c.get("/strategies/new").text
    assert "Chetan Angel" in form and "Bina Angel" not in form

    # he sees only his own things: Bina's strategy, account and P&L are private ...
    assert c.get(f"/strategies/{sb.id}").status_code == 404
    assert c.get(f"/strategies/{sb.id}/slots").status_code == 404
    assert c.get(f"/api/instances/{sb.id}/detail").status_code == 404
    page = c.get("/strategies?owner=all").text  # a partner cannot widen the filter
    assert "Bina Breakout" not in page and "Chetan Breakout" in page
    assert "Bina Angel" not in c.get("/accounts?owner=all").text
    st = c.get("/api/state?owner=all").json()
    assert {i["name"] for i in st["instances"]} == {"Chetan Breakout"}
    assert {a["label"] for a in st["accounts"]} == {"Chetan Angel"}
    assert "Bina" not in c.get("/compare?owner=all&show=all").text
    assert all(e["account_id"] in (None, acc_c) for e in c.get("/api/events?owner=all").json()["events"])
    # ... and every change is refused
    h = {"X-CSRF-Token": tc}
    for method, url in (("get", f"/strategies/{sb.id}/edit"), ("post", f"/strategies/{sb.id}/edit"),
                        ("post", f"/strategies/{sb.id}/delete"), ("get", f"/accounts/{acc_b}/edit"),
                        ("post", f"/accounts/{acc_b}/edit"), ("post", f"/accounts/{acc_b}/delete"),
                        ("post", f"/accounts/{acc_b}/test"), ("post", f"/accounts/{acc_b}/reconnect")):
        r = getattr(c, method)(url, **({"data": {**STRATEGY_FORM, "csrf_token": tc, "name": "x", "label": "x"}}
                                       if method == "post" else {}), follow_redirects=False)
        assert r.status_code == 403, (method, url, r.status_code)
    for url in (f"/api/instances/{sb.id}/enable", f"/api/instances/{sb.id}/disable", f"/api/instances/{sb.id}/kill",
                f"/api/accounts/{acc_b}/kill", f"/api/accounts/{acc_b}/kill/reset", "/api/kill/global"):
        assert c.post(url, json={}, headers=h).status_code == 403, url
    assert c.post("/accounts/{}/feed".format(acc_c), data={"csrf_token": tc}).status_code == 403  # admin only
    assert not strategy(c, "Bina Breakout").enabled

    # his own strategy: he can start and stop it
    sc = strategy(c, "Chetan Breakout")
    assert c.post(f"/api/instances/{sc.id}/enable", json={}, headers=h).status_code == 200
    assert sc.id in c.app.state.platform.strategies.runtimes
    assert c.post(f"/api/instances/{sc.id}/disable", json={}, headers=h).status_code == 200

    # "Clone" on a shared strategy = start his own run of it
    r = c.post(f"/strategies/{sb.id}/clone", data={"csrf_token": tc}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/strategies/shared/{sb.strategy_id}/run"

    # partner filter
    page = c.get("/strategies?owner=me").text
    assert "Chetan Breakout" in page and f'href="/strategies/{sb.id}"' not in page  # only his runs

    # the admin can step in on anyone's strategy (emergencies)
    ta = as_user(c, "admin")
    assert c.get(f"/strategies/{sb.id}/edit").status_code == 200
    assert c.post(f"/api/instances/{sb.id}/enable", json={}, headers={"X-CSRF-Token": ta}).status_code == 200
    # but even the admin cannot link Bina's strategy to Chetan's account
    r = c.post(f"/strategies/{sb.id}/edit", data={**STRATEGY_FORM, "csrf_token": ta, "name": "Bina Breakout",
                                                    "broker_account_id": str(acc_c)})
    assert r.status_code == 400 and "belongs to another partner" in r.text


def test_viewer_is_read_only(client):
    c = client
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    add_strategy(c, tb, "Bina Breakout", acc_b)
    tv = as_user(c, "vik")
    assert c.get("/strategies").status_code == 200 and c.get("/compare").status_code == 200
    assert c.get("/strategies/new").status_code == 403
    assert c.get("/accounts/new?broker=paper").status_code == 403
    r = c.post("/backtest", data={"csrf_token": tv}, follow_redirects=False)
    assert r.status_code == 403
    sb = strategy(c, "Bina Breakout")
    assert c.post(f"/api/instances/{sb.id}/enable", json={}, headers={"X-CSRF-Token": tv}).status_code == 403


def test_engine_refuses_strategy_on_another_partners_account(client):
    c = client
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    add_strategy(c, tb, "Bina Breakout", acc_b)
    sb = strategy(c, "Bina Breakout")
    with c.app.state.db.session() as s:  # e.g. the account was given to someone else afterwards
        s.get(BrokerAccount, acc_b).owner_user_id = uid(c, "chetan")
    r = c.post(f"/api/instances/{sb.id}/enable", json={}, headers={"X-CSRF-Token": tb})
    assert r.status_code == 200
    assert sb.id not in c.app.state.platform.strategies.runtimes
    row = strategy(c, "Bina Breakout")
    assert row.status == "ERROR" and "another partner" in (row.status_message or "")


def test_account_owner_change_blocked_while_strategies_use_it(client):
    c = client
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    add_strategy(c, tb, "Bina Breakout", acc_b)
    ta = as_user(c, "admin")
    r = c.post(f"/accounts/{acc_b}/edit", data={"csrf_token": ta, "label": "Bina Angel", **ACCOUNT_FORM,
                                                 "owner_user_id": str(uid(c, "chetan"))})
    assert r.status_code == 400 and "re-link them first" in r.text


def test_deactivating_a_partner_stops_their_strategies(client):
    c = client
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    add_strategy(c, tb, "Bina Breakout", acc_b)
    sb = strategy(c, "Bina Breakout")
    assert c.post(f"/api/instances/{sb.id}/enable", json={}, headers={"X-CSRF-Token": tb}).status_code == 200
    ta = as_user(c, "admin")
    assert "square off open trades" in c.get("/users").text
    r = c.post(f"/users/{uid(c, 'bina')}/active", data={"csrf_token": ta, "active": "0", "open_trades": "exit_now"},
               follow_redirects=False)
    assert r.status_code == 303 and "stopped+1+strategies" in r.headers["location"]
    assert not strategy(c, "Bina Breakout").enabled
    assert sb.id not in c.app.state.platform.strategies.runtimes


def test_admin_creates_for_a_partner_and_pages_render(client):
    c = client
    ta = as_user(c, "admin")
    acc = add_account(c, ta, "Chetan Angel", owner_user_id=str(uid(c, "chetan")))
    with c.app.state.db.session() as s:
        assert s.get(BrokerAccount, acc).owner_user_id == uid(c, "chetan")
    assert add_strategy(c, ta, "For Chetan", acc, owner_user_id=str(uid(c, "chetan"))).status_code == 303
    assert strategy(c, "For Chetan").owner_user_id == uid(c, "chetan")
    for url in ("/", "/strategies?owner=all", f"/strategies?owner={uid(c, 'chetan')}", "/accounts?owner=me",
                "/compare?owner=me", "/trades?owner=me", "/orders?owner=all", "/users", "/api/state"):
        assert c.get(url).status_code == 200, url
    assert c.get("/api/state").json()["instances"] == []  # the admin's own view: nothing of Chetan's
    st = c.get("/api/state?owner=all").json()
    assert st["instances"][0]["owner"] == "chetan" and st["accounts"][0]["owner"] == "chetan"
    st = c.get(f"/api/state?owner={uid(c, 'chetan')}").json()
    assert [i["name"] for i in st["instances"]] == ["For Chetan"]
    assert c.get(f"/strategies/{strategy(c, 'For Chetan').id}").status_code == 200  # admin may open it


def test_migration_gives_existing_data_to_the_first_admin(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from storage.db import MIGRATIONS_DIR, Database

    db = Database(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db.url)
    cfg.attributes["connection_engine"] = db.engine
    command.upgrade(cfg, "0001")
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO users (username, password_hash, role, totp_enabled, failed_attempts, "
                          "must_change_password, is_active, created_at) VALUES ('boss', 'x', 'admin', 0, 0, 0, 1, "
                          "'2026-11-01 00:00:00')"))
        conn.execute(text("INSERT INTO broker_accounts (broker_type, label, client_id, credentials_enc, enabled, "
                          "auto_relogin, risk_limits, status, kill_switch, created_at, updated_at) VALUES "
                          "('paper', 'A', '', '', 1, 1, '{}', 'DISCONNECTED', 0, '2026-11-01', '2026-11-01')"))
        conn.execute(text("INSERT INTO strategy_instances (name, template_id, params, broker_account_id, mode, "
                          "enabled, risk_limits, notes, status, stop_after_trade, created_at, updated_at) VALUES "
                          "('S', 'nifty_ref_breakout', '{}', 1, 'paper', 0, '{}', '', 'STOPPED', 0, "
                          "'2026-11-01', '2026-11-01')"))
        # a trade pointing at both (what made a table-rebuild migration fail with foreign_keys=ON)
        conn.execute(text("INSERT INTO trades (trade_ref, instance_id, account_id, mode, trade_date, slot_id, "
                          "direction, exchange, tradingsymbol, token, lots, lot_size, quantity, open_quantity, "
                          "spot_sl_side, tp_spot_side, status, meta, created_at, updated_at) VALUES ('T1', 1, 1, "
                          "'paper', '2026-11-01', '', 'CE_BUY', 'NFO', 'X', '1', 1, 65, 65, 0, 'below', 'above', "
                          "'CLOSED', '{}', '2026-11-01', '2026-11-01')"))
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    db.migrate()
    backups = list((tmp_path / "backups").glob("pre-upgrade-0001-to-*.db"))
    assert len(backups) == 1  # the database was copied before the upgrade
    with db.session() as s:
        boss = s.scalar(select(User.id).where(User.username == "boss"))
        assert s.scalar(select(BrokerAccount.owner_user_id)) == boss
        assert s.scalar(select(StrategyInstance.owner_user_id)) == boss
        from storage.models import SharedStrategy

        sh = s.scalar(select(SharedStrategy))  # each old instance became a shared strategy + its run
        assert sh.name == "S" and sh.created_by == boss and s.scalar(select(StrategyInstance.strategy_id)) == sh.id
    db.dispose()


def run_form(c: TestClient, t: str, sid: int, name: str, account_id: int, lots: str, **extra: str):
    return c.post(f"/strategies/shared/{sid}/run", data={
        "csrf_token": t, "name": name, "broker_account_id": str(account_id), "mode": "paper", "notes": "",
        "params.lots": lots, "risk.max_daily_loss": "0", "risk.max_daily_profit": "0",
        "risk.max_trades_per_day": "13", "risk.max_lots_per_order": "20", "risk.max_premium_per_trade": "0",
        **extra}, follow_redirects=False)


def test_shared_strategy_each_partner_runs_it_on_own_terms(client):
    from storage.models import SharedStrategy

    c = client
    ta = as_user(c, "admin")
    acc_a = add_account(c, ta, "Admin Angel")
    assert add_strategy(c, ta, "Breakout", acc_a).status_code == 303
    mine = strategy(c, "Breakout")
    with c.app.state.db.session() as s:
        sh = s.get(SharedStrategy, mine.strategy_id)
        sid, rules_by = sh.id, sh.created_by
    assert rules_by == uid(c, "admin") and mine.params["lots"] == 5

    # Bina runs the same strategy on her own account with her own lots
    tb = as_user(c, "bina")
    acc_b = add_account(c, tb, "Bina Angel")
    form = c.get(f"/strategies/shared/{sid}/run").text
    assert "Bina Angel" in form and "Admin Angel" not in form and "Shared rules (read-only)" in form
    assert 'name="params.buffer_points"' not in form  # the rules are not hers to change here
    assert run_form(c, tb, sid, "Breakout – bina", acc_a, "2").status_code == 400  # not her account
    r = run_form(c, tb, sid, "Breakout – bina", acc_b, "2", **{"params.buffer_points": "99"})
    assert r.status_code == 303, r.text
    hers = strategy(c, "Breakout – bina")
    assert hers.owner_user_id == uid(c, "bina") and hers.strategy_id == sid and hers.broker_account_id == acc_b
    assert hers.params["lots"] == 2 and hers.params["buffer_points"] == 2.25  # own lots, shared rules

    # she cannot change the shared rules; she can make her own version
    assert c.get(f"/strategies/shared/{sid}/edit").status_code == 403
    r = c.post(f"/strategies/shared/{sid}/fork", data={"csrf_token": tb}, follow_redirects=False)
    assert r.status_code == 303
    with c.app.state.db.session() as s:
        fork = s.scalar(select(SharedStrategy).where(SharedStrategy.name == "Breakout (bina)"))
        fork_id = fork.id
    assert c.get(f"/strategies/shared/{fork_id}/edit").status_code == 200

    # her lots: she edits only her run
    page = c.get(f"/strategies/{hers.id}/edit").text
    assert 'name="params.lots"' in page and 'name="params.buffer_points"' not in page
    r = c.post(f"/strategies/{hers.id}/edit", data={
        "csrf_token": tb, "name": "Breakout – bina", "broker_account_id": str(acc_b), "mode": "paper", "notes": "",
        "params.lots": "3", "risk.max_daily_loss": "0", "risk.max_daily_profit": "0", "risk.max_trades_per_day": "13",
        "risk.max_lots_per_order": "20", "risk.max_premium_per_trade": "0"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert strategy(c, "Breakout – bina").params["lots"] == 3 and strategy(c, "Breakout").params["lots"] == 5

    # the admin changes the rules: every run follows, each keeps its own lots
    ta = as_user(c, "admin")
    rules = {k: v for k, v in STRATEGY_FORM.items() if k.startswith("params.") and k != "params.lots"}
    r = c.post(f"/strategies/shared/{sid}/edit", data={**rules, "csrf_token": ta, "name": "Breakout", "notes": "",
                                                      "params.buffer_points": "3.5"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert strategy(c, "Breakout").params["buffer_points"] == 3.5 and strategy(c, "Breakout").params["lots"] == 5
    assert strategy(c, "Breakout – bina").params["buffer_points"] == 3.5
    assert strategy(c, "Breakout – bina").params["lots"] == 3

    # pages
    for url in ("/strategies", f"/strategies/shared/{sid}", f"/backtest?strategy_id={sid}", f"/strategies/{hers.id}"):
        assert c.get(url).status_code == 200, url
    page = c.get(f"/strategies/shared/{sid}").text
    assert "Breakout – bina" not in page  # the admin's own view: only their run of these rules
    page = c.get(f"/strategies/shared/{sid}?owner=all").text
    assert "Breakout – bina" in page and "bina" in page
    # a strategy with runs cannot be deleted
    r = c.post(f"/strategies/shared/{sid}/delete", data={"csrf_token": ta}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_ema_lots_are_personal_and_tp_levels_are_rules(client):
    from storage.models import SharedStrategy

    c = client
    ta = as_user(c, "admin")
    acc = add_account(c, ta, "Admin Angel")
    r = c.post("/strategies/new?template=ema_rsi_breakout", data={
        "csrf_token": ta, "name": "EMA", "broker_account_id": str(acc), "mode": "paper", "notes": "",
        "params.lots": "10", "params.tp1_lots": "4", "params.tp2_lots": "4", "params.allow_long": "on",
        "params.allow_short": "on", "params.tp_enabled": "on", "params.trail_after_tp1": "on", "risk.max_daily_loss": "0",
        "risk.max_daily_profit": "0", "risk.max_trades_per_day": "13", "risk.max_lots_per_order": "20",
        "risk.max_premium_per_trade": "0"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    run = strategy(c, "EMA")
    assert run.params["tp1_lots"] == 4 and run.strategy_id
    with c.app.state.db.session() as s:
        assert s.get(SharedStrategy, run.strategy_id).template_id == "ema_rsi_breakout"
    # the edit-rules page has no lots fields; the TP RSI levels are rules
    page = c.get(f"/strategies/shared/{run.strategy_id}/edit").text
    assert 'name="params.tp1_lots"' not in page and 'name="params.tp1_rsi"' in page
