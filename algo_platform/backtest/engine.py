"""Backtester: replays historical 1-minute spot candles through the *same* strategy class the
live engine runs, and evaluates exits with the same `evaluate_exit` rules.

Option premiums are modelled (Black-Scholes with a configurable IV, weekly expiries on a
configurable weekday) because expired option contracts are not downloadable from most
brokers. Spot-side rules (reference candles, buffer, triggers, spot SL, timed exit) are exact;
premium P&L is an estimate — the report shows spot points captured alongside it.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType, Tick
from core.clock import IST, FakeClock, combine, floor_to_timeframe, hm_or_hms, to_ist
from core.events import Category, Level
from core.options_math import next_weekly_expiries, option_premium
from core.settings_schema import Charges
from engine.charges import option_charges
from engine.exit_manager import ExitState, evaluate_exit, evaluate_partial
from engine.market_data import series_of
from strategies.base import EntrySignal, Strategy, TradeInfo
from strategies.registry import get_template

WARMUP_DAYS = 5  # earlier trading days a backtest day can read through ctx.history()


def spot_instrument(underlying: str = "NIFTY", exchange: str = "NSE") -> Instrument:
    return Instrument(exchange=Exchange(exchange), token="SPOT", tradingsymbol=f"{underlying} INDEX",
                      underlying=underlying, instrument_type=InstrumentType.INDEX)


SPOT = spot_instrument()


@dataclass
class BacktestConfig:
    template_id: str
    params: dict[str, Any]
    day_from: date
    day_to: date
    iv: float = 0.13
    lot_size: int = 65
    expiry_weekday: int = 1  # Tuesday (NIFTY weekly expiry since Sep-2025; Thursday = 3 before that)
    slippage_pct: float = 0.5  # adverse slippage on each fill, % of premium
    square_off: time = time(15, 15)
    charges: Charges = field(default_factory=Charges)
    underlying: str = "NIFTY"
    exchange: str = "NSE"  # NSE (options on NFO) or BSE (SENSEX/BANKEX, options on BFO)
    path_step_points: float = 0.5  # intra-bar price path resolution; fills happen at the first step beyond a level


@dataclass
class SimTrade:
    trade_id: str
    slot_id: str
    day: date
    direction: str
    symbol: str
    strike: float
    expiry: date
    lots: int
    qty: int
    signal_time: datetime
    entry_spot: float
    trigger: float
    entry_premium: float
    spot_sl: float | None
    exit_at: datetime
    state: ExitState
    exit_time: datetime | None = None
    exit_spot: float | None = None
    exit_premium: float | None = None
    exit_reason: str | None = None
    gross: float | None = None
    charges: float | None = None
    net: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    open_qty: int = 0
    partial_fills: list[dict[str, Any]] = field(default_factory=list)  # TP1/TP2 scale-outs

    def as_dict(self) -> dict[str, Any]:
        return {"trade_id": self.trade_id, "day": self.day.isoformat(), "slot": self.meta.get("slot_no"),
                "ref_start": self.meta.get("ref_start"), "direction": self.direction, "symbol": self.symbol,
                "strike": self.strike, "expiry": self.expiry.isoformat(), "lots": self.lots, "qty": self.qty,
                "signal_time": self.signal_time.strftime("%H:%M:%S"), "entry_spot": round(self.entry_spot, 2),
                "trigger": self.trigger, "entry_premium": round(self.entry_premium, 2), "spot_sl": self.spot_sl,
                "exit_at": self.exit_at.strftime("%H:%M:%S" if self.exit_at.second else "%H:%M"),
                "exit_time": self.exit_time.strftime("%H:%M:%S") if self.exit_time else None,
                "exit_spot": round(self.exit_spot, 2) if self.exit_spot is not None else None,
                "exit_premium": round(self.exit_premium, 2) if self.exit_premium is not None else None,
                "exit_reason": self.exit_reason, "spot_points": round(self._points(), 2),
                "partials": ", ".join(f"{p['label']}:{p['qty']}@{p['premium']:.2f}"
                                      + (f" (RSI {p['rsi']:.1f})" if p.get("rsi") is not None else "")
                                      for p in self.partial_fills),
                "rsi_in": self.meta.get("rsi_in"), "rsi_out": self.meta.get("rsi_out"),
                "regime": (self.meta.get("regime_entry") or self.meta.get("regime") or {}).get("note"),
                "regime_block": bool((self.meta.get("regime_entry") or self.meta.get("regime") or {}).get("would_block")),
                "gross": self.gross, "charges": self.charges, "net": self.net}

    def _points(self) -> float:
        if self.exit_spot is None:
            return 0.0
        d = self.exit_spot - self.entry_spot
        return d if self.direction == "CE_BUY" else -d


class BacktestContext:
    """StrategyContext implementation backed by historical candles and a modelled option chain."""

    def __init__(self, cfg: BacktestConfig, minutes: list[Candle], clock: FakeClock,
                 prior: list[Candle] | None = None) -> None:
        self.cfg = cfg
        self.prior = prior or []  # earlier days' spot candles, for indicator warm-up
        self.instance_id = None
        self.instance_name = "BACKTEST"
        self.clock = clock
        self.minutes = {c.start: c for c in minutes}
        self.spot_price: float | None = None
        self.events: list[dict[str, Any]] = []
        self.trades: list[SimTrade] = []
        self.open: SimTrade | None = None
        self.strategy: Strategy | None = None
        self.spot = spot_instrument(cfg.underlying, cfg.exchange)
        self._seq = 0

    # ---- StrategyContext
    def now(self) -> datetime:
        return self.clock.now()

    def emit(self, event_type: str, message: str, level: Level = Level.INFO, category: Category = Category.STRATEGY,
             trade_id: str | None = None, **payload: Any) -> None:
        if level.rank >= Level.INFO.rank:
            self.events.append({"time": self.now().strftime("%Y-%m-%d %H:%M:%S"), "type": event_type,
                                "message": message, "trade_id": trade_id})

    def resolve(self, key: InstrumentKey) -> Instrument:
        return self.spot

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        return next_weekly_expiries(self.now().date(), self.cfg.expiry_weekday, 3)

    def ltp(self, instrument: Instrument) -> float | None:
        return self.spot_price

    async def get_candle(self, instrument: Instrument, start: datetime, timeframe_min: int) -> Candle | None:
        bars = [self.minutes.get(start + timedelta(minutes=i)) for i in range(timeframe_min)]
        if any(b is None for b in bars):
            present = [b for b in bars if b is not None]
            if not present:
                return None
            bars = present
        return Candle(start=start, timeframe_min=timeframe_min, open=bars[0].open, high=max(b.high for b in bars),
                      low=min(b.low for b in bars), close=bars[-1].close, source="historical")

    def has_open_position(self) -> bool:
        return self.open is not None

    def square_off_time(self) -> time:
        return self.cfg.square_off

    def load_day_state(self, day: date) -> dict[str, Any]:
        return {}  # a backtest never restarts mid-day

    def save_day_state(self, day: date, state: dict[str, Any]) -> None:
        return None

    def premium(self, strike: float, expiry: date, is_call: bool) -> float:
        return option_premium(self.spot_price or strike, strike, expiry, self.now(), self.cfg.iv, is_call)

    async def submit_entry(self, signal: EntrySignal) -> TradeInfo | None:
        if self.open is not None:
            return None
        self._seq += 1
        is_call = signal.option.instrument_type is InstrumentType.CE
        prem = self.premium(signal.option.strike, signal.option.expiry, is_call) * (1 + self.cfg.slippage_pct / 100)
        qty = signal.lots * self.cfg.lot_size
        plan = signal.exit_plan
        sq = combine(self.now().date(), self.cfg.square_off)
        tid = f"B{self._seq:05d}"
        symbol = f"{self.cfg.underlying}{signal.option.expiry:%d%b%y}{signal.option.strike:g}{signal.option.instrument_type.value}".upper()
        st = ExitState(trade_db_id=self._seq, trade_ref=tid, instance_id=None, instance_name="BACKTEST", account_id=0,
                       mode="backtest", direction=signal.direction.value, spot_series=series_of(self.spot),
                       option_series="", option_key=signal.option, spot_key=None, entry_price=prem, quantity=qty,
                       exit_at=min(plan.exit_at, sq), square_off_at=sq, spot_sl=plan.spot_sl,
                       spot_sl_side=plan.spot_sl_side, tp_spot_level=plan.tp_spot_level, tp_spot_side=plan.tp_spot_side,
                       tp_premium_level=round(prem * (1 + plan.tp_premium_pct / 100), 2) if plan.tp_premium_pct else None,
                       partials=[({"label": p.label, "basis": "premium", "level": round(prem * (1 + p.premium_pct / 100), 2),
                                   "side": "above", "qty": p.lots * self.cfg.lot_size, "done": False}
                                  if p.premium_pct is not None else
                                  {"label": p.label, "basis": "spot", "level": p.spot_level, "side": p.side,
                                   "qty": p.lots * self.cfg.lot_size, "done": False}) for p in plan.partial_targets])
        trade = SimTrade(trade_id=tid, slot_id=signal.slot_id, day=self.now().date(), direction=signal.direction.value,
                         symbol=symbol, strike=signal.option.strike or 0.0, expiry=signal.option.expiry or self.now().date(),
                         lots=signal.lots, qty=qty, signal_time=self.now(), entry_spot=signal.spot,
                         trigger=signal.trigger, entry_premium=prem, spot_sl=plan.spot_sl, exit_at=st.exit_at, state=st,
                         meta=dict(signal.meta), open_qty=qty)
        snap = self._indicators()
        if snap and "rsi" in snap:
            trade.meta["rsi_in"] = snap["rsi"]
        trade.meta["protective_floor"] = (prem * (1 - plan.protective_sl_pct / 100)) if plan.protective_sl_pct else None
        self.open = trade
        self.trades.append(trade)
        self.emit("ENTRY", f"ENTRY {trade.direction} symbol={symbol} strike={trade.strike:g} qty={qty} "
                           f"spot={signal.spot:.2f} spot_SL={plan.spot_sl:.2f} exit={hm_or_hms(st.exit_at)}", trade_id=tid)
        return TradeInfo(trade_id=tid, slot_id=signal.slot_id, direction=signal.direction.value, status="OPEN",
                         entry_price=prem)

    # ---- exits
    async def history(self, instrument: Instrument, timeframe_min: int, count: int) -> list[Candle]:
        """Completed candles before now, built from earlier days' and today's 1-minute bars."""
        now = self.now()
        bars = [c for c in self.prior if c.start < now] + sorted(
            (c for c in self.minutes.values() if c.start < now), key=lambda c: c.start)
        groups: dict[datetime, list[Candle]] = {}
        for b in bars:
            groups.setdefault(floor_to_timeframe(b.start, timeframe_min), []).append(b)
        out = [Candle(start=k, timeframe_min=timeframe_min, open=g[0].open, high=max(x.high for x in g),
                      low=min(x.low for x in g), close=g[-1].close, source="historical")
               for k, g in sorted(groups.items()) if k + timedelta(minutes=timeframe_min) <= now]
        return out[-count:]

    def tighten_stop(self, trade_id: str, level: float) -> None:
        t = self.open
        if t is None or t.trade_id != trade_id or t.state.spot_sl is None:
            return
        tighter = level > t.state.spot_sl if t.state.spot_sl_side == "below" else level < t.state.spot_sl
        if tighter:
            t.state.spot_sl, t.state.sl_reason = round(level, 2), "trailing_stop"

    async def request_exit(self, trade_id: str, reason: str, partial: str | None = None) -> None:
        """Strategy-decided exit, filled at once at the modelled premium (the live engine sends a market order)."""
        t = self.open
        if t is None or t.trade_id != trade_id or self.spot_price is None:
            return
        prem = self.premium(t.strike, t.expiry, t.direction == "CE_BUY")
        out = prem * (1 - self.cfg.slippage_pct / 100)
        if partial is None:
            await self._close(t, reason, out)
            return
        idx = next((i for i, p in enumerate(t.state.partials) if p["label"] == partial and not p.get("done")), None)
        if idx is None:
            return
        self._partial(t, idx, out)
        if t.open_qty <= 0:
            await self._close(t, partial, out)

    def _partial(self, t: SimTrade, idx: int, out: float) -> None:
        p = t.state.partials[idx]
        p["done"] = True
        q = min(int(p["qty"]), t.open_qty)
        t.open_qty -= q
        snap = self._indicators()
        t.partial_fills.append({"label": p["label"], "qty": q, "premium": out, "time": self.now(),
                                "rsi": snap.get("rsi") if snap else None})
        self.emit("PARTIAL_EXIT", f"{p['label'].upper()} sold qty={q} price={out:.2f} "
                  f"pnl={(out - t.entry_premium) * q:.2f} remaining={t.open_qty}", trade_id=t.trade_id)

    async def check_exit(self) -> None:
        t = self.open
        if t is None or self.spot_price is None:
            return
        prem = self.premium(t.strike, t.expiry, t.direction == "CE_BUY")
        reason = evaluate_exit(t.state, self.spot_price, prem, self.now())
        floor = t.meta.get("protective_floor")
        if reason is None and floor and prem <= floor:
            reason = "protective_sl"
        out = prem * (1 - self.cfg.slippage_pct / 100)
        if reason is None:
            # same scale-out rule as the live exit manager: TP1, TP2 ... sell part of the position
            idx = evaluate_partial(t.state, self.spot_price, prem)
            while idx is not None and t.open_qty > 0:
                self._partial(t, idx, out)
                idx = evaluate_partial(t.state, self.spot_price, prem)
            if t.open_qty > 0:
                return
            reason = t.partial_fills[-1]["label"]
        await self._close(t, reason, out)

    def _indicators(self) -> dict[str, float] | None:
        try:
            return self.strategy.indicator_snapshot(self.spot_price) if self.strategy is not None else None
        except Exception:
            return None

    async def _close(self, t: SimTrade, reason: str, out: float) -> None:
        snap = self._indicators()
        if snap and "rsi" in snap:
            t.meta["rsi_out"] = snap["rsi"]
        sold_value = sum(p["qty"] * p["premium"] for p in t.partial_fills) + out * t.open_qty
        t.exit_time, t.exit_spot, t.exit_reason = self.now(), self.spot_price, reason
        t.exit_premium = sold_value / t.qty  # average over all exits, as the live P&L is computed
        t.open_qty = 0
        t.gross = round(sold_value - t.entry_premium * t.qty, 2)
        t.charges = option_charges(self.cfg.charges, t.entry_premium * t.qty, sold_value).total
        t.net = round(t.gross - t.charges, 2)
        self.open = None
        self.emit("EXIT", f"EXIT reason={reason} premium_in={t.entry_premium:.2f} premium_out={t.exit_premium:.2f} pnl={t.gross:.2f}",
                  trade_id=t.trade_id)
        if self.strategy is not None:
            await self.strategy.on_trade_update(TradeInfo(trade_id=t.trade_id, slot_id=t.slot_id, direction=t.direction,
                                                          status="CLOSED", exit_reason=reason, pnl=t.gross))


def _path(c: Candle) -> list[tuple[int, float]]:
    """Intra-bar anchor points (second offset, price): O → one extreme → the other → C.
    Bullish bars visit the low first, bearish bars the high first (the conservative order)."""
    if c.close >= c.open:
        return [(0, c.open), (15, c.low), (30, c.high), (50, c.close)]
    return [(0, c.open), (15, c.high), (30, c.low), (50, c.close)]


def _walk(c: Candle, step_points: float) -> list[tuple[float, float, bool]]:
    """The bar's price path as ticks (second offset, price, is_anchor), walking between the anchor
    points in steps of `step_points`. A trigger or stop crossed inside a bar therefore fills at the
    first step beyond it, as a live tick feed would, instead of at the bar's extreme."""
    anchors = _path(c)
    out: list[tuple[float, float, bool]] = [(float(anchors[0][0]), anchors[0][1], True)]
    for (t0, p0), (t1, p1) in zip(anchors, anchors[1:]):
        n = max(1, math.ceil(abs(p1 - p0) / step_points)) if step_points > 0 else 1
        for k in range(1, n + 1):
            price = p1 if k == n else round((p0 + (p1 - p0) * k / n) * 20) / 20  # 0.05 tick grid
            out.append((t0 + (t1 - t0) * k / n, price, k == n))
    return out


