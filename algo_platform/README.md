# Algo Platform — multi-strategy, multi-broker algo trading for Indian markets

A self-hosted Python platform that runs several strategy **instances** across several **broker
accounts**, with a secure web UI. It ships with:

- **Strategy:** index 5-minute reference-candle breakout (buys the ATM option). It works for NSE
  indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY; options on NFO) and BSE indices (SENSEX, BANKEX;
  options on BFO).
- **Brokers:** Angel One (SmartAPI) fully implemented, a Paper broker (simulated fills on live
  prices, with an optional simulated market for demos), and documented stubs for Zerodha, Upstox,
  Dhan and Fyers.

**Stack:** Python 3.11+ · FastAPI · Jinja2 templates + HTMX with a small vanilla-JS WebSocket client
(no React build step) · SQLAlchemy 2 + Alembic · SQLite by default, PostgreSQL optional · asyncio throughout ·
all times Asia/Kolkata.

> ⚠️ Trading derivatives carries substantial risk. Run everything in **paper mode** first, use small
> size when you go live, and keep watching the broker's own terminal. This software is provided as is.

---

## 1. Strategy rules (as implemented)

**NIFTY 5-minute Reference Candle Breakout — ATM option buying** (`strategies/nifty_ref_breakout.py`)

> Full walkthrough with timeline, worked example, settings and caveats: [docs/strategies/nifty_ref_breakout.md](docs/strategies/nifty_ref_breakout.md)

| Rule | Implementation |
|---|---|
| Reference candles | Only from the fixed schedule: 09:45, 10:00, 10:15, 10:45, 11:15, 11:45, 12:10, 12:45, 13:15, 13:45, 14:10, 14:35, 14:55 (editable). They are never picked dynamically. |
| Candle | NIFTY **spot** 5-minute candle, read `candle_finalization_delay_sec` (5 s) after it closes |
| Triggers | `long_trigger = ref_high + buffer_points`, `short_trigger = ref_low − buffer_points` (buffer 2.25) |
| Entry | `spot > long_trigger` → **CE_BUY** ATM call; `spot < short_trigger` → **PE_BUY** ATM put (strictly beyond the trigger) |
| Entry window | Reference close → reference close + `entry_window_candles` × 5 min (2 candles = 10 min), and never at or after the hard exit |
| Hard exit | Reference **start** + `exit_offset_min` (15): 09:45 → 10:00, 12:10 → 12:25, 14:55 → 15:10 |
| ATM strike | Spot at signal, rounded to the nearest `strike_step` (50); halves round up |
| Expiry | Nearest non-expired weekly from the broker's instrument master (an expiry-day contract counts) |
| Quantity | `lots` (5) × lot size **read from the broker's instrument master** |
| Spot stop-loss | Managed by the platform, fixed for the whole trade: CE exits if `spot ≤ ref_low`; PE exits if `spot ≥ ref_high` |
| Take-profit | `none` (default) · `spot_points` · `r_multiple` (multiple of entry spot − spot SL) · `premium_pct` |
| Disaster stop | **Optional, off by default** (`protective_sl_pct` = 0; it is not in the strategy rules). When set, an SL-M SELL goes to the broker that far below the fill price; if the broker rejects SL-M, the platform falls back to SL-limit. A wide value such as 70% gives crash protection without cutting normal trades. |
| Exits | Whichever comes first: spot SL, TP, hard exit (at exactly reference start + 15 min), final square-off, protective SL at broker, manual exit at broker, kill switch. All other pending exit orders are cancelled. |
| One trade per slot | First breakout only, no re-entry after SL, slot skipped if no breakout, one open position per instance |

### Other indices (SENSEX, BANKNIFTY, …)
Create one instance per index and set:

| Setting | NIFTY | BANKNIFTY | SENSEX |
|---|---|---|---|
| Underlying index | `NIFTY` | `BANKNIFTY` | `SENSEX` |
| Index exchange | NSE | NSE | **BSE** (options are bought on BFO) |
| Strike step | 50 | 100 | 100 |
| Buffer points | 2.25 | larger (backtest it) | larger, ~3.3× NIFTY's (backtest it) |

