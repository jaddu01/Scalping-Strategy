"""Angel One SmartAPI adapter.

- REST via `SmartApi.SmartConnect` (synchronous SDK, wrapped with asyncio.to_thread)
- Login with client code + PIN + TOTP (generated with pyotp from the stored TOTP secret)
- Tick stream via `SmartApi.smartWebSocketV2.SmartWebSocketV2` in a background thread with
  automatic reconnect + resubscribe
- Order-update stream via the SmartAPI order-update websocket (wss://tns.angelone.in/smart-order-update)
- Scrip master downloaded once per day and cached on disk
- Per-endpoint rate limiting
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pyotp
from pydantic import Field

from brokers.base import (
    BrokerAdapter,
    BrokerCredentials,
    BrokerError,
    BrokerRateLimited,
    Candle,
    Exchange,
    Fill,
    Funds,
    Instrument,
    InstrumentKey,
    InstrumentType,
    Order,
    OrderModify,
    OrderRejected,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
    Profile,
    SessionExpired,
    Side,
    Tick,
    Validity,
)
from brokers.instruments import InstrumentStore
from brokers.ratelimit import RateLimiter
from core.clock import IST, now_ist, to_ist

log = logging.getLogger("algo.broker.angelone")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
ORDER_UPDATE_WS_URL = "wss://tns.angelone.in/smart-order-update"
LOADED_SEGMENTS = {"NSE", "NFO", "BSE", "BFO"}
SESSION_ERROR_CODES = {"AG8001", "AG8002", "AG8003", "AB1010", "AB8050", "AB8051"}

# Angel exchangeType codes used by SmartWebSocketV2
WS_EXCHANGE_TYPE = {Exchange.NSE: 1, Exchange.NFO: 2, Exchange.BSE: 3, Exchange.BFO: 4, Exchange.MCX: 5,
                    Exchange.CDS: 13}
WS_EXCHANGE_FROM_CODE = {v: k for k, v in WS_EXCHANGE_TYPE.items()}

ORDER_TYPE_TO_ANGEL = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.SL: "STOPLOSS_LIMIT",
                       OrderType.SL_M: "STOPLOSS_MARKET"}
ORDER_TYPE_FROM_ANGEL = {v: k for k, v in ORDER_TYPE_TO_ANGEL.items()}
PRODUCT_TO_ANGEL = {ProductType.INTRADAY: "INTRADAY", ProductType.DELIVERY: "DELIVERY",
                    ProductType.CARRYFORWARD: "CARRYFORWARD", ProductType.MARGIN: "MARGIN"}
PRODUCT_FROM_ANGEL = {v: k for k, v in PRODUCT_TO_ANGEL.items()}
PRODUCT_FROM_ANGEL.update({"NRML": ProductType.CARRYFORWARD, "MIS": ProductType.INTRADAY,
                           "CNC": ProductType.DELIVERY})
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE",
             30: "THIRTY_MINUTE", 60: "ONE_HOUR", 1440: "ONE_DAY"}
# Max calendar days per getCandleData request, per interval (SmartAPI documented limits).
INTERVAL_MAX_DAYS = {1: 30, 3: 60, 5: 100, 10: 100, 15: 200, 30: 200, 60: 400, 1440: 2000}

STATUS_MAP = {
    "complete": OrderStatus.COMPLETE,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "trigger pending": OrderStatus.TRIGGER_PENDING,
    "open": OrderStatus.OPEN,
    "open pending": OrderStatus.OPEN,
    "validation pending": OrderStatus.OPEN,
    "put order req received": OrderStatus.OPEN,
    "modify pending": OrderStatus.OPEN,
    "modify validation pending": OrderStatus.OPEN,
    "modified": OrderStatus.OPEN,
    "cancel pending": OrderStatus.OPEN,
    "after market order req received": OrderStatus.OPEN,
    "not modified": OrderStatus.OPEN,
    "not cancelled": OrderStatus.OPEN,
}


class AngelOneCredentials(BrokerCredentials):
    client_id: str = Field(..., title="Client code", description="Angel One client code (login id)")
    api_key: str = Field(..., title="SmartAPI key", description="API key from smartapi.angelbroking.com",
                         json_schema_extra={"secret": True})
    pin: str = Field(..., title="PIN / MPIN", description="Your Angel One login PIN",
                     json_schema_extra={"secret": True})
    totp_secret: str = Field(..., title="TOTP secret",
                             description="Base32 secret shown when enabling TOTP at smartapi.angelbroking.com/enable-totp",
                             json_schema_extra={"secret": True})
    client_local_ip: str = Field("", title="Local IP (optional)", description="Sent as X-ClientLocalIP")
    client_public_ip: str = Field("", title="Static public IP",
                                  description="The static IP this account is registered with at Angel One. All of "
                                              "its API calls go out from this IP (each partner has their own); "
                                              "it is checked before every login.")
    mac_address: str = Field("", title="MAC address (optional)", description="Sent as X-MACAddress")


def _ws_proxy(public_ip: str | None) -> dict[str, Any]:
    """websocket-client arguments that send a socket through the account's IP gateway (if any)."""
    from urllib.parse import urlparse

    from brokers import egress

    proxy = egress.proxy_for(public_ip)
    if not proxy:
        return {}
    u = urlparse(proxy)
    return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}


