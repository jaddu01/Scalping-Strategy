"""STUB adapter for Dhan (DhanHQ).

SDK: dhanhq. Authentication uses a client id + access token generated in the Dhan web console.
Live market feed and order updates via DhanHQ websockets. Order tag field: `correlationId`.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from brokers.base import BrokerCredentials
from brokers.stubs._stub import StubAdapter


class DhanCredentials(BrokerCredentials):
    client_id: str = Field(..., title="Client ID")
    access_token: str = Field(..., title="Access token", json_schema_extra={"secret": True})


class DhanAdapter(StubAdapter):
    broker_type: ClassVar[str] = "dhan"
    display_name: ClassVar[str] = "Dhan (DhanHQ) (stub)"
    credentials_model: ClassVar[type[BrokerCredentials]] = DhanCredentials
    sdk_hint: ClassVar[str] = "Install dhanhq and implement brokers/stubs/dhan.py."
