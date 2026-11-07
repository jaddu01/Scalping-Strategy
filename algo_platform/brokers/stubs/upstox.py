"""STUB adapter for Upstox.

SDK: upstox-python-sdk (v2 API). OAuth2 login returns an access token valid for the day.
Market data via the Market Data Feed V3 websocket (protobuf); order updates via the Portfolio stream feed.
Order tag field: `tag`.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from brokers.base import BrokerCredentials
from brokers.stubs._stub import StubAdapter


class UpstoxCredentials(BrokerCredentials):
    client_id: str = Field(..., title="UCC / client id")
    api_key: str = Field(..., title="API key", json_schema_extra={"secret": True})
    api_secret: str = Field(..., title="API secret", json_schema_extra={"secret": True})
    redirect_uri: str = Field("http://127.0.0.1:8000/upstox/callback", title="Redirect URI")
    access_token: str = Field("", title="Access token (paste daily if not automated)", json_schema_extra={"secret": True})


class UpstoxAdapter(StubAdapter):
    broker_type: ClassVar[str] = "upstox"
    display_name: ClassVar[str] = "Upstox (stub)"
    credentials_model: ClassVar[type[BrokerCredentials]] = UpstoxCredentials
    sdk_hint: ClassVar[str] = "Install upstox-python-sdk and implement brokers/stubs/upstox.py."
