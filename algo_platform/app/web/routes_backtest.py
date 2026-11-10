"""Backtest page: pick a template (or an instance's parameters), a date range and a data source
(broker historical API or CSV upload), run, and view trades, win rate, P&L, drawdown, equity curve."""
from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.deps import SessionInfo, audit, current_user, require_admin
from app.web.forms import build_fields, errors_by_field, parse_form
from app.web.render import redirect, render
from backtest.candle_cache import CandleCache
from backtest.engine import BacktestConfig, parse_candle_csv, run_backtest
from backtest.report import equity_svg
from brokers.base import Exchange, InstrumentKey, InstrumentType
from core.clock import combine, now_ist
from storage.models import BacktestRun, BrokerAccount, StrategyInstance
from storage.repo import load_global_settings
from strategies.registry import all_templates, get_template

router = APIRouter()


def _ctx(request: Request, template_id: str, params: Any, errors: dict[str, str] | None = None,
         general: list[str] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cls = get_template(template_id)
    with request.app.state.db.session() as s:
        instances = [(i.id, i.name, i.template_id) for i in s.scalars(select(StrategyInstance).order_by(StrategyInstance.name))]
        accounts = [(a.id, a.label) for a in s.scalars(select(BrokerAccount).order_by(BrokerAccount.label))]
        runs = list(s.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(15)))
    today = date.today()
    return {"templates_list": [(k, v.display_name) for k, v in all_templates().items() if not k.startswith("test_")],
            "template_id": template_id, "fields": build_fields(cls.settings_model, params, errors, "params."),
            "general_errors": general or [], "instances": instances, "accounts": accounts, "runs": runs,
            "cfg": cfg or {"day_from": (today - timedelta(days=20)).isoformat(), "day_to": today.isoformat(),
                           "iv": 13.0, "lot_size": 65, "expiry_weekday": 1, "slippage_pct": 0.5, "source": "broker"}}


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, template: str = "nifty_ref_breakout", instance_id: int | None = None,
                        user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    params: Any = get_template(template).settings_model()
    if instance_id:
        with request.app.state.db.session() as s:
            inst = s.get(StrategyInstance, instance_id)
            if inst is not None:
                template = inst.template_id
                params = get_template(template).settings_model.model_validate(inst.params or {})
    return render(request, "backtest.html", _ctx(request, template, params))


