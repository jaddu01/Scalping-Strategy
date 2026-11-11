"""SQLAlchemy ORM models. Datetimes are stored as timezone-aware IST values."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.clock import IST, now_ist


class ISTDateTime(TypeDecorator):
    """Stores datetimes as naive UTC-free IST wall time; returns aware IST datetimes.

    SQLite has no timezone support, so normalising everything to IST keeps comparisons and
    filters simple and correct (the platform is IST-only by design)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.astimezone(IST).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: ISTDateTime, dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin | trader | viewer
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    last_seen_at: Mapped[datetime] = mapped_column(default=now_ist)
    expires_at: Mapped[datetime] = mapped_column()
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pending_2fa: Mapped[bool] = mapped_column(Boolean, default=False)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_type: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(100), unique=True)
    client_id: Mapped[str] = mapped_column(String(64), default="")
    credentials_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_relogin: Mapped[bool] = mapped_column(Boolean, default=True)
    risk_limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32), default="DISCONNECTED")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    funds_available: Mapped[float | None] = mapped_column(Float, nullable=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    # the partner whose Angel account this is; only they (or an admin) may change or use it
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # users.id
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class SharedStrategy(Base):
    """A strategy's rules, shared by all partners. Each partner runs it as a StrategyInstance ("run") with
    their own broker account, lots, mode and risk limits; a run's params = these params + its own lots."""
    __tablename__ = "shared_strategies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    template_id: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    # who may change the rules (besides admins); admin-made strategies: admins only
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # users.id
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class StrategyInstance(Base):
    __tablename__ = "strategy_instances"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    template_id: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    broker_account_id: Mapped[int | None] = mapped_column(ForeignKey("broker_accounts.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")  # paper | live
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="STOPPED")
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_after_trade: Mapped[bool] = mapped_column(Boolean, default=False)
    # the partner who runs this strategy; it may only trade on a broker account with the same owner
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # users.id
    # the shared strategy this run follows (None: a stand-alone instance with its own rules)
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # shared_strategies.id
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_ref: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # e.g. T000123
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_instances.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("broker_accounts.id"), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    slot_id: Mapped[str] = mapped_column(String(32), default="")
    direction: Mapped[str] = mapped_column(String(8))
    exchange: Mapped[str] = mapped_column(String(8), default="NFO")
    tradingsymbol: Mapped[str] = mapped_column(String(64), default="")
    token: Mapped[str] = mapped_column(String(32), default="")
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    lots: Mapped[int] = mapped_column(Integer, default=0)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    quantity: Mapped[int] = mapped_column(Integer, default=0)  # units actually filled at entry
    open_quantity: Mapped[int] = mapped_column(Integer, default=0)
    signal_time: Mapped[datetime | None] = mapped_column(nullable=True)
    signal_spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_trigger: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_sl_side: Mapped[str] = mapped_column(String(8), default="below")
    tp_spot_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_spot_side: Mapped[str] = mapped_column(String(8), default="above")
    tp_premium_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_premium_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_time: Mapped[datetime | None] = mapped_column(nullable=True)
    protective_sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protective_sl_trigger: Mapped[float | None] = mapped_column(Float, nullable=True)
    protective_sl_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exit_trigger_spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_trigger_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING_ENTRY", index=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    charges: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_account_broker_id", "account_id", "broker_order_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    paper: Mapped[bool] = mapped_column(Boolean, default=False)
    broker_order_id: Mapped[str] = mapped_column(String(64))
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True, index=True)
    trade_ref: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_instances.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), default="EXTERNAL")  # ENTRY|PROTECTIVE_SL|EXIT|KILL|EXTERNAL
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str] = mapped_column(String(8))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(16), default="INTRADAY")
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_price: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    placed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist)
    exchange_time: Mapped[datetime | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_account_fill", "account_id", "fill_id", "broker_order_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    paper: Mapped[bool] = mapped_column(Boolean, default=False)
    fill_id: Mapped[str] = mapped_column(String(64))
    broker_order_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    time: Mapped[datetime] = mapped_column()


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_ts", "ts"), Index("ix_events_trade", "trade_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column()
    level: Mapped[str] = mapped_column(String(10), index=True)
    category: Mapped[str] = mapped_column(String(10), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=now_ist, index=True)
    username: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class Holiday(Base):
    __tablename__ = "holidays"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    description: Mapped[str] = mapped_column(String(200), default="")


class PaperState(Base):
    __tablename__ = "paper_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist, onupdate=now_ist)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=now_ist)
    username: Mapped[str] = mapped_column(String(64), default="")
    template_id: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
