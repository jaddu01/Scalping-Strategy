"""Credential encryption, secret masking, global/broker settings validation, holiday parsing, charges."""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from brokers.angelone import AngelOneCredentials
from core.events import Category, EventBus
from core.masking import mask_secrets, mask_value
from core.settings_schema import GlobalSettings, InstanceRiskLimits
from engine.calendar import MarketCalendar, parse_holiday_file
from engine.charges import option_charges
from storage.crypto import Crypto, MasterKeyError, masked_credentials


def test_encrypt_decrypt_roundtrip():
    c = Crypto(Crypto.generate_key())
    secret = {"api_key": "abc123", "pin": "4321", "totp_secret": "JBSWY3DPEHPK3PXP"}
    token = c.encrypt_json(secret)
    assert "abc123" not in token and "4321" not in token
    assert c.decrypt_json(token) == secret


def test_wrong_key_cannot_decrypt():
    token = Crypto(Crypto.generate_key()).encrypt("hello")
    with pytest.raises(MasterKeyError):
        Crypto(Crypto.generate_key()).decrypt(token)


def test_missing_or_bad_master_key(monkeypatch):
    monkeypatch.delenv("ALGO_MASTER_KEY", raising=False)
    with pytest.raises(MasterKeyError):
        Crypto()
    with pytest.raises(MasterKeyError):
        Crypto("not-a-key")


def test_masking_for_display_and_logs():
    assert mask_value("ABCDEFGHIJ") == "AB••••••IJ"
    shown = masked_credentials({"client_id": "A123", "api_key": "SECRETKEY99"}, {"api_key"})
    assert shown["client_id"] == "A123" and "SECRETKEY" not in shown["api_key"]
    payload = {"clientcode": "A1", "password": "1234", "totp": "123456", "jwtToken": "x", "symboltoken": "26000",
               "nested": {"api_key": "k", "refreshToken": "r"}, "msg": "Authorization: Bearer eyJabcdefghij.abcdefghij.abcdefghij"}
    m = mask_secrets(payload)
    assert m["clientcode"] == "A1" and m["symboltoken"] == "26000"
    assert m["password"] == m["totp"] == m["jwtToken"] == "•••"
    assert m["nested"] == {"api_key": "•••", "refreshToken": "•••"}
    assert "eyJabcdefghij" not in m["msg"]


def test_event_bus_masks_payloads():
    bus = EventBus()
    seen = []
    bus.add_sink(seen.append)
    bus.emit(Category.ORDER, "REQUEST", "token=26000", api_key="SECRET", pin="1111", token="26000")
    assert seen[0].payload == {"api_key": "•••", "pin": "•••", "token": "26000"}


def test_broker_credentials_schema_marks_secrets():
    assert AngelOneCredentials.secret_fields() == {"api_key", "pin", "totp_secret"}
    with pytest.raises(ValidationError):
        AngelOneCredentials(client_id="A1")  # missing required secrets


def test_global_settings_defaults_and_validation():
    g = GlobalSettings()
    assert g.general.timezone == "Asia/Kolkata"
    assert g.market.final_square_off.strftime("%H:%M") == "15:15"
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"market": {"final_square_off": "15:45"}})  # after close
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"risk": {"max_daily_loss": -5}})
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        InstanceRiskLimits(max_lots_per_order=0)
    dumped = g.model_dump(mode="json")
    assert dumped["market"]["market_open"] == "09:15"
    assert GlobalSettings.model_validate(dumped) == g


def test_holiday_parsing_and_calendar():
    csv_text = "date,description\n2026-01-26,Republic Day\n15-Aug-2026,Independence Day\n"
    h = parse_holiday_file(csv_text)
    assert h == {date(2026, 1, 26): "Republic Day", date(2026, 8, 15): "Independence Day"}
    assert parse_holiday_file("- date: 2026-03-03\n  description: Holi\n") == {date(2026, 3, 3): "Holi"}
    cal = MarketCalendar(GlobalSettings(), h)
    assert not cal.is_trading_day(date(2026, 1, 26))
    assert not cal.is_trading_day(date(2026, 11, 7))  # Saturday
    assert cal.is_trading_day(date(2026, 11, 5))
    with pytest.raises(ValueError):
        parse_holiday_file("not a date, x")


def test_option_charges():
    g = GlobalSettings()
    ch = option_charges(g.charges, buy_value=100 * 325, sell_value=120 * 325)
    assert ch.brokerage == 40.0
    assert ch.stt == pytest.approx(120 * 325 * 0.001, abs=0.01)
    assert ch.stamp == pytest.approx(100 * 325 * 0.00003, abs=0.01)
    assert ch.gst == pytest.approx((ch.brokerage + ch.exchange + ch.sebi + ch.ipft) * 0.18, abs=0.02)
    assert 40 < ch.total < 120