@router.post("/backtest")
async def run(request: Request, template: str = "nifty_ref_breakout", user: SessionInfo = Depends(require_admin)) -> Response:
    form = await request.form()
    cls = get_template(template)
    raw = parse_form(cls.settings_model, form, "params.")
    cfg_raw = {k: form.get(k) for k in ("day_from", "day_to", "iv", "lot_size", "expiry_weekday", "slippage_pct",
                                         "source", "account_id")}
    try:
        settings = cls.settings_model.model_validate(raw)
    except ValidationError as exc:
        e, g = errors_by_field(exc, "params.")
        return render(request, "backtest.html", _ctx(request, template, raw, e, g, cfg_raw), 400)
    try:
        d_from, d_to = date.fromisoformat(str(cfg_raw["day_from"])), date.fromisoformat(str(cfg_raw["day_to"]))
        if d_to < d_from:
            raise ValueError("end date is before start date")
        if (d_to - d_from).days > 370:
            raise ValueError("date range is limited to one year per run")
        cfg = BacktestConfig(template_id=template, params=settings.model_dump(mode="json"), day_from=d_from, day_to=d_to,
                             iv=float(cfg_raw["iv"] or 13) / 100, lot_size=int(cfg_raw["lot_size"] or 65),
                             expiry_weekday=int(cfg_raw["expiry_weekday"] or 1),
                             slippage_pct=float(cfg_raw["slippage_pct"] or 0),
                             charges=load_global_settings(request.app.state.db).charges,
                             square_off=load_global_settings(request.app.state.db).market.final_square_off,
                             underlying=getattr(settings, "underlying", "NIFTY"),
                             exchange=getattr(settings, "exchange", "NSE"))
    except (ValueError, TypeError) as exc:
        return render(request, "backtest.html", _ctx(request, template, raw, {}, [str(exc)], cfg_raw), 400)
    days_cached = days_fetched = 0
    try:
        if cfg_raw["source"] == "csv":
            upload = form.get("csv_file")
            if upload is None or not hasattr(upload, "read"):
                raise ValueError("choose a CSV file")
            candles = parse_candle_csv((await upload.read(50_000_000)).decode("utf-8", "ignore"))
        else:
            p = request.app.state.platform
            aid = int(cfg_raw["account_id"] or 0)
            acc = p.accounts.get(aid) if p else None
            if acc is None or acc.status != "CONNECTED":
                raise ValueError("pick a connected broker account for historical data (or upload a CSV)")
            spot = acc.adapter.resolve_symbol(InstrumentKey(underlying=cfg.underlying, exchange=Exchange(cfg.exchange),
                                                            instrument_type=InstrumentType.INDEX))
            cache = CandleCache(request.app.state.config.cache_dir)

            async def fetch(start, end):
                return await acc.adapter.get_historical_candles(spot, 1, start, end)

            # indicator strategies (e.g. EMA / RSI) read a few earlier days to warm up; they are not traded
            warm = getattr(get_template(template), "warmup_days", 0)
            fetch_from = d_from - timedelta(days=warm * 2 + 2) if warm else d_from
            candles, days_cached, days_fetched = await cache.get_range(
                cfg.exchange, cfg.underlying, fetch_from, d_to, now_ist().date(), fetch,
                lambda d: combine(d, time(9, 15)), lambda d: combine(d, time(15, 30)))
        if not candles:
            raise ValueError("no candles returned for that range")
        result = await run_backtest(cfg, candles)
    except ValueError as exc:
        return render(request, "backtest.html", _ctx(request, template, raw, {}, [str(exc)], cfg_raw), 400)
    except Exception as exc:  # broker/API failure
        return render(request, "backtest.html", _ctx(request, template, raw, {}, [f"backtest failed: {exc}"], cfg_raw), 500)
    cfg_store = {"day_from": d_from.isoformat(), "day_to": d_to.isoformat(), "iv": cfg.iv, "lot_size": cfg.lot_size,
                 "expiry_weekday": cfg.expiry_weekday, "slippage_pct": cfg.slippage_pct, "source": cfg_raw["source"],
                 "candles": len(candles), "underlying": cfg.underlying, "exchange": cfg.exchange,
                 "path_step_points": cfg.path_step_points, "days_cached": days_cached, "days_fetched": days_fetched}
    with request.app.state.db.session() as s:
        row = BacktestRun(username=user.username, template_id=template, params=cfg.params, config=cfg_store,
                          summary=result["summary"], result=result)
        s.add(row)
        s.flush()
        rid = row.id
    audit(request, user, "backtest_run", template, {"run_id": rid, **cfg_store})
    return redirect(f"/backtest/{rid}")


@router.get("/backtest/{rid}", response_class=HTMLResponse)
async def view(rid: int, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    with request.app.state.db.session() as s:
        row = s.get(BacktestRun, rid)
        if row is None:
            raise HTTPException(404, "backtest not found")
        s.expunge(row)
    return render(request, "backtest_result.html", {"run": row, "res": row.result,
                                                    "svg": equity_svg(row.result.get("equity", []))})



@router.get("/backtest/{rid}/candles.csv")
async def candles_csv(rid: int, request: Request, user: SessionInfo = Depends(current_user)) -> Response:
    """The cached spot candles a broker-sourced run used, in the CSV upload format."""
    with request.app.state.db.session() as s:
        row = s.get(BacktestRun, rid)
        if row is None:
            raise HTTPException(404, "backtest not found")
        cfg = dict(row.config or {})
    if cfg.get("source") != "broker":
        raise HTTPException(404, "this run used an uploaded CSV")
    und, exch = cfg.get("underlying", "NIFTY"), cfg.get("exchange", "NSE")
    text = CandleCache(request.app.state.config.cache_dir).export_range(
        exch, und, date.fromisoformat(cfg["day_from"]), date.fromisoformat(cfg["day_to"]))
    name = f"{exch}_{und}_{cfg['day_from']}_{cfg['day_to']}.csv"
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
