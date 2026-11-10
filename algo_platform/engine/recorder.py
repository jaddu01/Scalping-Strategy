"""Market tick recorder: the raw material for exact backtests and replays.

On every trading day, from a minute before the open to 5 minutes after the close, it records every tick
of each configured index (NIFTY by default) and of its ATM ± N CE / PE strikes on the nearest weekly
expiry (re-centred as the index moves), straight from the live data-feed account. After the close it
downloads the real 1-minute candles of every recorded option while the contracts still exist (expired
contracts can never be downloaded later) and writes a manifest.

Files, one folder per day:  <data_dir>/recordings/YYYY-MM-DD/
  ticks.csv.gz         ts_ms,series,ltp,volume              (appended as ticks arrive, flushed every second)
  instruments.json     series -> symbol, token, exchange, type, expiry, strike, lot size, tick size
  candles_1m.csv.gz    series,timestamp,open,high,low,close,volume   (after the close)
  manifest.json        counts, sizes, first / last tick, what was downloaded
The recorder only listens: it never places orders and never changes what strategies see.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from brokers.base import Exchange, Instrument, InstrumentKey, InstrumentType, Tick
from core.clock import Clock, combine, fmt_hm
from core.events import Category, EventBus, Level
from core.settings_schema import GlobalSettings
from engine.calendar import MarketCalendar
from engine.market_data import MarketDataService, series_of

log = logging.getLogger("algo.recorder")

STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100}
BSE_INDICES = {"SENSEX", "BANKEX", "SENSEX50"}


def atm(spot: float, step: int) -> float:
    return float(int(spot / step + 0.5) * step)


class TickRecorder:
    def __init__(self, market: MarketDataService, calendar: MarketCalendar, clock: Clock, bus: EventBus,
                 settings: Callable[[], GlobalSettings], root: Path) -> None:
        self.market, self.calendar, self.clock, self.bus, self.settings = market, calendar, clock, bus, settings
        self.root = Path(root)
        self.day: date | None = None
        self.instruments: dict[str, dict[str, Any]] = {}  # series -> description
        self.keys: dict[str, InstrumentKey] = {}  # series -> key (subscribed by the recorder)
        self.centres: dict[str, float] = {}  # underlying -> ATM strike currently covered
        self.buffer: list[str] = []
        self.ticks_today = 0
        self.first_tick: datetime | None = None
        self.last_tick: datetime | None = None
        self.closed_days: set[date] = set()
        self.failed: set[str] = set()
        self._task: asyncio.Task | None = None
        self._busy = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="tick-recorder")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.flush()
        await self._unsubscribe_all()

    def _emit(self, event_type: str, message: str, level: Level = Level.INFO, **payload: Any) -> None:
        self.bus.emit(Category.SYSTEM, event_type, message, level, **payload)

    def day_dir(self, d: date) -> Path:
        return self.root / d.isoformat()

    # ------------------------------------------------------------------ schedule
    def window(self, d: date) -> tuple[datetime, datetime, datetime]:
        """(start recording, stop recording, download candles at)."""
        m = self.settings().market
        open_at = combine(d, m.market_open) - timedelta(minutes=1)
        close_at = combine(d, m.market_close) + timedelta(minutes=5)
        return open_at, close_at, close_at + timedelta(minutes=5)

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("recorder step failed")
            await asyncio.sleep(1.0)

    async def step(self) -> None:
        """One scheduler pass (every second): open the day, keep strikes centred, flush, close the day."""
        cfg = self.settings().recorder
        now = self.clock.now()
        d = now.date()
        if self.day is not None and self.day != d:
            await self._close_day(self.day, download=False)
        if not cfg.enabled or not self.calendar.is_trading_day(d):
            return
        start, stop, download_at = self.window(d)
        if start <= now < stop:
            feed = self.market.feed
            if feed is None or getattr(feed, "is_paper", False):
                return  # only record a real broker's data, never a simulator
            if self.day != d:
                self._open_day(d)
            if not self._busy:
                self._busy = True
                try:
                    await self._ensure_subscriptions(now)
                finally:
                    self._busy = False
            self.flush()
        elif now >= download_at and d not in self.closed_days and (self.day == d or self._unfinished(d)):
            if self.day != d:  # restarted after the close: finish the day from its saved instrument list
                self._load_instruments(d)
            await self._close_day(d, download=cfg.download_option_candles)
        if now.hour == 0 and now.minute == 30 and now.second < 2:
            self.cleanup(d)

    def _unfinished(self, d: date) -> bool:
        folder = self.day_dir(d)
        return (folder / "instruments.json").exists() and not (folder / "manifest.json").exists()

    def _load_instruments(self, d: date) -> None:
        try:
            self.instruments = json.loads((self.day_dir(d) / "instruments.json").read_text())
        except Exception:
            self.instruments = {}

    def _open_day(self, d: date) -> None:
        self.day = d
        self.instruments, self.keys, self.centres, self.buffer, self.failed = {}, {}, {}, [], set()
        self.ticks_today, self.first_tick, self.last_tick = 0, None, None
        folder = self.day_dir(d)
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / "instruments.json"
        if old.exists():  # restarted mid-day: keep appending to the same files
            try:
                self.instruments = json.loads(old.read_text())
            except Exception:
                self.instruments = {}
        self._emit("RECORDER_STARTED", f"recording ticks for {d.isoformat()} into {folder}", Level.INFO, day=str(d))
        self.cleanup(d)

    # ------------------------------------------------------------------ subscriptions
    async def _ensure_subscriptions(self, now: datetime) -> None:
        cfg = self.settings().recorder
        for und in cfg.underlyings:
            exch = Exchange.BSE if und in BSE_INDICES else Exchange.NSE
            opt_exch = Exchange.BFO if und in BSE_INDICES else Exchange.NFO
            spot_key = InstrumentKey(underlying=und, exchange=exch, instrument_type=InstrumentType.INDEX)
            await self._add(spot_key)
            spot = self.market.ltp(spot_key)
            if spot is None:
                spot = await self.market.fetch_ltp(spot_key)
            if spot is None:
                continue
            step = STRIKE_STEP.get(und, 50)
            centre = atm(spot, step)
            if self.centres.get(und) == centre:
                continue
            self.centres[und] = centre
            expiries = sorted(e for e in self.market.option_expiries(und, opt_exch) if e >= now.date())
            expiries = expiries[:2] if cfg.expiries == "nearest_and_next" else expiries[:1]
            n = cfg.strikes_each_side
            for exp in expiries:
                for k in range(-n, n + 1):
                    for typ in (InstrumentType.CE, InstrumentType.PE):
                        await self._add(InstrumentKey(underlying=und, exchange=opt_exch, instrument_type=typ,
                                                      expiry=exp, strike=centre + k * step))
        self._save_instruments()

    async def _add(self, key: InstrumentKey) -> None:
        series = series_of(key)
        if series in self.keys or series in self.failed:
            return
        try:
            inst = await self.market.subscribe(key, self._on_tick)
        except Exception as exc:
            self.failed.add(series)
            log.debug("recorder: cannot subscribe %s: %s", series, exc)
            return
        if inst is None:
            return
        self.keys[series] = key
        self.instruments[series] = self._describe(inst)

    @staticmethod
    def _describe(inst: Instrument) -> dict[str, Any]:
        return {"symbol": inst.tradingsymbol, "token": inst.token, "exchange": inst.exchange.value,
                "underlying": inst.underlying, "type": inst.instrument_type.value,
                "expiry": inst.expiry.isoformat() if inst.expiry else None, "strike": inst.strike,
                "lot_size": inst.lot_size, "tick_size": inst.tick_size}

    async def _unsubscribe_all(self) -> None:
        for key in list(self.keys.values()):
            try:
                await self.market.unsubscribe(key, self._on_tick)
            except Exception:
                pass
        self.keys = {}

    # ------------------------------------------------------------------ ticks
    def _on_tick(self, series: str, tick: Tick) -> None:
        if self.day is None or series not in self.keys:
            return
        ts = tick.ts or self.clock.now()
        self.buffer.append(f"{int(ts.timestamp() * 1000)},{series},{tick.ltp},{tick.volume if tick.volume is not None else ''}\n")
        self.ticks_today += 1
        self.first_tick = self.first_tick or ts
        self.last_tick = ts

    def flush(self) -> None:
        if not self.buffer or self.day is None:
            return
        lines, self.buffer = self.buffer, []
        path = self.day_dir(self.day) / "ticks.csv.gz"
        new = not path.exists()
        try:
            with gzip.open(path, "at", encoding="utf-8") as f:  # each flush appends a gzip member
                if new:
                    f.write("ts_ms,series,ltp,volume\n")
                f.writelines(lines)
        except Exception as exc:
            self.buffer = lines + self.buffer  # try again next second
            log.warning("recorder: could not write ticks: %s", exc)

    def _save_instruments(self) -> None:
        if self.day is None:
            return
        try:
            (self.day_dir(self.day) / "instruments.json").write_text(json.dumps(self.instruments, indent=1))
        except Exception as exc:
            log.warning("recorder: could not write instruments: %s", exc)

    # ------------------------------------------------------------------ end of day
    async def _close_day(self, d: date, download: bool) -> None:
        self.flush()
        self._save_instruments()
        candles = 0
        missing: list[str] = []
        if download:
            candles, missing = await self.download_candles(d)
        await self._unsubscribe_all()
        folder = self.day_dir(d)
        ticks = folder / "ticks.csv.gz"
        manifest = {"day": d.isoformat(), "ticks": self.ticks_today, "instruments": len(self.instruments),
                    "first_tick": self.first_tick.isoformat() if self.first_tick else None,
                    "last_tick": self.last_tick.isoformat() if self.last_tick else None,
                    "ticks_file_bytes": ticks.stat().st_size if ticks.exists() else 0,
                    "candle_rows": candles, "candles_missing": missing,
                    "written_at": self.clock.now().isoformat()}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=1))
        except Exception as exc:
            log.warning("recorder: could not write manifest: %s", exc)
        self.closed_days.add(d)
        self.day = None
        self._emit("RECORDER_DAY_DONE",
                   f"{d.isoformat()}: {manifest['ticks']} ticks on {manifest['instruments']} instruments "
                   f"({manifest['ticks_file_bytes'] / 1e6:.1f} MB), {candles} option candle rows"
                   + (f", {len(missing)} without candles" if missing else ""), Level.INFO, **manifest)

    async def download_candles(self, d: date) -> tuple[int, list[str]]:
        """Real 1-minute candles of every recorded instrument (while the contracts still exist)."""
        feed = self.market.feed
        if feed is None:
            return 0, list(self.instruments)
        start, end = combine(d, self.settings().market.market_open), combine(d, self.settings().market.market_close)
        rows, missing = [], []
        for series, info in self.instruments.items():
            try:
                key = self.keys.get(series) or InstrumentKey(
                    underlying=info["underlying"], exchange=Exchange(info["exchange"]),
                    instrument_type=InstrumentType(info["type"]),
                    expiry=date.fromisoformat(info["expiry"]) if info.get("expiry") else None, strike=info.get("strike"))
                inst = feed.resolve_symbol(key)
                for c in await feed.get_historical_candles(inst, 1, start, end):
                    rows.append(f"{series},{c.start.isoformat()},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
            except Exception as exc:
                missing.append(series)
                log.debug("recorder: candles for %s failed: %s", series, exc)
        if rows:
            with gzip.open(self.day_dir(d) / "candles_1m.csv.gz", "wt", encoding="utf-8") as f:
                f.write("series,timestamp,open,high,low,close,volume\n")
                f.writelines(rows)
        return len(rows), missing

    def cleanup(self, today: date) -> None:
        keep = self.settings().recorder.retention_days
        if not self.root.exists():
            return
        for p in self.root.iterdir():
            try:
                d = date.fromisoformat(p.name)
            except ValueError:
                continue
            if (today - d).days > keep:
                shutil.rmtree(p, ignore_errors=True)

    # ------------------------------------------------------------------ UI
    def status(self) -> dict[str, Any]:
        return {"recording": self.day is not None, "day": self.day.isoformat() if self.day else None,
                "instruments": len(self.keys), "ticks_today": self.ticks_today,
                "last_tick": fmt_hm(self.last_tick) if self.last_tick else None}

    def days(self) -> list[dict[str, Any]]:
        out = []
        if not self.root.exists():
            return out
        for p in sorted(self.root.iterdir(), reverse=True):
            try:
                date.fromisoformat(p.name)
            except ValueError:
                continue
            man: dict[str, Any] = {}
            if (p / "manifest.json").exists():
                try:
                    man = json.loads((p / "manifest.json").read_text())
                except Exception:
                    man = {}
            files = {f.name: f.stat().st_size for f in p.iterdir() if f.is_file()}
            out.append({"day": p.name, "files": files, "ticks": man.get("ticks"),
                        "instruments": man.get("instruments"), "candle_rows": man.get("candle_rows"),
                        "complete": bool(man)})
        return out