async def run_backtest(cfg: BacktestConfig, candles: list[Candle]) -> dict[str, Any]:
    """candles: 1-minute (or coarser) spot candles, any order, may span many days."""
    cls = get_template(cfg.template_id)
    settings = cls.settings_model.model_validate(cfg.params)
    by_day: dict[date, list[Candle]] = {}
    warmup: dict[date, list[Candle]] = {}  # days before the range: indicator warm-up only, never traded
    for c in sorted(candles, key=lambda c: c.start):
        d = to_ist(c.start).date()
        if cfg.day_from <= d <= cfg.day_to:
            by_day.setdefault(d, []).append(c)
        elif d < cfg.day_from:
            warmup.setdefault(d, []).append(c)
    history_days = dict(warmup)
    all_trades: list[SimTrade] = []
    events: list[dict[str, Any]] = []
    tf_list = []
    for day, bars in sorted(by_day.items()):
        clock = FakeClock(combine(day, time(9, 14)))
        prior = [c for d in sorted(history_days)[-WARMUP_DAYS:] for c in history_days[d]]
        ctx = BacktestContext(cfg, bars, clock, prior)
        history_days[day] = bars
        strat = cls(settings, ctx)  # type: ignore[arg-type]
        ctx.strategy = strat
        tf_list = strat.candle_timeframes()
        await strat.on_start()
        step = int(round((bars[1].start - bars[0].start).total_seconds() / 60)) if len(bars) > 1 else 1
        step = max(1, step)
        agg: dict[int, dict[datetime, list[Candle]]] = {tf: {} for tf in tf_list}

        async def due_exit(until: datetime) -> None:
            # a timed exit due before the clock moves to `until` (e.g. 09:44:58) fires at its exact second,
            # at the last price seen, as the live 250 ms exit timer does
            if ctx.open is not None and clock.now() < ctx.open.state.exit_at <= until:
                clock.set(ctx.open.state.exit_at)
                await ctx.check_exit()

        for bar in bars:
            for off, price, anchor in _walk(bar, cfg.path_step_points):
                t_next = bar.start + timedelta(seconds=off * step)
                await due_exit(t_next)
                clock.set(t_next)
                if anchor:
                    await strat.on_timer(clock.now())
                ctx.spot_price = price
                await ctx.check_exit()
                await strat.on_tick(Tick(exchange=ctx.spot.exchange, token=ctx.spot.token, ltp=price, ts=clock.now()))
                await ctx.check_exit()
            for tf in tf_list:
                b = floor_to_timeframe(bar.start, tf)
                agg[tf].setdefault(b, []).append(bar)
                if bar.start + timedelta(minutes=step) >= b + timedelta(minutes=tf):
                    grp = agg[tf][b]
                    await due_exit(b + timedelta(minutes=tf))
                    clock.set(b + timedelta(minutes=tf))
                    await strat.on_candle(ctx.spot, Candle(start=b, timeframe_min=tf, open=grp[0].open,
                                                       high=max(x.high for x in grp), low=min(x.low for x in grp),
                                                       close=grp[-1].close, source="historical"))
        # end of day: anything still open is squared off
        if ctx.open is not None:
            clock.set(combine(day, cfg.square_off))
            ctx.open.state.exit_at = clock.now()
            await ctx.check_exit()
        all_trades.extend(ctx.trades)
        events.extend(ctx.events)
    from backtest.report import summarize

    result = summarize(all_trades)
    result["trades"] = [t.as_dict() for t in all_trades]
    result["events"] = events[-3000:]
    result["days"] = len(by_day)
    result["assumptions"] = {
        "premium_model": f"Black-Scholes, IV={cfg.iv:.2%}, weekly expiry weekday={cfg.expiry_weekday} (0=Mon)",
        "lot_size": cfg.lot_size, "slippage_pct_each_side": cfg.slippage_pct,
        "intra_bar_path": (f"O → low → high → C for bullish bars (high first for bearish), walked in "
                           f"{cfg.path_step_points:g}-point steps; entries and stops fill at the first step beyond "
                           f"the level"),
        "charges": cfg.charges.model_dump(),
    }
    return result