Lot size and expiries always come from the broker's instrument master. BANKNIFTY, FINNIFTY and
MIDCPNIFTY have monthly expiries only, so "nearest weekly" picks the nearest (monthly) contract.
SENSEX weeklies expire on Thursday. The form rejects mismatches such as SENSEX with exchange NSE.
The paper broker's simulated market streams both NIFTY and SENSEX.

The five bugs from the previous build are covered by tests in `tests/test_strategy_rules.py` and
`tests/test_engine_flow.py`: buffer applied; exit at +15 (not +20); fixed schedule only;
labels CE_BUY/PE_BUY (not CALL_CREDIT/PUT_CREDIT); 5 lots (not 1).

### Assumptions
1. **Entry-window end:** entries stop at whichever is earlier, ref close + 10 min or the hard exit.
   With the defaults both are ref start + 15.
2. **Late start:** if the app starts after a slot's hard exit, that slot is marked `MISSED`. If it starts
   inside an entry window, the reference candle is taken from the broker's historical API and the slot is
   still traded.
3. **Reference-candle source:** the tick-built candle is cross-checked against the broker's historical
   candle. On a mismatch (default tolerance 0.5 points) the broker's candle wins and a
   `CANDLE_MISMATCH` warning is logged. If historical data isn't available yet, a complete tick-built
   candle is used once it is 8 s old.
4. **`candle_close` trigger:** a 5-minute candle must close beyond the trigger. A candle that closes
   exactly at the hard exit can't trigger an entry, so with the defaults only the first candle after
   the reference can.
5. **Spot SL uses the reference high/low without the buffer**, exactly as specified.
6. **Signals blocked by risk:** a breakout rejected by risk or another check still counts as that
   slot's first breakout, so the slot is skipped rather than retried.
7. **Final square-off** (default 15:15, Global Settings) caps every trade's exit time.
8. **Protective-stop fill** (`protective_sl` exit reason): if the broker-side SL fills first, the
   trade closes with reason `protective_sl`.
9. **Disabling with "Exit position now"** closes the trade with reason `instance_disabled`.
10. **Product type** is INTRADAY (MIS); quantities are in units sent as lot multiples.
11. **Daily risk limits** use gross P&L (realised + live MTM), which matches the broker's screen.

---

## 2. Architecture

```
algo_platform/
├── app/            FastAPI app: main.py, auth/ (argon2, sessions, CSRF, TOTP), web/ (pages + templates), api/ (JSON + /ws)
├── brokers/        base.py (interface + normalized models), angelone.py, paper.py, instruments.py, ratelimit.py, stubs/
├── strategies/     base.py (plugin interface), registry.py, nifty_ref_breakout.py
├── engine/         platform.py (root), strategy_manager.py, market_data.py, candles.py, accounts.py (broker sync),
│                   order_manager.py, trades.py (trade lifecycle), exit_manager.py, risk_manager.py, charges.py, calendar.py
├── backtest/       engine.py (replays the same strategy class), report.py (stats + SVG equity curve)
├── storage/        db.py, models.py, crypto.py (Fernet), repo.py, event_sink.py, migrations/ (Alembic)
├── notify/         telegram.py
├── core/           config, clock (IST), structured events, secret masking, settings schema, Black-Scholes
├── tests/          106 pytest tests
└── run.py          starts the web app + engine in one process
```

### Data flow
```
broker tick stream ──► MarketDataService (one subscription per symbol, shared by all instances;
                       fallback to another account if the feed goes stale; candle builder)
        │
        ▼
InstanceRuntime (one isolated asyncio task per enabled instance) ──► Strategy (on_tick / on_timer / on_candle)
        │  EntrySignal (option key, lots, exit plan)
        ▼
TradeManager ──► RiskManager: global → broker account → instance ──► OrderManager ──► the instance's own
        │                                                                             broker adapter (or its paper shadow)
        ▼
BrokerSync (order-update websocket + backup polling of order book / trade book / positions)
        │  order status, fills, positions  (the broker is the source of truth)
        ▼
TradeManager (fills, protective SL, manual exits, P&L reconciliation) ──► ExitManager (spot SL / TP / time)
        │
        ▼
EventBus ──► DB + rotating JSONL file + console + Telegram + browser (WebSocket, ~1 s)
```

**Key properties**
- **Instance isolation:** an exception in one strategy marks only that instance as `ERROR` and sends
  an alert. Its open position keeps being managed by the ExitManager, which doesn't depend on the
  strategy task.
