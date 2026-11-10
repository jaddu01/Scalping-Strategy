"""Broker sync: adaptive poll interval and quiet handling of the broker's rate limit."""
from __future__ import annotations

from datetime import date, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from brokers.angelone import AngelOneAdapter, AngelOneCredentials, is_rate_limited
from brokers.base import BrokerRateLimited, OrderStatus
from core.clock import FakeClock, combine
from engine.accounts import BrokerSync
from tests.conftest import at
from tests.engine_harness import build_env

ANGEL_MSG = "Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'"


class DataException(Exception):  # same name as the SmartAPI exception
    pass


class RateLimitedApi:
    def __init__(self, fail: int) -> None:
        self.fail = fail
        self.calls = 0

    def generateSession(self, client, pin, totp):
        return {"status": True, "data": {"jwtToken": "Bearer J", "refreshToken": "R", "feedToken": "F",
                                         "clientcode": client, "name": "T"}}

    def getfeedToken(self):
        return "F"

    def position(self):
        self.calls += 1
        if self.calls <= self.fail:
            raise DataException(ANGEL_MSG)
        return {"status": True, "data": []}


async def _angel(monkeypatch, fail: int) -> tuple[AngelOneAdapter, RateLimitedApi]:
    a = AngelOneAdapter(AngelOneCredentials(client_id="A1", api_key="K", pin="1234", totp_secret="JBSWY3DPEHPK3PXP"),
                        account_id=1)
    api = RateLimitedApi(fail)
    monkeypatch.setattr(a, "_new_api", lambda: api)
    monkeypatch.setattr("brokers.angelone.RATE_LIMIT_RETRY_SEC", 0.01)
    await a.login()
    return a, api


def test_detects_angel_rate_limit_message():
    assert is_rate_limited(Exception(ANGEL_MSG))
    assert not is_rate_limited(Exception("Invalid Token"))


async def test_angel_retries_once_then_raises_rate_limited(monkeypatch):
    a, api = await _angel(monkeypatch, fail=1)
    assert await a.get_positions() == [] and api.calls == 2  # one quiet retry
    a, api = await _angel(monkeypatch, fail=5)
    with pytest.raises(BrokerRateLimited):
        await a.get_positions()
    assert api.calls == 2


class FlakyAdapter:
    is_paper = False

    def __init__(self) -> None:
        self.fail = False

    def is_connected(self) -> bool:
        return True

    def on_order_update(self, cb: Any) -> None:
        pass

    async def get_order_book(self):
        if self.fail:
            raise BrokerRateLimited("orderBook: rate limited", retryable=True)
        return []

    async def get_trade_book(self):
        return []

    async def get_positions(self):
        return []


async def test_rate_limit_is_quiet_then_alerts_after_30s_and_clears(tmp_path):
    from core.events import EventBus
    from storage.db import Database

    db = Database(f"sqlite:///{tmp_path / 's.db'}")
    db.create_all()
    bus, events = EventBus(), []
    bus.add_sink(events.append)
    clock = FakeClock(at("10:00"))
    ad = FlakyAdapter()
    sync = BrokerSync(ad, 1, "Angel", False, db, bus, clock, lambda: 3.0, rate_alert_sec=lambda: 30.0)
    ad.fail = True
    for sec in (0, 5, 10, 25):
        clock.set(at("10:00", sec))
        await sync.poll_once()
    types = [e.event_type for e in events]
    assert "SYNC_POLL_FAILED" not in types and "SYNC_RATE_LIMITED" not in types  # quiet
    clock.set(at("10:00", 31))
    await sync.poll_once()
    clock.set(at("10:00", 40))
    await sync.poll_once()
    assert [e.event_type for e in events].count("SYNC_RATE_LIMITED") == 1  # one alert, not one per poll
    ad.fail = False
    clock.set(at("10:00", 45))
    await sync.poll_once()
    assert events[-1].event_type == "SYNC_RATE_LIMIT_CLEARED"
    ad.fail = True  # a new single failure later is quiet again
    clock.set(at("10:05"))
    await sync.poll_once()
    assert [e.event_type for e in events].count("SYNC_RATE_LIMITED") == 1


async def test_poll_interval_by_market_hours_and_open_trade(tmp_path):
    env = await build_env(tmp_path)  # Thursday 09:49, mock broker (a real-broker adapter)
    try:
        p, acc = env.platform, env.platform.accounts[env.account_id]
        sync = acc.live_sync
        assert p.sync_poll_interval(sync) == 12.0  # market hours, flat
        p.trades.live[999] = SimpleNamespace(account_id=env.account_id, mode="live")
        assert p.sync_poll_interval(sync) == 3.0  # live trade open
        p.trades.live[999] = SimpleNamespace(account_id=env.account_id, mode="paper")
        assert p.sync_poll_interval(sync) == 12.0  # a paper trade never needs the real broker polled fast
        del p.trades.live[999]
        sync.orders["X1"] = SimpleNamespace(status=OrderStatus.OPEN)
        assert p.sync_poll_interval(sync) == 3.0  # working order (e.g. placed by hand in the broker app)
        del sync.orders["X1"]
        env.clock.set(at("08:40"))
        assert p.sync_poll_interval(sync) == 60.0  # before pre-open
        env.clock.set(at("15:36"))
        assert p.sync_poll_interval(sync) == 60.0  # after close + 5 min
        env.clock.set(at("15:34"))
        assert p.sync_poll_interval(sync) == 12.0
        env.clock.set(combine(date(2026, 11, 7), time(11, 0)))  # Saturday
        assert p.sync_poll_interval(sync) == 60.0
    finally:
        await env.platform.stop()
