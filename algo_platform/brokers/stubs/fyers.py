"""STUB adapter for Fyers (API v3).

SDK: fyers-apiv3. OAuth login (auth code -> access token); TOTP-based automation is possible.
Ticks via FyersDataSocket, order updates via FyersOrderSocket. Order tag field: `orderTag`.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from brokers.base import BrokerCredentials
from brokers.stubs._stub import StubAdapter


class FyersCredentials(BrokerCredentials):
    client_id: str = Field(..., title="Client ID (FYxxxx)")
    app_id: str = Field(..., title="App ID")
    secret_key: str = Field(..., title="Secret key", json_schema_extra={"secret": True})
    redirect_uri: str = Field("http://127.0.0.1:8000/fyers/callback", title="Redirect URI")
    pin: str = Field("", title="PIN (optional)", json_schema_extra={"secret": True})
    totp_secret: str = Field("", title="TOTP secret (optional)", json_schema_extra={"secret": True})


class FyersAdapter(StubAdapter):
    broker_type: ClassVar[str] = "fyers"
    display_name: ClassVar[str] = "Fyers (API v3) (stub)"
    credentials_model: ClassVar[type[BrokerCredentials]] = FyersCredentials
    sdk_hint: ClassVar[str] = "Install fyers-apiv3 and implement brokers/stubs/fyers.py."
