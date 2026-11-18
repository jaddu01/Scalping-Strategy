"""Admin-only market data download through the platform's logged-in data-feed account (no second broker login).

Built for studies on instruments the platform does not trade, e.g. MCX Gold Mini futures:
  GET /api/admin/symbols?exchange=MCX&q=GOLDM            -> Angel's matching contracts (JSON)
  GET /api/admin/history.csv?exchange=MCX&symbol=GOLDM&tf=5&days=100[&contract=GOLDM05DEC26FUT]
      -> candles of that futures contract (default: the nearest one not yet expired) as CSV; a copy is also
         saved under data/exports/ on the server.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth.deps import SessionInfo, audit, require_admin
from brokers.base import Exchange, Instrument, InstrumentType

router = APIRouter(prefix="/api/admin")
FUT = re.compile(r"^(?P<name>[A-Z0-9]+?)(?P<dd>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})FUT$")


def _feed_adapter(request: Request) -> Any:
    p = request.app.state.platform
    if p is None or p.market.feed_account_id is None:
        raise HTTPException(503, "no data feed account is connected")
    acc = p.accounts.get(p.market.feed_account_id)
    adapter = getattr(acc, "adapter", None)
    if adapter is None or not hasattr(adapter, "search_symbols"):
        raise HTTPException(400, "the data feed account's broker has no symbol search (Angel One only)")
    return adapter


def futures_contracts(rows: list[dict[str, Any]], name: str, today: date) -> list[tuple[date, str, str]]:
    """(expiry, tradingsymbol, token) of `name`'s futures not yet expired, nearest first."""
    out = []
    for r in rows:
        m = FUT.match(str(r.get("tradingsymbol", "")).upper())
        if not m or m["name"] != name.upper():
            continue
        try:
            exp = datetime.strptime(f"{m['dd']}{m['mon']}{m['yy']}", "%d%b%y").date()
        except ValueError:
            continue
        if exp >= today:
            out.append((exp, str(r["tradingsymbol"]), str(r["symboltoken"])))
    return sorted(out)


@router.get("/symbols")
async def symbols(request: Request, exchange: str = "MCX", q: str = Query(..., min_length=2),
                  user: SessionInfo = Depends(require_admin)) -> dict[str, Any]:
    return {"results": await _feed_adapter(request).search_symbols(exchange, q)}


@router.get("/history.csv")
async def history_csv(request: Request, exchange: str = "MCX", symbol: str = Query(..., min_length=2),
                      contract: str | None = None, tf: int = Query(5, ge=1, le=1440),
                      days: int = Query(100, ge=1, le=400),
                      user: SessionInfo = Depends(require_admin)) -> Response:
    adapter = _feed_adapter(request)
    try:
        exch = Exchange(exchange.upper())
    except ValueError:
        raise HTTPException(400, f"unknown exchange {exchange}") from None
    p = request.app.state.platform
    now = p.clock.now()
    rows = await adapter.search_symbols(exch.value, symbol)
    found = futures_contracts(rows, symbol, now.date())
    if contract:
        found = [f for f in found if f[1].upper() == contract.upper()]
    if not found:
        raise HTTPException(404, f"no live futures contract for {symbol} on {exch.value}"
                                 + (f" named {contract}" if contract else ""))
    exp, tsym, token = found[0]
    inst = Instrument(exchange=exch, token=token, tradingsymbol=tsym, underlying=symbol.upper(),
                      instrument_type=InstrumentType.FUT, expiry=exp)
    candles = await adapter.get_historical_candles(inst, tf, now - timedelta(days=days), now)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for c in candles:
        w.writerow([c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume])
    name = f"{exch.value}_{tsym}_{tf}m_{now:%Y%m%d}.csv"
    try:
        out_dir = p.config.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(buf.getvalue())
    except Exception:
        pass  # the download still works
    audit(request, user, "history_download", tsym, {"timeframe": tf, "days": days, "candles": len(candles)})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})