- **Order tags** `I{instance}T{trade}{E|S|X|K}` (entry / protective SL / exit / kill) map every broker
  order back to its instance and trade. Untagged orders show as **External / manual**.
- **Manual exit at the broker:** if the broker position shrinks without a platform exit (confirmed on
  two polls), the trade is closed with `manual_exit_at_broker`, the remaining exit orders are cancelled,
  and the slot is not re-entered. If the protective SL is cancelled at the broker, a CRITICAL alert is raised.
- **Safe retries:** after a network error, the order book is checked for our tag before re-sending,
  so an order is never placed twice.
- **Restart reconciliation:** on startup each account logs in and syncs orders, fills and positions,
  then reconciles every open trade before its instances start. Each trade is resumed, re-protected,
  or closed with the correct reason.
- **P&L:** MTM = (LTP − avg) × qty on every tick. Realised P&L uses actual fills from the trade book.
  Platform P&L is compared with the broker's position P&L every poll; a difference beyond the
  tolerance shows a ⚠ badge and logs `PNL_MISMATCH`. Gross P&L and estimated net (after the charges
  configured in Global Settings) are shown per trade, instance, account and in total.

---

## 3. Setup

```bash
cd algo_platform
python3.12 -m venv .venv && source .venv/bin/activate      # Python 3.11+
pip install -r requirements.txt

cp .env.example .env
python run.py genkey            # prints a master key
# paste it into .env as ALGO_MASTER_KEY=...   (keep an offline backup: without it the
#                                             stored broker credentials cannot be decrypted)
python run.py                   # http://127.0.0.1:8000
pytest                          # run the test suite
```

### Master encryption key
Broker credentials (API key, PIN, TOTP secret), app users' TOTP secrets and the Telegram bot token
are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The key comes **only** from the environment
variable `ALGO_MASTER_KEY` and is never written to the database. Secrets are never displayed after
saving (only masked) and are masked in every log and event payload.

### First run
Open `http://127.0.0.1:8000`. You're redirected to **/setup** to create the admin user. Then:
1. **Profile → Set up 2FA** (optional): app-login TOTP via any authenticator app.
2. **Users** (admin only): add Viewer (read-only) or Admin users. New users must change their
   temporary password at first login.
3. Sessions are HttpOnly, SameSite=Strict cookies. They are Secure over HTTPS (`ALGO_COOKIE_SECURE`),
   CSRF-protected, expire after 30 idle minutes / 12 hours, and are rate-limited per IP. An account
   locks for 15 minutes after 5 failed logins. If an admin is locked out:
   `python run.py reset-password admin`.

### Try it without a broker
**Broker accounts → Add account → Paper trading**, tick **Simulate market data**, save. That account
streams a synthetic NIFTY feed and option chain and becomes the data feed. Create an instance on it in
paper mode and enable it. The schedule only runs during market hours (09:15–15:30 IST on trading days).

### Adding broker accounts
**Broker accounts → Add account**, choose the broker. The form is generated from the adapter's
credential schema. Use **Test connection** to verify the login, funds and status. **Use as data feed**
chooses the account that streams market data for every instance (also in Global Settings).
**Auto re-login** logs the account in again each trading day at 08:45 (configurable).

### Angel One (SmartAPI) API key + TOTP
1. Sign in at **smartapi.angelbroking.com** with your Angel One client code and create an app
   (type: Trading API). Copy the **API key**.
2. Enable TOTP at **smartapi.angelbroking.com/enable-totp**. When the QR code is shown, copy the
   **secret text** (base32, e.g. `JBSWY3DP…`). The platform generates the 6-digit codes itself with
   `pyotp`. You can also scan the same QR into your phone app.
3. In the platform enter: client code, API key, **PIN** (MPIN), TOTP secret. Optionally enter your
   **static public IP** and local IP/MAC (sent as SmartAPI headers).
4. **Test connection.** The scrip master is downloaded once a day (cached under `data/cache`) and
   lot sizes and tick sizes are read from it.

### Before going live — check your broker's current API rules
SEBI's retail algorithmic-trading framework (circular of Feb-2025, phased in by the exchanges and
brokers) and broker policies change. **Before trading live, check with your broker:**
- **Static IP registration** for API order placement, and whether orders from other IPs are rejected.
- **Algo identification / registration:** whether your strategy needs an exchange-issued algo ID or
  must be registered through the broker, and any limits on orders per second for unregistered algos.
