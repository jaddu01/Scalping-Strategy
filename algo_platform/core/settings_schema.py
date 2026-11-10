"""Global Settings model (stored as JSON in the settings table, edited on the Settings page).

Secrets (Telegram bot token) are NOT part of this model; they are stored encrypted separately
and are excluded from YAML export.
"""
from __future__ import annotations

from datetime import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from core.clock import fmt_hm


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MarketHours(_M):
    pre_open: time = Field(time(9, 0), title="Pre-open")
    market_open: time = Field(time(9, 15), title="Market open")
    market_close: time = Field(time(15, 30), title="Market close")
    final_square_off: time = Field(time(15, 15), title="Final square-off time",
                                   description="All platform positions are closed at this time")
    daily_relogin: time = Field(time(8, 45), title="Daily broker re-login time",
                                description="Accounts with auto re-login log in again at this time")

    @field_serializer("pre_open", "market_open", "market_close", "final_square_off", "daily_relogin")
    def _ser(self, v: time) -> str:
        return fmt_hm(v)

    @model_validator(mode="after")
    def _order(self) -> "MarketHours":
        if not (self.pre_open <= self.market_open < self.final_square_off <= self.market_close):
            raise ValueError("expected pre_open <= market_open < final_square_off <= market_close")
        return self


class BreachAction(str, Enum):
    BLOCK_NEW = "block_new_entries"
    SQUARE_OFF = "square_off_all"


class GlobalRisk(_M):
    max_daily_loss: float = Field(0.0, ge=0, title="Max daily loss (₹, 0 = off)")
    max_daily_profit: float = Field(0.0, ge=0, title="Max daily profit (₹, 0 = off)")
    max_open_positions: int = Field(5, ge=1, le=100, title="Max open positions (all instances)")
    on_breach: BreachAction = Field(BreachAction.SQUARE_OFF, title="On daily loss/profit breach")
    warn_at_pct: float = Field(80.0, ge=10, le=100, title="Warn at % of limit")


class Charges(_M):
    """Indian F&O (options) charge rates. Check your broker's current rate card."""
    brokerage_per_order: float = Field(20.0, ge=0, title="Brokerage per executed order (₹)")
    stt_sell_pct: float = Field(0.1, ge=0, title="STT on sell premium (%)")
    exchange_txn_pct: float = Field(0.03503, ge=0, title="Exchange transaction charge (% of premium)")
    sebi_per_crore: float = Field(10.0, ge=0, title="SEBI fee (₹ per crore)")
    stamp_buy_pct: float = Field(0.003, ge=0, title="Stamp duty on buy (%)")
    gst_pct: float = Field(18.0, ge=0, title="GST (% on brokerage + exchange + SEBI)")
    ipft_pct: float = Field(0.0005, ge=0, title="NSE IPFT (% of premium)")


class Notifications(_M):
    telegram_enabled: bool = Field(False, title="Telegram alerts enabled")
    telegram_chat_id: str = Field("", title="Telegram chat ID")
    min_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        "WARNING", title="Minimum level", description="Events at or above this level are sent")
    categories: list[Literal["SYSTEM", "MARKET", "STRATEGY", "ORDER", "POSITION", "EXIT", "RISK", "ERROR", "AUDIT"]] = Field(
        default_factory=lambda: ["SYSTEM", "STRATEGY", "ORDER", "POSITION", "EXIT", "RISK", "ERROR"],
        title="Categories to send")
    always_send: list[str] = Field(
        default_factory=lambda: ["SIGNAL", "POSITION_OPENED", "POSITION_CLOSED", "KILL_SWITCH", "RISK_BREACH",
                                 "PROTECTIVE_SL_CANCELLED_EXTERNALLY", "FEED_STALE", "INSTANCE_ERROR",
                                 "BROKER_LOGIN_FAILED"],
        title="Event types always sent (regardless of level)")


