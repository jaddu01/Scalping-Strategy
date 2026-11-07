"""STUB adapter for Zerodha (Kite Connect).

SDK: kiteconnect (KiteConnect, KiteTicker). Login is a browser redirect flow that returns a
request_token; exchange it for an access_token with generate_session(request_token, api_secret).
Order updates arrive on KiteTicker.on_order_update. Order tag field: `tag` (max 20 chars).
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from brokers.base import BrokerCredentials
from brokers.stubs._stub import StubAdapter


class ZerodhaCredentials(BrokerCredentials):
    user_id: str = Field(..., title="User ID")
    api_key: str = Field(..., title="API key", json_schema_extra={"secret": True})
    api_secret: str = Field(..., title="API secret", json_schema_extra={"secret": True})
    password: str = Field("", title="Password (optional, for automated login)", json_schema_extra={"secret": True})
    totp_secret: str = Field("", title="TOTP secret (optional)", json_schema_extra={"secret": True})


class ZerodhaAdapter(StubAdapter):
    broker_type: ClassVar[str] = "zerodha"
    display_name: ClassVar[str] = "Zerodha (Kite Connect) (stub)"
    credentials_model: ClassVar[type[BrokerCredentials]] = ZerodhaCredentials
    sdk_hint: ClassVar[str] = "Install kiteconnect and implement brokers/stubs/zerodha.py."