def parse_candle_csv(text: str) -> list[Candle]:
    """CSV with columns timestamp/datetime (or date + time), open, high, low, close[, volume]."""
    rdr = csv.DictReader(io.StringIO(text.strip()))
    if rdr.fieldnames is None:
        raise ValueError("empty CSV")
    cols = {c.lower().strip(): c for c in rdr.fieldnames}

    def col(*names: str) -> str | None:
        for n in names:
            if n in cols:
                return cols[n]
        return None

    ts_c = col("timestamp", "datetime", "date_time", "time")
    d_c, t_c = col("date"), col("time")
    o_c, h_c, l_c, c_c = col("open", "o"), col("high", "h"), col("low", "l"), col("close", "c")
    v_c = col("volume", "v")
    if not (o_c and h_c and l_c and c_c) or not (ts_c or (d_c and t_c)):
        raise ValueError("CSV needs timestamp (or date + time), open, high, low, close columns")
    out = []
    for row in rdr:
        raw = row[ts_c] if ts_c and ts_c != t_c else f"{row[d_c]} {row[t_c]}"  # type: ignore[index]
        ts = _parse_ts(raw)
        out.append(Candle(start=ts, timeframe_min=1, open=float(row[o_c]), high=float(row[h_c]), low=float(row[l_c]),
                          close=float(row[c_c]), volume=float(row[v_c]) if v_c and row.get(v_c) else 0.0,
                          source="historical"))
    if not out:
        raise ValueError("CSV has no rows")
    out.sort(key=lambda c: c.start)
    if len(out) > 1:
        step = int(round((out[1].start - out[0].start).total_seconds() / 60)) or 1
        for c in out:
            c.timeframe_min = step
    return out


def _parse_ts(s: str) -> datetime:
    s = s.strip()
    try:
        return to_ist(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y%m%d %H:%M",
                "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp {s!r}")