# --------------------------------------------------------------------------- mapping helpers
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_angel_time(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    try:  # bare HH:MM:SS (trade book filltime) -> today
        t = datetime.strptime(s, "%H:%M:%S").time()
        return datetime.combine(now_ist().date(), t, tzinfo=IST)
    except ValueError:
        return None


def _exchange(value: str) -> Exchange:
    try:
        return Exchange(str(value).upper())
    except ValueError:
        return Exchange.NSE


def parse_scrip_row(row: dict[str, Any]) -> Instrument | None:
    """Convert one scrip-master row to a normalized Instrument (None for unsupported rows)."""
    seg = str(row.get("exch_seg", "")).upper()
    if seg not in LOADED_SEGMENTS:
        return None
    itype_raw = str(row.get("instrumenttype", "")).upper()
    symbol = str(row.get("symbol", ""))
    name = str(row.get("name", "")).upper()
    expiry = None
    if row.get("expiry"):
        try:
            expiry = datetime.strptime(str(row["expiry"]).upper(), "%d%b%Y").date()
        except ValueError:
            expiry = None
    if itype_raw in ("OPTIDX", "OPTSTK"):
        itype = InstrumentType.CE if symbol.endswith("CE") else InstrumentType.PE if symbol.endswith("PE") else None
        if itype is None:
            return None
    elif itype_raw in ("FUTIDX", "FUTSTK"):
        itype = InstrumentType.FUT
    elif itype_raw == "AMXIDX":
        itype = InstrumentType.INDEX
    elif itype_raw == "" and seg in ("NSE", "BSE"):
        itype = InstrumentType.EQ
    else:
        return None
    strike = _f(row.get("strike")) / 100.0 if itype in (InstrumentType.CE, InstrumentType.PE) else None
    tick = _f(row.get("tick_size")) / 100.0 or 0.05
    return Instrument.model_construct(
        exchange=Exchange(seg),
        token=str(row.get("token")),
        tradingsymbol=symbol,
        underlying=name or symbol.upper(),
        instrument_type=itype,
        expiry=expiry,
        strike=strike,
        lot_size=max(1, _i(row.get("lotsize"), 1)),
        tick_size=tick,
    )


def map_order(row: dict[str, Any]) -> Order:
    """Angel order-book / order-update row -> normalized Order."""
    status_raw = str(row.get("orderstatus") or row.get("status") or "").strip().lower()
    status = STATUS_MAP.get(status_raw, OrderStatus.UNKNOWN)
    qty = _i(row.get("quantity"))
    filled = _i(row.get("filledshares"))
    unfilled = _i(row.get("unfilledshares"), max(0, qty - filled))
    if status is OrderStatus.OPEN and filled > 0:
        status = OrderStatus.PARTIALLY_FILLED
    trig = _f(row.get("triggerprice"))
    price = _f(row.get("price"))
    return Order(
        order_id=str(row.get("orderid", "")),
        exchange=_exchange(row.get("exchange", "NFO")),
        tradingsymbol=str(row.get("tradingsymbol", "")),
        token=str(row.get("symboltoken", "")),
        side=Side.BUY if str(row.get("transactiontype", "")).upper() == "BUY" else Side.SELL,
        quantity=qty,
        filled_quantity=filled,
        pending_quantity=unfilled if not status.is_terminal else 0,
        average_price=_f(row.get("averageprice")),
        price=price or None,
        trigger_price=trig or None,
        order_type=ORDER_TYPE_FROM_ANGEL.get(str(row.get("ordertype", "")).upper(), OrderType.MARKET),
        product=PRODUCT_FROM_ANGEL.get(str(row.get("producttype", "")).upper(), ProductType.INTRADAY),
        validity=Validity.IOC if str(row.get("duration", "")).upper() == "IOC" else Validity.DAY,
        status=status,
        status_message=str(row.get("text") or ""),
        tag=(str(row.get("ordertag")) if row.get("ordertag") else None),
        exchange_order_id=(str(row.get("exchangeorderid") or row.get("exchorderid") or "") or None),
        placed_at=_parse_angel_time(row.get("exchtime")) or _parse_angel_time(row.get("updatetime")),
        updated_at=_parse_angel_time(row.get("updatetime")) or now_ist(),
        exchange_time=_parse_angel_time(row.get("exchorderupdatetime")) or _parse_angel_time(row.get("exchtime")),
        raw=row,
    )


def map_fill(row: dict[str, Any]) -> Fill:
    return Fill(
        fill_id=str(row.get("fillid", "")),
        order_id=str(row.get("orderid", "")),
        exchange=_exchange(row.get("exchange", "NFO")),
        tradingsymbol=str(row.get("tradingsymbol", "")),
        token=str(row.get("symboltoken", "")),
        side=Side.BUY if str(row.get("transactiontype", "")).upper() == "BUY" else Side.SELL,
        price=_f(row.get("fillprice")),
        quantity=_i(row.get("fillsize")),
        time=_parse_angel_time(row.get("filltime")) or now_ist(),
        product=PRODUCT_FROM_ANGEL.get(str(row.get("producttype", "")).upper(), ProductType.INTRADAY),
    )


def map_position(row: dict[str, Any]) -> Position:
    net = _i(row.get("netqty"))
    avg = _f(row.get("avgnetprice")) or _f(row.get("netprice"))
    if not avg:
        avg = _f(row.get("buyavgprice")) if net > 0 else _f(row.get("sellavgprice"))
    return Position(
        exchange=_exchange(row.get("exchange", "NFO")),
        tradingsymbol=str(row.get("tradingsymbol", "")),
        token=str(row.get("symboltoken", "")),
        product=PRODUCT_FROM_ANGEL.get(str(row.get("producttype", "")).upper(), ProductType.INTRADAY),
        net_quantity=net,
        average_price=avg,
        buy_quantity=_i(row.get("buyqty")),
        sell_quantity=_i(row.get("sellqty")),
        buy_average=_f(row.get("buyavgprice")),
        sell_average=_f(row.get("sellavgprice")),
        realized_pnl=_f(row.get("realised")),
        unrealized_pnl=_f(row.get("unrealised")),
        pnl=_f(row.get("pnl")),
        ltp=_f(row.get("ltp")),
        lot_size=max(1, _i(row.get("lotsize"), 1)),
    )


def build_order_params(req: OrderRequest) -> dict[str, Any]:
    """Normalized OrderRequest -> SmartAPI placeOrder params."""
    variety = "STOPLOSS" if req.order_type in (OrderType.SL, OrderType.SL_M) else "NORMAL"
    params: dict[str, Any] = {
        "variety": variety,
        "tradingsymbol": req.instrument.tradingsymbol,
        "symboltoken": req.instrument.token,
        "transactiontype": req.side.value,
        "exchange": req.instrument.exchange.value,
        "ordertype": ORDER_TYPE_TO_ANGEL[req.order_type],
        "producttype": PRODUCT_TO_ANGEL[req.product],
        "duration": req.validity.value,
        "price": f"{req.price:.2f}" if req.price is not None and req.order_type in (OrderType.LIMIT, OrderType.SL) else "0",
        "triggerprice": f"{req.trigger_price:.2f}" if req.trigger_price is not None else "0",
        "quantity": str(int(req.quantity)),
        "squareoff": "0",
        "stoploss": "0",
    }
    if req.tag:
        params["ordertag"] = req.tag[: AngelOneAdapter.order_tag_max_len]
    return params


def map_ws_tick(data: dict[str, Any]) -> Tick | None:
    """SmartWebSocketV2 parsed payload -> Tick (prices arrive in paise)."""
    try:
        exch = WS_EXCHANGE_FROM_CODE.get(int(data.get("exchange_type", 0)))
        if exch is None or "last_traded_price" not in data:
            return None
        ts_ms = int(data.get("exchange_timestamp") or 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=IST) if ts_ms > 0 else now_ist()
        return Tick(exchange=exch, token=str(data.get("token")), ltp=float(data["last_traded_price"]) / 100.0,
                    ts=ts, volume=_i(data.get("volume_trade_for_the_day")) or None)
    except Exception:  # malformed packet
        return None


# --------------------------------------------------------------------------- adapter
RATE_LIMIT_RETRY_SEC = 1.1
RATE_LIMIT_TEXT = ("exceeding access rate", "access denied because of exceeding", "too many requests")


def is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(t in text for t in RATE_LIMIT_TEXT)


class AngelOneAdapter(BrokerAdapter):
    broker_type: ClassVar[str] = "angelone"
    display_name: ClassVar[str] = "Angel One (SmartAPI)"
    credentials_model: ClassVar[type[BrokerCredentials]] = AngelOneCredentials
    supports_order_stream: ClassVar[bool] = True
    supports_tick_stream: ClassVar[bool] = True
    order_tag_max_len: ClassVar[int] = 20

    def __init__(self, credentials: AngelOneCredentials, **kw: Any) -> None:
        super().__init__(credentials, **kw)
        self.creds: AngelOneCredentials = credentials
        self._api: Any = None
        self._jwt: str | None = None
        self._refresh: str | None = None
        self._feed: str | None = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.store = InstrumentStore()
        self._order_variety: dict[str, str] = {}
        self._login_lock = asyncio.Lock()
        self.limiter = RateLimiter({
            "login": (1, 1.0),
            "order": (9, 1.0),     # SmartAPI: placeOrder/modify/cancel 20/s; stay well below
            "book": (1, 1.0),      # orderBook / tradeBook / position: 1/s
            "ltp": (9, 1.0),       # getMarketData / ltpData 10/s
            "candles": (3, 1.0),   # getCandleData 3/s
            "profile": (2, 1.0),
            "funds": (2, 1.0),
        })
        self._subscribed: dict[Exchange, set[str]] = {}
        self._tick_thread: threading.Thread | None = None
        self._tick_ws: Any = None
        self._tick_stop = threading.Event()
        self._tick_ready = threading.Event()
        self._order_thread: threading.Thread | None = None
        self._order_ws: Any = None
        self._order_stop = threading.Event()

    # ------------------------------------------------------------------ internals
    def _new_api(self) -> Any:
        from SmartApi import SmartConnect  # imported lazily: the SDK writes a logs/ folder on import

        from brokers import egress

        proxy = egress.proxy_for(self.creds.client_public_ip)
        api = SmartConnect(api_key=self.creds.api_key, timeout=10,
                           proxies={"http": proxy, "https": proxy} if proxy else None)
        if self.creds.client_local_ip:
            api.clientLocalIP = api.clientLocalIp = self.creds.client_local_ip
        if self.creds.client_public_ip:
            api.clientPublicIP = api.clientPublicIp = self.creds.client_public_ip
        if self.creds.mac_address:
            api.clientMacAddress = self.creds.mac_address
        _quiet_sdk_logging()
        return api

    @staticmethod
    def _check(resp: Any, what: str) -> Any:
        if resp is None:
            raise BrokerError(f"{what}: empty response", retryable=True)
        if isinstance(resp, dict):
            code = str(resp.get("errorcode") or resp.get("errorCode") or "")
            if code in SESSION_ERROR_CODES:
                raise SessionExpired(f"{what}: {resp.get('message')} ({code})", code=code, response=resp)
            if resp.get("status") is False or (resp.get("success") is False):
                raise BrokerError(f"{what}: {resp.get('message')} ({code})", code=code, response=resp,
                                  retryable=code.startswith("AB2") or code == "")
            return resp.get("data")
        return resp

    async def _call(self, bucket: str, what: str, fn_name: str, *args: Any, _retry_login: bool = True,
                    _retry_rate: bool = True) -> Any:
        if self._api is None:
            raise SessionExpired("not logged in")
        await self.limiter.acquire(bucket)
        try:
            resp = await asyncio.to_thread(getattr(self._api, fn_name), *args)
            return self._check(resp, what)
        except SessionExpired:
            if not _retry_login:
                raise
            log.warning("Angel session expired during %s; logging in again", what)
            await self.login()
            return await self._call(bucket, what, fn_name, *args, _retry_login=False)
        except BrokerError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if is_rate_limited(exc):
                # Angel occasionally refuses a call even within its documented limits: wait, retry once
                if _retry_rate:
                    await asyncio.sleep(RATE_LIMIT_RETRY_SEC)
                    return await self._call(bucket, what, fn_name, *args, _retry_login=_retry_login,
                                            _retry_rate=False)
                raise BrokerRateLimited(f"{what}: rate limited by Angel ({exc})", retryable=True) from exc
            if name == "TokenException":
                if not _retry_login:
                    raise SessionExpired(str(exc)) from exc
                await self.login()
                return await self._call(bucket, what, fn_name, *args, _retry_login=False)
            retryable = name in ("NetworkException", "DataException", "ConnectionError", "Timeout",
                                 "ReadTimeout", "ConnectTimeout")
            raise BrokerError(f"{what}: {exc}", retryable=retryable) from exc

    # ------------------------------------------------------------------ session
    async def login(self) -> Profile:
        async with self._login_lock:
            self._loop = asyncio.get_running_loop()
            await self.limiter.acquire("login")
            await self._check_egress()
            api = await asyncio.to_thread(self._new_api)
            try:
                totp = pyotp.TOTP(self.creds.totp_secret.replace(" ", "")).now()
            except Exception as exc:
                raise BrokerError(f"invalid TOTP secret: {exc}") from exc
            try:
                resp = await asyncio.to_thread(api.generateSession, self.creds.client_id, self.creds.pin, totp)
            except Exception as exc:
                self._connected = False
                raise BrokerError(f"login failed: {exc}", retryable=True) from exc
            if not resp or not resp.get("status"):
                self._connected = False
                msg = (resp or {}).get("message", "unknown error")
                raise BrokerError(f"login failed: {msg} ({(resp or {}).get('errorcode', '')})", response=resp)
            data = resp.get("data") or {}
            self._api = api
            self._jwt = data.get("jwtToken")  # already prefixed with "Bearer "
            self._refresh = data.get("refreshToken")
            self._feed = data.get("feedToken") or api.getfeedToken()
            self._connected = True
            self.last_login_at = now_ist()
            return Profile(client_id=str(data.get("clientcode", self.creds.client_id)), name=str(data.get("name", "")),
                           broker=self.broker_type, email=data.get("email"),
                           exchanges=list(data.get("exchanges") or []))

    async def _check_egress(self) -> None:
        """Refuse to log in unless this account's traffic leaves from its own static IP (see brokers/egress)."""
        from brokers import egress

        if not egress.enabled():
            return
        problem = egress.route_problem(self.creds.client_public_ip)
        if problem:
            self._connected = False
            raise BrokerError(f"IP check: {problem}")
        want = egress.expected_ip(self.creds.client_public_ip)
        try:
            got = await asyncio.to_thread(egress.check_egress, self.creds.client_public_ip)
        except Exception as exc:
            self._connected = False
            raise BrokerError(f"IP check failed (is the IP gateway running?): {exc}", retryable=True) from exc
        if got != want:
            self._connected = False
            raise BrokerError(f"IP check: traffic leaves from {got}, not this account's static IP {want}; "
                              "not logging in", retryable=True)

    async def refresh_session(self) -> None:
        if not self._api or not self._refresh:
            await self.login()
            return
        try:
            await self.limiter.acquire("login")
            resp = await asyncio.to_thread(self._api.generateToken, self._refresh)
            data = self._check(resp, "refresh") or {}
            jwt = data.get("jwtToken", "")
            self._jwt = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"
            self._feed = data.get("feedToken", self._feed)
            self._refresh = data.get("refreshToken", self._refresh)
            self._connected = True
        except Exception as exc:
            log.warning("token refresh failed (%s); doing a full login", exc)
            await self.login()

    def is_connected(self) -> bool:
        return self._connected and self._api is not None

    async def logout(self) -> None:
        if self._api:
            try:
                await asyncio.to_thread(self._api.terminateSession, self.creds.client_id)
            except Exception:
                pass
        self._connected = False

    async def close(self) -> None:
        self._tick_stop.set()
        self._order_stop.set()
        for ws in (self._tick_ws, self._order_ws):
            try:
                if ws is not None:
                    (getattr(ws, "close_connection", None) or ws.close)()
            except Exception:
                pass
        self._connected = False

    # ------------------------------------------------------------------ account
    async def get_profile(self) -> Profile:
        data = await self._call("profile", "getProfile", "getProfile", self._refresh) or {}
        return Profile(client_id=str(data.get("clientcode", self.creds.client_id)), name=str(data.get("name", "")),
                       broker=self.broker_type, email=data.get("email"), exchanges=list(data.get("exchanges") or []))

    async def get_funds(self) -> Funds:
        data = await self._call("funds", "getRMS", "rmsLimit") or {}
        return Funds(available_cash=_f(data.get("availablecash")), used_margin=_f(data.get("utiliseddebits")),
                     net=_f(data.get("net")), raw=data)

    # ------------------------------------------------------------------ instruments
    async def load_instruments(self, force: bool = False) -> int:
        cache_dir = Path(self.cache_dir or ".")
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"angel_scrip_master_{now_ist():%Y%m%d}.json"
        if force or not path.exists():
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(SCRIP_MASTER_URL)
                r.raise_for_status()
                path.write_bytes(r.content)
            for old in cache_dir.glob("angel_scrip_master_*.json"):
                if old != path:
                    old.unlink(missing_ok=True)
        rows = await asyncio.to_thread(lambda: json.loads(path.read_text()))
        self.store = await asyncio.to_thread(self._build_store, rows)
        return len(self.store)

    @staticmethod
    def _build_store(rows: list[dict[str, Any]]) -> InstrumentStore:
        store = InstrumentStore()
        for row in rows:
            inst = parse_scrip_row(row)
            if inst is not None:
                store.add(inst)
        return store

    def resolve_symbol(self, key: InstrumentKey) -> Instrument:
        return self.store.resolve(key)

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        return self.store.option_expiries(underlying, exchange)

    def find_instrument(self, exchange: Exchange, token: str) -> Instrument | None:
        return self.store.by_token(exchange, token)

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: OrderRequest) -> str:
        params = build_order_params(req)
        try:
            data = await self._call("order", "placeOrder", "placeOrderFullResponse", params)
        except BrokerError as exc:
            if isinstance(exc, SessionExpired):
                raise
            raise OrderRejected(str(exc), code=exc.code, response=exc.response, retryable=exc.retryable) from exc
        order_id = str((data or {}).get("orderid") or "")
        if not order_id:
            raise OrderRejected(f"placeOrder returned no order id: {data}")
        self._order_variety[order_id] = params["variety"]
        return order_id

    async def modify_order(self, req: OrderModify) -> str:
        variety = self._order_variety.get(req.order_id) or (
            "STOPLOSS" if req.order_type in (OrderType.SL, OrderType.SL_M) else "NORMAL")
        params = {
            "variety": variety,
            "orderid": req.order_id,
            "ordertype": ORDER_TYPE_TO_ANGEL[req.order_type],
            "producttype": PRODUCT_TO_ANGEL[req.product],
            "duration": req.validity.value,
            "price": f"{req.price:.2f}" if req.price is not None else "0",
            "triggerprice": f"{req.trigger_price:.2f}" if req.trigger_price is not None else "0",
            "quantity": str(int(req.quantity)),
            "tradingsymbol": req.instrument.tradingsymbol,
            "symboltoken": req.instrument.token,
            "exchange": req.instrument.exchange.value,
        }
        data = await self._call("order", "modifyOrder", "modifyOrder", params)
        return str((data or {}).get("orderid") or req.order_id)

    async def cancel_order(self, order_id: str) -> str:
        variety = self._order_variety.get(order_id, "NORMAL")
        data = await self._call("order", "cancelOrder", "cancelOrder", order_id, variety)
        return str((data or {}).get("orderid") or order_id)

    async def get_order_book(self) -> list[Order]:
        rows = await self._call("book", "orderBook", "orderBook") or []
        orders = [map_order(r) for r in rows]
        for r in rows:
            if r.get("orderid") and r.get("variety"):
                self._order_variety[str(r["orderid"])] = str(r["variety"])
        return orders

    async def get_trade_book(self) -> list[Fill]:
        rows = await self._call("book", "tradeBook", "tradeBook") or []
        return [map_fill(r) for r in rows]

    async def get_positions(self) -> list[Position]:
        rows = await self._call("book", "position", "position") or []
        return [map_position(r) for r in rows]

    # ------------------------------------------------------------------ market data
    async def get_ltp(self, instruments: list[Instrument]) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for i in range(0, len(instruments), 50):
            chunk = instruments[i: i + 50]
            tokens: dict[str, list[str]] = {}
            for inst in chunk:
                tokens.setdefault(inst.exchange.value, []).append(inst.token)
            data = await self._call("ltp", "getMarketData", "getMarketData", "LTP", tokens) or {}
            for row in data.get("fetched", []) or []:
                out[(str(row.get("exchange")), str(row.get("symbolToken")))] = _f(row.get("ltp"))
        return out

    async def get_historical_candles(self, instrument: Instrument, timeframe_min: int, start: datetime,
                                     end: datetime) -> list[Candle]:
        interval = INTERVALS.get(timeframe_min)
        if interval is None:
            raise BrokerError(f"unsupported timeframe {timeframe_min}m for Angel One")
        start, end = to_ist(start), to_ist(end)
        max_days = INTERVAL_MAX_DAYS[timeframe_min]
        out: list[Candle] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=max_days) - timedelta(minutes=1))
            params = {"exchange": instrument.exchange.value, "symboltoken": instrument.token, "interval": interval,
                      "fromdate": cursor.strftime("%Y-%m-%d %H:%M"), "todate": chunk_end.strftime("%Y-%m-%d %H:%M")}
            rows = await self._call("candles", "getCandleData", "getCandleData", params) or []
            for r in rows:
                ts = to_ist(datetime.fromisoformat(str(r[0])))
                out.append(Candle(start=ts, timeframe_min=timeframe_min, open=_f(r[1]), high=_f(r[2]), low=_f(r[3]),
                                  close=_f(r[4]), volume=_f(r[5]) if len(r) > 5 else 0.0, source="historical"))
            cursor = chunk_end + timedelta(minutes=1)
        out.sort(key=lambda c: c.start)
        return out

    # ------------------------------------------------------------------ tick stream
    async def subscribe_ticks(self, instruments: list[Instrument]) -> None:
        self._loop = asyncio.get_running_loop()
        new: dict[Exchange, list[str]] = {}
        for inst in instruments:
            s = self._subscribed.setdefault(inst.exchange, set())
            if inst.token not in s:
                s.add(inst.token)
                new.setdefault(inst.exchange, []).append(inst.token)
        if self._tick_thread is None or not self._tick_thread.is_alive():
            self._start_tick_thread()
        elif new and self._tick_ready.is_set():
            await asyncio.to_thread(self._ws_send_subscribe, new, True)

    async def unsubscribe_ticks(self, instruments: list[Instrument]) -> None:
        gone: dict[Exchange, list[str]] = {}
        for inst in instruments:
            s = self._subscribed.get(inst.exchange, set())
            if inst.token in s:
                s.discard(inst.token)
                gone.setdefault(inst.exchange, []).append(inst.token)
        if gone and self._tick_ready.is_set():
            await asyncio.to_thread(self._ws_send_subscribe, gone, False)

    def _ws_send_subscribe(self, tokens: dict[Exchange, list[str]], subscribe: bool) -> None:
        ws = self._tick_ws
        if ws is None or not tokens:
            return
        token_list = [{"exchangeType": WS_EXCHANGE_TYPE[e], "tokens": list(t)} for e, t in tokens.items() if t]
        try:
            if subscribe:
                ws.subscribe("algo", ws.LTP_MODE, token_list)
            else:
                ws.unsubscribe("algo", ws.LTP_MODE, token_list)
        except Exception as exc:
            log.warning("tick (un)subscribe failed: %s", exc)

    def _start_tick_thread(self) -> None:
        self._tick_stop.clear()
        self._tick_thread = threading.Thread(target=self._tick_thread_main, name=f"angel-ticks-{self.account_id}",
                                             daemon=True)
        self._tick_thread.start()

    def _post(self, fn: Any, *args: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)

    def _tick_thread_main(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        backoff = 1.0
        while not self._tick_stop.is_set():
            if not (self._jwt and self._feed):
                _time.sleep(1)
                continue
            try:
                ws = SmartWebSocketV2(self._jwt, self.creds.api_key, self.creds.client_id, self._feed,
                                      max_retry_attempt=0)
                ws.input_request_dict = {}  # class attribute in the SDK; make it per-instance
                _quiet_sdk_logging()
                self._tick_ws = ws

                def on_open(wsapp: Any) -> None:
                    nonlocal backoff
                    backoff = 1.0
                    self._tick_ready.set()
                    self._post(self._emit_connection, "ticks", "connected", "")
                    current = {e: list(t) for e, t in self._subscribed.items() if t}
                    self._ws_send_subscribe(current, True)
                    if current:
                        self._post(self._emit_connection, "ticks", "resubscribed",
                                   f"{sum(len(v) for v in current.values())} tokens")

                def on_data(wsapp: Any, message: Any) -> None:
                    if isinstance(message, dict):
                        tick = map_ws_tick(message)
                        if tick is not None:
                            self._post(self._emit_tick, tick)

                def on_error(*args: Any) -> None:
                    self._post(self._emit_connection, "ticks", "error", " ".join(str(a) for a in args))

                def on_close(*args: Any) -> None:
                    return None

                ws.on_open = on_open
                ws.on_data = on_data
                ws.on_error = on_error
                ws.on_close = on_close
                ws.connect()  # blocks until the socket closes
            except Exception as exc:
                self._post(self._emit_connection, "ticks", "error", str(exc))
            finally:
                self._tick_ready.clear()
            if self._tick_stop.is_set():
                break
            self._post(self._emit_connection, "ticks", "reconnecting", f"in {backoff:.0f}s")
            _time.sleep(backoff)
            backoff = min(30.0, backoff * 2)
        self._post(self._emit_connection, "ticks", "disconnected", "")

    # ------------------------------------------------------------------ order stream
    def on_order_update(self, cb: Any) -> None:
        super().on_order_update(cb)
        if self._order_thread is None or not self._order_thread.is_alive():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            self._order_stop.clear()
            self._order_thread = threading.Thread(target=self._order_thread_main,
                                                  name=f"angel-orders-{self.account_id}", daemon=True)
            self._order_thread.start()

    def _order_thread_main(self) -> None:
        import websocket

        backoff = 1.0
        while not self._order_stop.is_set():
            if not (self._jwt and self._feed):
                _time.sleep(1)
                continue
            headers = {"Authorization": self._jwt, "x-api-key": self.creds.api_key,
                       "x-client-code": self.creds.client_id, "x-feed-token": self._feed}

            def on_open(wsapp: Any) -> None:
                nonlocal backoff
                backoff = 1.0
                self._post(self._emit_connection, "orders", "connected", "")

            def on_message(wsapp: Any, message: Any) -> None:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "ignore")
                if not message or message in ("pong", "ping"):
                    return
                try:
                    payload = json.loads(message)
                except ValueError:
                    return
                data = payload.get("orderData") if isinstance(payload, dict) else None
                if isinstance(data, dict) and data.get("orderid"):
                    self._post(self._emit_order, map_order(data))
                    if data.get("variety"):
                        self._order_variety[str(data["orderid"])] = str(data["variety"])

            def on_error(wsapp: Any, error: Any) -> None:
                self._post(self._emit_connection, "orders", "error", str(error))

            try:
                ws = websocket.WebSocketApp(ORDER_UPDATE_WS_URL, header=headers, on_open=on_open,
                                            on_message=on_message, on_error=on_error)
                self._order_ws = ws
                ws.run_forever(sslopt={"cert_reqs": ssl.CERT_REQUIRED}, ping_interval=10, ping_timeout=8,
                               ping_payload="ping", **_ws_proxy(self.creds.client_public_ip))
            except Exception as exc:
                self._post(self._emit_connection, "orders", "error", str(exc))
            if self._order_stop.is_set():
                break
            self._post(self._emit_connection, "orders", "reconnecting", f"in {backoff:.0f}s")
            _time.sleep(backoff)
            backoff = min(30.0, backoff * 2)
        self._post(self._emit_connection, "orders", "disconnected", "")


def _quiet_sdk_logging() -> None:
    """The SDK configures logzero to write every message to ./logs; keep it to errors only."""
    try:
        import logzero

        logzero.loglevel(logging.ERROR)
    except Exception:
        pass
