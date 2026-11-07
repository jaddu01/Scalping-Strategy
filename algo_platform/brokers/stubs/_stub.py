"""Shared base for not-yet-implemented broker adapters. Every method raises NotImplementedError."""
from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

from brokers.base import (BrokerAdapter, Candle, Exchange, Fill, Funds, Instrument, InstrumentKey, Order,
                          OrderModify, OrderRequest, Position, Profile)


class StubAdapter(BrokerAdapter):
    """STUB: interface-complete placeholder. Implement each method by mapping the broker's SDK
    payloads to the normalized models in brokers/base.py (see brokers/angelone.py for a full example)."""

    broker_type: ClassVar[str] = "stub"
    sdk_hint: ClassVar[str] = ""

    def _ni(self, what: str):
        raise NotImplementedError(f"{self.display_name} adapter is a stub: {what} is not implemented. {self.sdk_hint}")

    async def login(self) -> Profile:
        self._ni("login")

    async def refresh_session(self) -> None:
        self._ni("refresh_session")

    def is_connected(self) -> bool:
        return False

    async def get_profile(self) -> Profile:
        self._ni("get_profile")

    async def get_funds(self) -> Funds:
        self._ni("get_funds")

    async def load_instruments(self, force: bool = False) -> int:
        self._ni("load_instruments")

    def resolve_symbol(self, key: InstrumentKey) -> Instrument:
        self._ni("resolve_symbol")

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        self._ni("option_expiries")

    def find_instrument(self, exchange: Exchange, token: str) -> Instrument | None:
        self._ni("find_instrument")

    async def place_order(self, req: OrderRequest) -> str:
        self._ni("place_order")

    async def modify_order(self, req: OrderModify) -> str:
        self._ni("modify_order")

    async def cancel_order(self, order_id: str) -> str:
        self._ni("cancel_order")

    async def get_order_book(self) -> list[Order]:
        self._ni("get_order_book")

    async def get_trade_book(self) -> list[Fill]:
        self._ni("get_trade_book")

    async def get_positions(self) -> list[Position]:
        self._ni("get_positions")

    async def get_ltp(self, instruments: list[Instrument]) -> dict[tuple[str, str], float]:
        self._ni("get_ltp")

    async def get_historical_candles(self, instrument: Instrument, timeframe_min: int, start: datetime,
                                     end: datetime) -> list[Candle]:
        self._ni("get_historical_candles")

    async def subscribe_ticks(self, instruments: list[Instrument]) -> None:
        self._ni("subscribe_ticks")

    async def unsubscribe_ticks(self, instruments: list[Instrument]) -> None:
        self._ni("unsubscribe_ticks")
