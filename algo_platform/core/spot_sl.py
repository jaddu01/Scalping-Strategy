"""Spot stop-loss arithmetic shared by the engine's live view and the web pages (pure, unit tested)."""
from __future__ import annotations

from typing import Any


def points_in_favour(direction: str | None, entry: float | None, now: float | None) -> float | None:
    """Index points in the trade's favour: CE = now − entry, PE = entry − now."""
    if entry is None or now is None or direction not in ("CE_BUY", "PE_BUY"):
        return None
    return round((now - entry) if direction == "CE_BUY" else (entry - now), 2)


def sl_view(direction: str | None, entry_spot: float | None, sl: float | None, side: str | None,
            spot: float | None) -> dict[str, Any]:
    """The spot stop-loss of a trade and, given the current spot, how far away it still is.
    sl_risk = entry spot → SL in points; sl_left = current spot → SL (≤ 0: the SL level is reached);
    sl_left_pct = sl_left as % of sl_risk (100 at entry, 0 at the SL, above 100 in profit);
    running = points in the trade's favour now."""
    if sl is None:
        return {"sl": None}
    side = side or ("below" if direction == "CE_BUY" else "above")
    risk = round(abs(entry_spot - sl), 2) if entry_spot is not None else None
    left = None if spot is None else round(spot - sl if side == "below" else sl - spot, 2)
    pct = round(100 * left / risk) if left is not None and risk else None
    return {"sl": sl, "sl_side": side, "sl_risk": risk, "sl_left": left, "sl_left_pct": pct,
            "running": points_in_favour(direction, entry_spot, spot)}