- Whether **SL-M orders are allowed for options** (some brokers block market/SL-M in F&O; the
  platform falls back to SL-limit, but confirm the behaviour).
- Session/token validity, mandatory daily logout and re-login rules, and API rate limits (the Angel
  adapter throttles to ~9 orders/s, 1 book call/s, 3 candle calls/s).
- Current **charges** (brokerage, STT, exchange, GST, SEBI, stamp duty): update them in
  **Global Settings → Charges** so the net P&L estimates are right.

### Exposing the UI remotely (HTTPS)
> **Deploying to a cloud server:** follow [docs/DEPLOY.md](docs/DEPLOY.md). Scripts in `deploy/` set up the
> service, HTTPS (Caddy), firewall and backups on Ubuntu 24.04 in two commands.

The server binds to **127.0.0.1** by default. Don't expose it directly. Put it behind a reverse
proxy that terminates TLS, for example Caddy:

```
algo.example.com {
    reverse_proxy 127.0.0.1:8000
}
```
or nginx (WebSocket upgrade required for live updates):
```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
Then set `ALGO_TRUST_PROXY_HEADERS=true` (so the client IP and HTTPS are detected correctly), turn on
2FA for every user, and ideally restrict access by IP or VPN.

### PostgreSQL
`pip install "psycopg[binary]"` and set
`ALGO_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/algo`. Migrations run automatically at
startup (`python run.py migrate` runs them manually).

---

## 4. Using the platform

- **Dashboard** — instances (status: Running / Waiting / In trade / Stopping / Stopped / Error) with
  on/off toggles, mode, broker, and today's trades and P&L. Open positions show spot SL distance, the
  protective SL order at the broker (id, trigger, status), take-profit, a countdown to the timed exit,
  MTM vs broker P&L, and a mismatch badge. Also: positions not opened by the platform, account health
  dots (green/amber/red, last tick, last order sync), and the **global kill switch**.
- **Disabling an instance with an open position** asks whether to *Exit position now* or *Let the
  current trade finish, then stop*. **Enabling a live instance** asks for confirmation.
- **Parameter edits** on a running instance are queued and applied at the next idle moment (between
  slots), never mid-trade.
- **Kill switches:** per instance (exit + disable), per broker account (cancel all orders, exit all
  positions including manual ones, disable its instances, block new entries until reset), and global
  (all accounts).
- **Trades & Orders** — filters, CSV export, daily summary per instance. Click a trade for its
  **timeline**: reference candle → signal → entry order → fill → SL order → exit trigger → exit fill
  → P&L, with the latency between steps.
- **Logs** — live stream plus history with filters (date, level, category, event type, instance,
  broker, trade id, text). Export to CSV/JSON. Events are also written to `data/logs/events.jsonl`
  (rotated daily, retention configurable).
- **Backtest** — replays historical 1-minute spot candles (from a connected broker account or a CSV
  upload) through the **same strategy class**, using the same exit rules. Option premiums are modelled
  with Black-Scholes (configurable IV, weekly expiry weekday, slippage) because expired contracts
  can't be downloaded. Spot rules are exact; premium P&L is an estimate. Inside each 1-minute bar the
  price is walked in 0.5-point steps, so entries and stops fill at the first price beyond the level
  (as a live tick feed would), not at the bar's high/low. Broker candles are cached per day under
  `data/cache/candles/`, so re-runs use identical data; each run has a "download candles (CSV)" link.
- **Global settings** — market hours, final square-off, NSE holidays (edit or import CSV/YAML), data
  feed, global risk (max daily loss/profit, max open positions, action on breach), charges, Telegram
  (bot token, chat id, **Send test message**, categories/level/always-send events), sync/staleness
  thresholds, logging. **Export/Import YAML** excludes all secrets.
- **Audit log** — every login (success/failure), settings change (old → new), toggle, kill switch,
  account and user change, with user, IP and timestamp.

> The NSE holiday list ships empty. Import the official list for the year (NSE website →
> Resources → Exchange Communication → Holidays) in Global Settings.

---

## 5. Adding a new strategy plugin

1. Create `strategies/my_strategy.py`:

```python
from typing import ClassVar
from pydantic import Field
from brokers.base import Exchange, InstrumentKey, InstrumentType
from strategies.base import Strategy, StrategySettings, EntrySignal, ExitPlan, Direction
from strategies.registry import register

