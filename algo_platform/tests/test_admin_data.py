"""Admin market-data download (MCX Gold Mini study): contract choice, CSV, admin only."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.api.admin_data import futures_contracts
from brokers.base import Candle, Exchange
from tests.test_rbac import as_user, client  # noqa: F401  (fixture)

ROWS = [{"exchange": "MCX", "tradingsymbol": "GOLDM05NOV26FUT", "symboltoken": "1"},
        {"exchange": "MCX", "tradingsymbol": "GOLDM05JAN27FUT", "symboltoken": "3"},
        {"exchange": "MCX", "tradingsymbol": "GOLDM05DEC26FUT", "symboltoken": "2"},
        {"exchange": "MCX", "tradingsymbol": "GOLDM26DEC5000CE", "symboltoken": "9"},
        {"exchange": "MCX", "tradingsymbol": "GOLD05DEC26FUT", "symboltoken": "7"}]


def test_futures_contracts_nearest_live_first():
    got = futures_contracts(ROWS, "GOLDM", date(2026, 11, 18))
    assert [(e.isoformat(), s, t) for e, s, t in got] == [("2026-12-05", "GOLDM05DEC26FUT", "2"),
                                                         ("2027-01-05", "GOLDM05JAN27FUT", "3")]


class FakeAngel:
    def __init__(self):
        self.asked = None

    async def search_symbols(self, exchange, text):
        return ROWS

    async def get_historical_candles(self, inst, tf, start, end):
        self.asked = (inst.exchange, inst.token, inst.tradingsymbol, tf)
        t0 = datetime(2026, 11, 17, 9, 0).astimezone()
        return [Candle(start=t0 + timedelta(minutes=5 * i), timeframe_min=5, open=100 + i, high=101 + i,
                       low=99 + i, close=100.5 + i, volume=10) for i in range(3)]


def test_history_download_is_admin_only_and_uses_the_feed_session(client, monkeypatch):  # noqa: F811
    from app.api import admin_data

    p = client.app.state.platform
    fake = FakeAngel()
    monkeypatch.setattr(admin_data, "_feed_adapter", lambda request: fake)
    assert client.get("/api/admin/symbols?q=GOLDM").json()["results"] == ROWS
    r = client.get("/api/admin/history.csv?exchange=MCX&symbol=GOLDM&tf=5&days=30")
    assert r.status_code == 200, r.text
    lines = r.text.strip().splitlines()
    assert lines[0] == "timestamp,open,high,low,close,volume" and len(lines) == 4
    assert fake.asked == (Exchange.MCX, "2", "GOLDM05DEC26FUT", 5)  # nearest live contract
    assert "GOLDM05DEC26FUT_5m" in r.headers["content-disposition"]
    assert any((p.config.data_dir / "exports").glob("MCX_GOLDM05DEC26FUT_5m_*.csv"))
    assert client.get("/api/admin/history.csv?symbol=GOLDM&contract=GOLDM05JAN27FUT").status_code == 200
    assert fake.asked[1] == "3"
    assert client.get("/api/admin/history.csv?symbol=SILVERM").status_code == 404
    as_user(client, "bina")  # a partner: not allowed
    assert client.get("/api/admin/history.csv?symbol=GOLDM").status_code == 403
