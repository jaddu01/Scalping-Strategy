"""Backtest statistics and an inline SVG equity curve (no external chart library)."""
from __future__ import annotations

from collections import defaultdict
from html import escape
from typing import Any


def summarize(trades: list[Any]) -> dict[str, Any]:
    closed = [t for t in trades if t.net is not None]
    nets = [t.net for t in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    equity, peak, max_dd = [], 0.0, 0.0
    running = 0.0
    for t in closed:
        running += t.net
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        equity.append({"t": f"{t.day.isoformat()} {t.exit_time:%H:%M}" if t.exit_time else t.day.isoformat(),
                       "equity": round(running, 2)})
    per_day: dict[str, float] = defaultdict(float)
    reasons: dict[str, int] = defaultdict(int)
    for t in closed:
        per_day[t.day.isoformat()] += t.net
        reasons[t.exit_reason or "-"] += 1
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "summary": {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(100 * len(wins) / len(closed), 2) if closed else 0.0,
            "gross_pnl": round(sum(t.gross for t in closed), 2),
            "charges": round(sum(t.charges for t in closed), 2),
            "net_pnl": round(sum(nets), 2),
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(max_dd, 2),
            "spot_points": round(sum(t.as_dict()["spot_points"] for t in closed), 2),
            "best_day": max(per_day.values()) if per_day else 0.0,
            "worst_day": min(per_day.values()) if per_day else 0.0,
        },
        "equity": equity,
        "per_day": [{"day": d, "net": round(v, 2)} for d, v in sorted(per_day.items())],
        "exit_reasons": dict(reasons),
    }


def equity_svg(points: list[dict[str, Any]], width: int = 900, height: int = 260) -> str:
    if not points:
        return '<p class="muted">No closed trades.</p>'
    pad_l, pad_r, pad_t, pad_b = 64, 12, 12, 28
    vals = [0.0] + [p["equity"] for p in points]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(vals) - 1 or 1

    def x(i: int) -> float:
        return pad_l + w * i / n

    def y(v: float) -> float:
        return pad_t + h * (hi - v) / (hi - lo)

    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = path + f" L{x(len(vals) - 1):.1f},{y(0):.1f} L{x(0):.1f},{y(0):.1f} Z"
    ticks = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        ticks.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end" class="axis">{v:,.0f}</text>')
    first, last = escape(points[0]["t"]), escape(points[-1]["t"])
    final = points[-1]["equity"]
    cls = "pos" if final >= 0 else "neg"
    return (f'<svg viewBox="0 0 {width} {height}" class="equity" role="img" aria-label="Equity curve">'
            + "".join(ticks)
            + f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>'
            + f'<path d="{area}" class="area {cls}"/><path d="{path}" class="line {cls}"/>'
            + f'<text x="{pad_l}" y="{height - 8}" class="axis">{first}</text>'
            + f'<text x="{width - pad_r}" y="{height - 8}" text-anchor="end" class="axis">{last}</text></svg>')