class Sync(_M):
    poll_interval_sec: float = Field(3.0, ge=1, le=60, title="Order/position backup poll interval (s)",
                                     description="Used during market hours while a live trade or order is open")
    poll_interval_flat_sec: float = Field(12.0, ge=1, le=120, title="Poll interval when flat (s)",
                                          description="During market hours with no open live trade or order")
    poll_interval_closed_sec: float = Field(60.0, ge=5, le=110, title="Poll interval outside market hours (s)",
                                            description="Before pre-open, after close + 5 min, weekends, holidays")
    rate_limit_alert_sec: float = Field(30.0, ge=5, le=600, title="Alert after broker rate-limiting for (s)",
                                        description="Single 'exceeding access rate' replies are retried quietly; "
                                                    "alert only when polls keep failing this long")
    pnl_tolerance: float = Field(5.0, ge=0, title="P&L reconciliation tolerance (₹)")
    tick_stale_sec: int = Field(15, ge=3, le=600, title="Tick feed stale after (s)")
    order_feed_stale_sec: int = Field(120, ge=10, le=3600, title="Order sync stale after (s)")
    mtm_snapshot_sec: int = Field(30, ge=5, le=600, title="MTM snapshot interval (s)")
    fill_timeout_sec: float = Field(10.0, ge=2, le=120, title="Order fill confirmation timeout (s)")
    candle_tolerance_points: float = Field(0.5, ge=0, title="Tick vs historical candle tolerance (points)")


class LoggingSettings(_M):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", title="Console log level")
    retention_days: int = Field(30, ge=1, le=3650, title="Log retention (days)")


class GeneralSettings(_M):
    timezone: Literal["Asia/Kolkata"] = Field("Asia/Kolkata", title="Timezone")
    data_feed_account_id: int | None = Field(None, title="Data feed account",
                                             description="Broker account that streams market data for all instances")
    feed_fallback: bool = Field(True, title="Fall back to another connected account if the feed drops")
    trade_on_weekends: bool = Field(False, title="Allow trading on weekends (special sessions)")


class RecorderSettings(_M):
    enabled: bool = Field(True, title="Record market ticks",
                          description="Saves every tick of the index and its ATM ± N option strikes during market "
                                      "hours, plus the strikes' real 1-minute candles after the close — for exact "
                                      "backtests and replays later")
    underlyings: list[str] = Field(default_factory=lambda: ["NIFTY"], title="Indices to record",
                                   description="Comma separated, e.g. NIFTY, BANKNIFTY, SENSEX")
    strikes_each_side: int = Field(5, ge=0, le=20, title="Strikes each side of ATM",
                                   description="5 = ATM ± 5 strikes, CE and PE (22 options); re-centred as the index moves")
    expiries: Literal["nearest", "nearest_and_next"] = Field("nearest", title="Expiries to record")
    download_option_candles: bool = Field(True, title="Download option 1-minute candles after the close",
                                          description="While the contracts still exist (expired ones cannot be "
                                                      "downloaded later)")
    retention_days: int = Field(90, ge=1, le=3650, title="Keep recordings for (days)")

    @field_validator("underlyings", mode="before")
    @classmethod
    def _split(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p.strip() for p in v.replace(";", ",").split(",")]
        return [str(x).strip().upper() for x in v if str(x).strip()]


class GlobalSettings(_M):
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    market: MarketHours = Field(default_factory=MarketHours)
    risk: GlobalRisk = Field(default_factory=GlobalRisk)
    charges: Charges = Field(default_factory=Charges)
    notifications: Notifications = Field(default_factory=Notifications)
    sync: Sync = Field(default_factory=Sync)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    recorder: RecorderSettings = Field(default_factory=RecorderSettings)


class InstanceRiskLimits(_M):
    max_daily_loss: float = Field(0.0, ge=0, title="Max daily loss (₹, 0 = off)")
    max_daily_profit: float = Field(0.0, ge=0, title="Max daily profit (₹, 0 = off)")
    max_trades_per_day: int = Field(13, ge=0, le=500, title="Max trades per day (0 = off)")
    max_lots_per_order: int = Field(20, ge=1, le=1000, title="Max lots per order")
    max_premium_per_trade: float = Field(0.0, ge=0, title="Max premium outlay per trade (₹, 0 = off)")


class AccountRiskLimits(_M):
    max_daily_loss: float = Field(0.0, ge=0, title="Max daily loss (₹, 0 = off)")
    max_open_positions: int = Field(5, ge=1, le=100, title="Max open positions")
    max_orders_per_day: int = Field(500, ge=1, le=100000, title="Max orders per day")
