"""Today's slots with each slot's trade: entry / exit spot, points captured and P&L (strategy page)."""
from __future__ import annotations

from typing import Any

from core.spot_sl import points_in_favour as _pts
from core.spot_sl import sl_view

SL_REASONS = ("spot_stop_loss", "trailing_stop", "protective_sl")


def slot_trade_view(trade: dict[str, Any] | None, live: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure (unit tested). `trade`: the stored trade's fields; `live`: for an open trade, the current spot,
    premium and open quantity. Points are lot-weighted over the TP1 / TP2 legs and the final exit."""
    if not trade:
        return {}
    d, entry_spot, qty = trade.get("direction"), trade.get("signal_spot"), trade.get("quantity") or 0
    lot = trade.get("lot_size") or 1
    entry_prem = trade.get("entry_price")
    legs: list[dict[str, Any]] = []
    sold = 0
    realized = 0.0
    for p in trade.get("partials") or []:
        if not p.get("filled"):
            continue
        sold += p["filled"]
        if entry_prem is not None and p.get("price") is not None:
            realized += (p["price"] - entry_prem) * p["filled"]
        legs.append({"label": p["label"].upper(), "lots": p["filled"] // lot, "qty": p["filled"],
                     "spot": p.get("spot"), "points": _pts(d, entry_spot, p.get("spot")), "premium": p.get("price")})
    rest = max(0, qty - sold)
    is_open = trade.get("status") in ("OPEN", "EXITING", "PENDING_ENTRY")
    if is_open and live:
        exit_spot, exit_prem = live.get("spot"), live.get("premium")
        pnl = realized + ((exit_prem - entry_prem) * live.get("open_qty", rest)
                          if exit_prem is not None and entry_prem is not None else 0.0)
        net = None
    else:
        exit_spot = trade.get("exit_trigger_spot")
        if exit_spot is None and rest == 0 and legs:
            exit_spot = legs[-1]["spot"]  # all lots sold at the targets
        exit_prem, pnl, net = trade.get("exit_price"), trade.get("gross_pnl"), trade.get("net_pnl")
    final_pts = _pts(d, entry_spot, exit_spot)
    if legs and qty:
        known = [(leg["points"], leg["qty"]) for leg in legs] + ([(final_pts, rest)] if rest else [])
        points = (round(sum(p * q for p, q in known) / qty, 2)
                  if all(p is not None for p, _ in known) else None)
    else:
        points = final_pts
    live_now = bool(is_open and live)
    src = {**trade, **{k: v for k, v in (live or {}).items() if k.startswith("spot_sl")}} if live_now else trade
    # the live trade's stop (a trailed stop has moved); distance only while the trade is open
    sl = sl_view(d, entry_spot, src.get("spot_sl"), src.get("spot_sl_side"), exit_spot if live_now else None)
    sl["sl_hit"] = (not is_open) and trade.get("exit_reason") in SL_REASONS
    return {**sl, "entry_spot": entry_spot, "exit_spot": exit_spot, "points": points, "legs": legs,
            "entry_premium": entry_prem, "exit_premium": exit_prem, "pnl": round(pnl, 2) if pnl is not None else None,
            "net": net, "live": live_now, "exit_time": trade.get("exit_time"),
            "status": trade.get("status")}
