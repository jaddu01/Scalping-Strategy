"""Tick recorder: records the index and ATM ± N options during market hours, downloads candles after the close."""
from __future__ import annotations

import gzip
import json
from datetime import date, time

from core.clock import combine
from engine.recorder import TickRecorder, atm
from tests.conftest import DAY, at, make_candle
from tests.engine_harness import build_env


def make(env, tmp_path) -> TickRecorder:
    p = env.platform
    return TickRecorder(p.market, p.calendar, env.clock, p.bus, lambda: p.settings, tmp_path / "rec")


def rows(path):
    with gzip.open(path, "rt") as f:
        return [line.rstrip("\n").split(",") for line in f][1:]


def test_atm_rounding():
    assert atm(23674.9, 50) == 23650 and atm(23675.0, 50) == 23700 and atm(51249, 100) == 51200


async def test_records_index_and_atm_strikes_then_finishes_the_day(tmp_path):
    env = await build_env(tmp_path)
    try:
        rec = make(env, tmp_path)
        env.spot(23660.0)
        await env.settle()
        await rec.step()
        # NIFTY index + ATM 23650 ± 5 strikes, CE and PE, nearest expiry = 23 instruments
        assert len(rec.keys) == 23 and rec.centres["NIFTY"] == 23650
        strikes = sorted({v["strike"] for v in rec.instruments.values() if v["strike"]})
        assert strikes[0] == 23400 and strikes[-1] == 23900 and len(strikes) == 11
        assert {v["expiry"] for v in rec.instruments.values() if v["type"] in ("CE", "PE")} == {DAY.isoformat()}
        env.spot(23662.5)
        env.option_price(23650, "CE", 101.5)
        env.option_price(23700, "PE", 88.0)
        await env.settle()
        await rec.step()
        folder = tmp_path / "rec" / DAY.isoformat()
        got = rows(folder / "ticks.csv.gz")
        assert ["NFO:NIFTY:CE:%s:23650" % DAY.isoformat(), "101.5"] in [r[1:3] for r in got]
        assert any(r[1].startswith("NSE:NIFTY:INDEX") and r[2] == "23662.5" for r in got)
        # the index moves 100 points: strikes are re-centred (new ones added, old ones kept)
        env.spot(23760.0)
        await env.settle()
        await rec.step()
        assert rec.centres["NIFTY"] == 23750 and len(rec.keys) == 23 + 2 * 2
        inst = json.loads((folder / "instruments.json").read_text())
        assert len(inst) == len(rec.keys) and all("lot_size" in v and "token" in v for v in inst.values())
        # after the close: candles downloaded, manifest written, subscriptions released
        env.mock.history[at("15:29")] = make_candle(at("15:29"), 1, 2, 0.5, 1.5, tf=1)
        env.clock.set(at("15:40", 1))
        await rec.step()
        man = json.loads((folder / "manifest.json").read_text())
        assert man["ticks"] >= 3 and man["instruments"] == 27 and man["candle_rows"] >= 1
        assert (folder / "candles_1m.csv.gz").exists() and rec.keys == {} and rec.day is None
        assert env.types("RECORDER_DAY_DONE")
        assert rec.days()[0]["day"] == DAY.isoformat() and rec.days()[0]["complete"]
    finally:
        await env.platform.stop()


async def test_never_records_a_simulator_or_outside_hours(tmp_path):
    env = await build_env(tmp_path)
    try:
        rec = make(env, tmp_path)
        env.spot(23660.0)
        env.mock.is_paper = True  # a simulated feed
        await rec.step()
        assert rec.day is None and not rec.keys
        env.mock.is_paper = False
        env.clock.set(at("09:00"))  # before the open
        await rec.step()
        assert rec.day is None
        env.clock.set(combine(date(2026, 11, 7), time(10, 0)))  # Saturday
        await rec.step()
        assert rec.day is None
        env.platform.settings.recorder.enabled = False
        env.clock.set(at("10:00"))
        await rec.step()
        assert rec.day is None
    finally:
        await env.platform.stop()


async def test_restart_after_close_still_downloads_and_old_days_are_deleted(tmp_path):
    env = await build_env(tmp_path)
    try:
        rec = make(env, tmp_path)
        env.spot(23660.0)
        await env.settle()
        await rec.step()
        rec.flush()
        # app restarted at 15:38: a fresh recorder finishes the day from instruments.json
        rec2 = make(env, tmp_path)
        env.clock.set(at("15:41"))
        await rec2.step()
        assert (tmp_path / "rec" / DAY.isoformat() / "manifest.json").exists()
        old = tmp_path / "rec" / "2026-01-01"
        old.mkdir(parents=True)
        rec2.cleanup(DAY)
        assert not old.exists() and (tmp_path / "rec" / DAY.isoformat()).exists()
    finally:
        await env.platform.stop()


def test_recordings_panel_and_download(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.config import AppConfig
    from storage.crypto import Crypto
    from tests.test_auth import login, setup_admin

    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key())
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()
    with TestClient(create_app(cfg, start_engine=True), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        login(c)
        day = tmp_path / "recordings" / "2026-11-11"
        day.mkdir(parents=True)
        (day / "manifest.json").write_text(json.dumps({"ticks": 1234, "instruments": 23, "candle_rows": 8000}))
        with gzip.open(day / "ticks.csv.gz", "wt") as f:
            f.write("ts_ms,series,ltp,volume\n1,NSE:NIFTY:INDEX:-:-,25000,\n")
        html = c.get("/backtest").text
        assert "Recorded market data" in html and "2026-11-11" in html and "1234" in html
        r = c.get("/recordings/2026-11-11/ticks.csv.gz")
        assert r.status_code == 200 and gzip.decompress(r.content).startswith(b"ts_ms")
        assert c.get("/recordings/2026-11-11/..%2F..%2Fw.db").status_code == 404
        assert c.get("/recordings/notaday/manifest.json").status_code == 404
