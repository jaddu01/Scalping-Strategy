"""Builds a full Platform against a temporary database and the MockBroker, with a fake clock."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from brokers.registry import BROKERS
from core.clock import FakeClock
from core.config import AppConfig
from core.events import Event
from engine.platform import Platform
from storage.crypto import Crypto, get_crypto, set_crypto
from storage.db import Database
from storage.models import BrokerAccount, StrategyInstance
from tests.conftest import DAY, NEXT_TUE, at, make_candle
from tests.mock_broker import SPOT_TOKEN, MockBroker


@dataclass
class Env:
    platform: Platform
    db: Database
    clock: FakeClock
    events: list[Event] = field(default_factory=list)
    account_id: int = 0
    instance_ids: list[int] = field(default_factory=list)

    @property
    def mock(self) -> MockBroker:
        return self.platform.accounts[self.account_id].adapter  # type: ignore[return-value]

    def types(self, event_type: str | None = None) -> list[Event]:
        return [e for e in self.events if event_type is None or e.event_type == event_type]

    def messages(self, event_type: str) -> list[str]:
        return [e.message for e in self.types(event_type)]

    async def settle(self, n: int = 6, delay: float = 0.03) -> None:
        for _ in range(n):
            for rt in self.platform.strategies.runtimes.values():
                rt._wake.set()
            await asyncio.sleep(delay)

    async def at(self, hhmm: str, sec: int = 0, n: int = 6) -> None:
        self.clock.set(at(hhmm, sec))
        await self.settle(n)

    async def wait_for(self, pred: Any, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return
            await self.settle(1, 0.02)
        raise AssertionError("condition not met in time")

    def ref_candle(self, start: str, high: float, low: float) -> None:
        self.mock.history[at(start)] = make_candle(at(start), (high + low) / 2, high, low, (high + low) / 2)

    def spot(self, price: float) -> None:
        self.mock.push(SPOT_TOKEN, price)

    def option_price(self, strike: float, typ: str, price: float, expiry=DAY, underlying: str = "NIFTY") -> None:
        self.mock.push(self.mock.option(strike, typ, expiry, underlying).token, price)


async def build_env(tmp_path: Path, start: datetime | None = None, instances: list[dict[str, Any]] | None = None,
                    mode: str = "paper") -> Env:
    MockBroker.expiries = [DAY, NEXT_TUE]
    BROKERS["mock"] = MockBroker
    if get_crypto is None:  # pragma: no cover
        pass
    key = Crypto.generate_key()
    set_crypto(Crypto(key))
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_all()
    cfg = AppConfig(data_dir=tmp_path, database_url=db.url, master_key=key)
    clock = FakeClock(start or at("09:49"))
    with db.session() as s:
        acc = BrokerAccount(broker_type="mock", label="Mock1", client_id="MOCK1",
                            credentials_enc=get_crypto().encrypt_json({}), enabled=True)
        s.add(acc)
        s.flush()
        acc_id = acc.id
        ids = []
        for spec in instances or [{"name": "NIFTY-T"}]:
            inst = StrategyInstance(name=spec["name"], template_id=spec.get("template", "nifty_ref_breakout"),
                                    params=({"protective_sl_pct": 40} if spec.get("template", "nifty_ref_breakout") == "nifty_ref_breakout" else {})
                                    | spec.get("params", {}), broker_account_id=acc_id,
                                    mode=spec.get("mode", mode), enabled=spec.get("enabled", True),
                                    risk_limits=spec.get("risk", {}))
            s.add(inst)
            s.flush()
            ids.append(inst.id)
    p = Platform(cfg, db, clock=clock, db_events=False, file_events=False)
    env = Env(platform=p, db=db, clock=clock, account_id=acc_id, instance_ids=ids)
    p.bus.add_sink(env.events.append)
    await p.start()
    await env.wait_for(lambda: p.accounts[acc_id].ready.is_set())
    return env