class MySettings(StrategySettings):
    lots: int = Field(1, ge=1, le=50, title="Lots", description="Lots per trade")
    threshold: float = Field(20.0, ge=0, title="Threshold (points)")

@register
class MyStrategy(Strategy):
    template_id: ClassVar[str] = "my_strategy"
    display_name: ClassVar[str] = "My strategy"
    settings_model: ClassVar[type[StrategySettings]] = MySettings

    def subscriptions(self):
        return [InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)]

    async def on_tick(self, tick):
        ...  # decide, then:
        # info = await self.ctx.submit_entry(EntrySignal(..., exit_plan=ExitPlan(exit_at=..., spot_sl=...)))

    def is_idle(self) -> bool:      # when new parameters may be applied
        return not self.ctx.has_open_position()
```

2. Import it in `strategies/registry.py::_load_builtin` (or anywhere at startup).
3. It appears in **Strategies → New instance**, with a form generated from `MySettings` (types,
   defaults, min/max, descriptions, inline validation errors). The backtester runs it unchanged.

Strategies never call brokers directly. The engine resolves symbols, runs risk checks, places and
confirms orders, and manages exits from the `ExitPlan`.

## 6. Adding a new broker adapter

1. Copy a stub from `brokers/stubs/` (e.g. `zerodha.py`), or start from `brokers/angelone.py`.
2. Declare the credential schema as a `BrokerCredentials` model. Mark secrets with
   `json_schema_extra={"secret": True}`; they are encrypted and masked automatically, and the account
   form is generated from the schema.
3. Implement every method of `BrokerAdapter` (`brokers/base.py`):
   `login, refresh_session, is_connected, get_profile, get_funds, load_instruments, resolve_symbol,
   option_expiries, find_instrument, place_order, modify_order, cancel_order, get_order_book,
   get_trade_book, get_positions, get_ltp, get_historical_candles, subscribe_ticks, unsubscribe_ticks`,
   plus push ticks with `self._emit_tick(...)`, order updates with `self._emit_order(...)` and stream
   state with `self._emit_connection(...)`.
   - Map everything to and from the normalized models (`Order`, `Position`, `Instrument`, `Tick`,
     `Candle`, `Fill`). Raise `OrderRejected`, `SessionExpired` or `BrokerError(retryable=...)`.
   - Wrap synchronous SDKs with `asyncio.to_thread`. Deliver callbacks on the event loop
     (`loop.call_soon_threadsafe`).
   - Put the instance/trade tag in the broker's order-tag field (respect `order_tag_max_len`).
   - Throttle with `brokers/ratelimit.RateLimiter` according to the broker's limits.
4. Register the class in `brokers/registry.py::BROKERS`.
5. Add it to the contract suite in `tests/test_adapters.py` (`FACTORIES`), and add mapping tests
   with a fake SDK as done for Angel One.

---

## 7. Tests

`pytest` (106 tests, ~13 s) covers:
- **Strategy rules** — buffer, fixed slot schedule and exit times, ATM strike, spot SL, TP modes,
  one trade per slot, no re-entry, skip on no breakout, candle-close mode, late start, parameter
  changes only between slots, restore after restart, log format.
- **Settings validation** — strategy, global, risk and broker credential schemas.
- **Credential encryption & masking.**
- **Login/auth** — setup, argon2, secure cookie, CSRF, lockout, rate limit, TOTP, viewer role,
  idle expiry, audit.
- **Engine end-to-end with a mock broker** — entry → fill → protective SL → spot-SL / timed exit →
  P&L; protective SL filled or cancelled at the broker; manual exit at the broker; enable/disable while
  in trade (both choices); kill switches; instance isolation; restart reconciliation.
- **Adapter contract tests** (mock broker + paper simulator), Angel One mapping and session-expiry
  re-login with a fake `SmartConnect`, and stubs raising `NotImplementedError`.
- **Candles, reference-candle cross-check, risk limits, backtester, and a full web UI flow**
  (forms, toggles, every page, WebSocket, YAML round trip, CSV backtest).
