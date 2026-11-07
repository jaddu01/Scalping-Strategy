"""Broker adapter registry. Register a new adapter class here to make it selectable in the UI."""
from __future__ import annotations

from typing import Any

from brokers.angelone import AngelOneAdapter
from brokers.base import BrokerAdapter, BrokerCredentials
from brokers.paper import PaperBroker
from brokers.stubs.dhan import DhanAdapter
from brokers.stubs.fyers import FyersAdapter
from brokers.stubs.upstox import UpstoxAdapter
from brokers.stubs.zerodha import ZerodhaAdapter

BROKERS: dict[str, type[BrokerAdapter]] = {
    cls.broker_type: cls
    for cls in (AngelOneAdapter, PaperBroker, ZerodhaAdapter, UpstoxAdapter, DhanAdapter, FyersAdapter)
}


def get_broker_class(broker_type: str) -> type[BrokerAdapter]:
    try:
        return BROKERS[broker_type]
    except KeyError as exc:
        raise KeyError(f"unknown broker type {broker_type!r}; known: {sorted(BROKERS)}") from exc


def credentials_model(broker_type: str) -> type[BrokerCredentials]:
    return get_broker_class(broker_type).credentials_model


def create_adapter(broker_type: str, credentials: dict[str, Any], **kw: Any) -> BrokerAdapter:
    cls = get_broker_class(broker_type)
    creds = cls.credentials_model.model_validate(credentials)
    return cls(creds, **kw)


def is_stub(broker_type: str) -> bool:
    from brokers.stubs._stub import StubAdapter

    return issubclass(get_broker_class(broker_type), StubAdapter)
