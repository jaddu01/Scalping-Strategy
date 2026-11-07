"""Estimated statutory charges for an options round trip (rates from Global Settings)."""
from __future__ import annotations

from dataclasses import dataclass

from core.settings_schema import Charges


@dataclass
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    ipft: float
    gst: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.ipft + self.gst, 2)

    def as_dict(self) -> dict[str, float]:
        return {"brokerage": self.brokerage, "stt": self.stt, "exchange": self.exchange, "sebi": self.sebi,
                "stamp": self.stamp, "ipft": self.ipft, "gst": self.gst, "total": self.total}


def option_charges(rates: Charges, buy_value: float, sell_value: float, buy_orders: int = 1,
                   sell_orders: int = 1) -> ChargeBreakdown:
    """buy_value / sell_value are premium turnover in rupees (price × quantity)."""
    turnover = buy_value + sell_value
    executed_orders = (1 if buy_value > 0 else 0) * buy_orders + (1 if sell_value > 0 else 0) * sell_orders
    brokerage = rates.brokerage_per_order * executed_orders
    stt = sell_value * rates.stt_sell_pct / 100
    exchange = turnover * rates.exchange_txn_pct / 100
    sebi = turnover * rates.sebi_per_crore / 1e7
    stamp = buy_value * rates.stamp_buy_pct / 100
    ipft = turnover * rates.ipft_pct / 100
    gst = (brokerage + exchange + sebi + ipft) * rates.gst_pct / 100
    r = lambda x: round(x, 2)  # noqa: E731
    return ChargeBreakdown(r(brokerage), r(stt), r(exchange), r(sebi), r(stamp), r(ipft), r(gst))
