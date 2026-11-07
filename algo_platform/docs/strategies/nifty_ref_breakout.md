# Index 5-min Reference Candle Breakout (`nifty_ref_breakout`)

ATM option buying on an index breakout. The strategy waits for a fixed-time 5-minute index candle (the
**reference candle**). If spot then breaks out of that candle's range by a small buffer, it buys the ATM call
(upside) or ATM put (downside). A fixed stop on the **index spot** (the other end of the reference candle) guards
the trade, and anything still open is closed at **exactly** 15 minutes after the reference candle started.

| | |
|---|---|
| Template id | `nifty_ref_breakout` (the name is kept for compatibility; the strategy works on any index) |
| Code | [`strategies/nifty_ref_breakout.py`](../../strategies/nifty_ref_breakout.py) (rules), [`engine/trades.py`](../../engine/trades.py) (orders), [`engine/exit_manager.py`](../../engine/exit_manager.py) (exits) |
| Tests | [`tests/test_strategy_rules.py`](../../tests/test_strategy_rules.py), [`tests/test_engine_flow.py`](../../tests/test_engine_flow.py), [`tests/test_sensex.py`](../../tests/test_sensex.py) |
| Instruments | NSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY → options on NFO) and BSE indices (SENSEX, BANKEX → options on BFO) |
| Direction | Long options only (buys CE or PE, never sells options) |
| Product | Intraday (MIS); every position is flat by the end of the day |

---

## 1. Daily timeline

The day is split into **13 independent slots**. Each slot has one reference candle and can produce at most one trade.

```
 ref start         ref close          entry window ends / hard exit
    |---- 5 min ------|--------- 10 min (2 candles) ---------|
    09:45           09:50                                 10:00
    [reference candle] [ watch for breakout, then manage trade ]
```

With the defaults (5-min candle, 2-candle entry window, exit 15 minutes after the reference start), every slot
looks like this:

| Slot | Reference candle | Entry window | Hard exit |
|---:|---|---|---|
| 1 | 09:45 – 09:50 | 09:50 – 10:00 | 10:00 |
| 2 | 10:00 – 10:05 | 10:05 – 10:15 | 10:15 |
| 3 | 10:15 – 10:20 | 10:20 – 10:30 | 10:30 |
| 4 | 10:45 – 10:50 | 10:50 – 11:00 | 11:00 |
| 5 | 11:15 – 11:20 | 11:20 – 11:30 | 11:30 |
| 6 | 11:45 – 11:50 | 11:50 – 12:00 | 12:00 |
| 7 | 12:10 – 12:15 | 12:15 – 12:25 | 12:25 |
| 8 | 12:45 – 12:50 | 12:50 – 13:00 | 13:00 |
| 9 | 13:15 – 13:20 | 13:20 – 13:30 | 13:30 |
| 10 | 13:45 – 13:50 | 13:50 – 14:00 | 14:00 |
| 11 | 14:10 – 14:15 | 14:15 – 14:25 | 14:25 |
| 12 | 14:35 – 14:40 | 14:40 – 14:50 | 14:50 |
| 13 | 14:55 – 15:00 | 15:00 – 15:10 | 15:10 |

Reference candles are **only** taken from this schedule; the strategy never picks one on its own. The global
**final square-off** (15:15 by default, under Settings → Market) closes anything still open, whatever the slot says.

The entry window is `ref close + entry_window_candles × timeframe`, but it never runs past the hard exit. With the
defaults the two end at the same moment, so a trade can be entered any time in the 10 minutes after the
reference candle closes.

---

## 2. What happens in one slot (step by step)

Each slot moves through these states (shown on the dashboard's slot table):

```
PENDING → AWAITING_CANDLE → WATCHING → ENTERING → IN_TRADE → DONE
                │                │          │
                └→ SKIPPED       └→ SKIPPED └→ SKIPPED / DONE (entry failed)
MISSED = the platform was started after this slot had already finished
```

### Step 1 — Read the reference candle
- At ref close + `candle_finalization_delay_sec` (5 s), the slot becomes `AWAITING_CANDLE`.
- The candle comes from the **index spot** (e.g. Nifty 50, not futures or options).
- Two sources are used: a candle the platform builds from live ticks, and the broker's historical candle API.
  **The broker's candle wins** if they disagree by more than `candle_tolerance_points` (logged as `CANDLE_MISMATCH`).
  If the historical API is slow, the tick-built candle is accepted after a few seconds.
- If no candle is available by the end of the entry window, the slot is skipped (`no_reference_candle`).

### Step 2 — Compute the triggers
```
long_trigger  = ref_high + buffer_points      (default buffer 2.25)
short_trigger = ref_low  − buffer_points
```
The slot becomes `WATCHING` and a `REF_CANDLE` event is logged with high, low, both triggers and the exit time.

### Step 3 — Wait for the breakout
| Condition (strictly beyond) | Action |
|---|---|
| spot **>** long_trigger | **CE_BUY**: buy the ATM call |
| spot **<** short_trigger | **PE_BUY**: buy the ATM put |
| anything else | keep waiting |

- `entry_trigger = tick` (default): the first spot tick beyond a trigger fires. The current spot is also checked
  as soon as the reference candle is read.
- `entry_trigger = candle_close`: a full candle must **close** beyond the trigger. Each candle is judged by its
  close time: it counts if it closes inside the entry window **and before the hard exit**. The window stays open
  for 15 s after the last candle's close, because a finished candle reaches the strategy a moment later. Settings
  where a window candle would close at or after the hard exit are refused when you save (see section 8).
- **First breakout only.** Once a direction fires, the slot never trades again, even if that entry fails or is
  stopped out. There is no re-entry and no reversal.
- If the window ends with no breakout, the slot is skipped (`no_breakout`).
- If this instance already has an open position (only possible with a custom schedule where slots overlap),
  the signal is rejected (`already_in_position`).

### Step 4 — Choose the contract
- **Strike:** spot at the signal rounded to the nearest `strike_step` (halves round up).
  NIFTY step 50: 24,123 → 24,100 · 24,125 → 24,150. SENSEX step 100: 79,349 → 79,300 · 79,350 → 79,400.
- **Expiry:** `nearest_weekly` = the nearest non-expired expiry in the broker's instrument master. On expiry day
  that is the contract expiring today. `next_weekly` = the one after it. If an index has no weekly contracts
  (e.g. BANKNIFTY monthly-only), "nearest" simply means the nearest listed expiry.
- **Quantity:** `lots × lot size`, where the lot size is **read from the broker's instrument master** and never
  hard-coded (e.g. 5 lots × 65 = 325 NIFTY; 5 lots × 20 = 100 SENSEX).

### Step 5 — Risk checks, then the entry order
Before any order is placed, the risk manager checks the instance, broker account and global limits
(max trades/day, max lots per order, max premium per trade, daily loss/profit, max open positions, max orders/day,
kill switches). Any failure rejects the signal and logs the reason.

- Order: **BUY**, intraday, `MARKET` by default. `LIMIT` sends `premium LTP × (1 + limit_slippage_pct %)`,
  rounded up to the tick size.
- The platform waits up to `fill_timeout_sec` (10 s) for the broker to confirm the fill. If it is not filled in
  that time, the order is cancelled and the slot is done (`entry_not_filled`). If the broker rejects it, the
  reason is `entry_rejected`.
- With a partial fill, the platform trades the filled quantity (`PARTIAL_ENTRY` warning).
- The entry price is the **broker's average fill price**, not the LTP.

### Step 6 — (Optional) Place a protective stop at the broker
**Off by default** (`protective_sl_pct = 0`). It is not part of the strategy rules: the strategy's stop is on the
index spot (step 7). When enabled, right after the fill a disaster stop goes to the broker:
```
trigger = entry_fill_price × (1 − protective_sl_pct %)     default 40%, rounded down to tick
```
- Sent as **SL-M SELL**. If the exchange or broker rejects SL-M, the platform retries as **SL-limit**
  (limit = 95% of the trigger).
- This order protects the position if the platform, internet or PC goes down. In normal running the spot SL
  below fires first.
- If both attempts fail, a CRITICAL `PROTECTIVE_SL_FAILED` is logged. The platform still manages the spot SL.
- Keep it **wide** if you use it (e.g. 70%). A tight premium stop, like the old 40% default, cuts trades that the
  spot rules would have kept, especially on expiry day when cheap options can drop 40% on a 10-point move.

### Step 7 — Manage the open trade (exits)
On **every tick** of the spot and the option, plus a 250 ms timer, the exit manager checks these in priority order:

| # | Exit | Rule | Reason logged |
|---:|---|---|---|
| 1 | **Spot stop-loss** | CE: spot **≤ ref_low** · PE: spot **≥ ref_high** (fixed for the whole trade) | `spot_stop_loss` |
| 2 | Take-profit (spot) | `spot_points` / `r_multiple` level reached (see below) | `take_profit` |
| 3 | Take-profit (premium) | option LTP ≥ entry × (1 + value %) | `take_profit` |
| 4 | Final square-off | global square-off time (15:15) if it comes before the slot exit | `square_off` |
| 5 | **Hard exit** | reference start + `exit_offset_min` (15 min) | `timed_exit` |

Exits that happen outside the platform are detected too:

| Exit | How it is detected | Reason logged |
|---|---|---|
| Protective SL filled at the broker | order update / polling | `protective_sl` |
| You closed it in the broker app | position goes flat at the broker (confirmed on two checks). A partial manual exit shrinks the trade and the protective SL quantity | `manual_exit_at_broker` |
| Kill switch (instance, account or global) | from the UI | `kill_switch` |

**Important: the stop-loss is on the index spot, not on the option premium.** The SL level is the opposite end of
the reference candle **without** the buffer. Only the protective SL-M uses the premium.

The stop does **not** move. It stays at the reference candle's low (CE) / high (PE) for the whole trade. A trade
that is neither stopped out nor closed by a take-profit is exited at **exactly** the slot's exit time
(reference start + 15 min, e.g. 10:00:00). This is a fixed rule, not a backstop. (A trailing stop was tried on
06-Nov-2026 and removed.)

When an exit fires:
1. The protective SL at the broker is **cancelled first**, so you never sell twice. If it had already filled,
   that fill is counted.
2. The quantity to sell is capped at what the broker says you hold.
3. A **MARKET SELL** is placed and its fill is confirmed. It is retried up to 3 times for any unfilled remainder.
4. P&L is calculated from broker fills, minus estimated charges (brokerage, STT, exchange, SEBI, stamp, GST).

---

## 3. Take-profit modes

| `take_profit_mode` | `take_profit_value` means | Target |
|---|---|---|
| `none` (default) | — | no TP; the trade runs to the spot SL or the hard exit |
| `spot_points` | index points | CE: entry spot + value · PE: entry spot − value |
| `r_multiple` | multiple of risk | risk = abs(entry spot − spot SL); target = entry spot ± value × risk |
| `premium_pct` | % gain on the option | option LTP ≥ entry fill × (1 + value / 100) |

"Entry spot" is the spot price at the moment of the signal.

---

## 4. Worked example (NIFTY, defaults)

Slot 2, reference candle 10:00 – 10:05: **high 24,120.40, low 24,085.10**.

| Item | Value |
|---|---|
| long_trigger | 24,120.40 + 2.25 = **24,122.65** |
| short_trigger | 24,085.10 − 2.25 = **24,082.85** |
| 10:05:05 | candle read, slot `WATCHING` |
| 10:07:12 | spot ticks **24,123.00** > 24,122.65 → **CE_BUY** |
| Strike | 24,123 / 50 → **24,100 CE**, nearest weekly expiry |
| Quantity | 5 lots × 65 = **325** |
| Fill | MARKET buy filled at avg **₹110.00** |
| Protective SL-M | off by default; with `protective_sl_pct` 40 it would be 110 × 0.60 = **₹66.00** |
| Spot SL | exit if spot **≤ 24,085.10** (the reference low); it does not move |
| Hard exit | **10:15** |
| TP (if `r_multiple` 1.5) | risk 24,123 − 24,085.10 = 37.90 → target 24,123 + 56.85 = **24,179.85** |

Outcomes:
- Spot reaches 24,179.85 first → `take_profit` (only if a TP mode is set).
- Spot falls to 24,085.10 first → `spot_stop_loss`.
- Neither by 10:15:00 → `timed_exit` at market, at exactly 10:15.

Either way the slot is DONE. Slot 3 (10:15 candle) starts fresh.

---

## 5. Settings reference

All settings are edited per instance on the Strategies page. Changes made while a slot is active are **queued** and
applied once the instance is idle (no slot waiting, watching or in a trade).

| Setting | Default | Meaning |
|---|---|---|
| `underlying` | NIFTY | index name as in the broker's instrument master |
| `exchange` | NSE | NSE (options on NFO) or BSE (options on BFO). SENSEX/BANKEX must use BSE |
| `reference_candle_starts` | 13 times above | comma separated HH:MM, aligned to the timeframe, inside 09:15 – 15:30 |
| `candle_timeframe_min` | 5 | reference candle length |
| `buffer_points` | 2.25 | added to high / subtracted from low for the triggers |
| `entry_window_candles` | 2 | entries allowed for this many candles after ref close (capped at the hard exit) |
| `exit_offset_min` | 15 | hard exit = ref **start** + this. Must be greater than the timeframe |
| `lots` | 5 | lots per trade |
| `strike_step` | 50 | ATM rounding step (NIFTY 50, BANKNIFTY 100, SENSEX 100, FINNIFTY 50, MIDCPNIFTY 25) |
| `expiry` | nearest_weekly | `nearest_weekly` or `next_weekly` |
| `entry_trigger` | tick | `tick` or `candle_close` |
| `entry_order_type` | MARKET | `MARKET` or `LIMIT` |
| `limit_slippage_pct` | 1.0 | LIMIT price = premium LTP × (1 + this %) |
| `protective_sl_pct` | 0 (off) | optional broker SL-M at this % below the fill; not in the strategy rules |
| `take_profit_mode` | none | see section 3 |
| `take_profit_value` | 0 | must be > 0 when a TP mode is chosen |
| `candle_finalization_delay_sec` | 5 | wait after ref close before reading the candle |

Validation refuses to save: start times outside market hours or not on the timeframe grid, duplicates, an exit
after 15:30, `exit_offset_min ≤ timeframe`, a BSE index with exchange NSE (or the reverse), a TP mode
with value 0, and (in `candle_close` mode) an entry window whose last candle closes at or after the hard exit.

**Running several indices:** create one instance per index (e.g. `NIFTY-REF` and `SENSEX-REF`), each with its own
underlying, exchange, strike step and lots. They run as separate tasks and do not block each other.

---

## 6. Logs you will see (Logs page / `data/logs/events.jsonl`)

| Event | When |
|---|---|
| `REF_CANDLE` | reference candle read: high, low, triggers, exit time, source |
| `ENTRY_WINDOW_OPENED` / `ENTRY_WINDOW_CLOSED` | window start/end (debug) |
| `SLOT_SKIPPED` | `no_breakout`, `no_reference_candle`, `already_in_position`, `signal_rejected` |
| `SIGNAL_REJECTED` | risk check, open position, no expiry, instance stopping |
| `ENTRY` / `OPENED` | entry filled: symbol, strike, qty, avg price, spot SL, protective SL, TP, exit time |
| `SL_PLACED` / `SL_M_REJECTED` / `PROTECTIVE_SL_FAILED` | protective stop at the broker |
| `TRIGGERED` | an exit fired, with its reason, spot and premium |
| `CLOSED` | final exit price, P&L and charges |
| `SLOTS_MISSED` | the platform started after some of today's slots had already finished |

Every event carries the instance, trade id and slot, so one trade can be followed from candle to P&L.

---

## 7. Restarts, crashes and backtests

- **Restart mid-day:** today's trades are reloaded from the database by slot id. A slot that already traded is
  not traded again. An open trade is reconciled against the broker's real position, and its exit timer and spot
  SL resume. Slots whose exit time has already passed are marked `MISSED`.
- **Strategy task crash:** exits are run by the platform's exit manager, not by the strategy, so open trades are
  still stopped out and time-exited.
- **Paper mode:** the same code path runs against the paper broker; fills use the live premium.
- **Backtest page:** the same strategy class is replayed over historical 1-minute spot candles. Inside each bar
  the price is walked in 0.5-point steps (open → low → high → close for an up bar, high first for a down bar), so
  an entry or stop fills at the first price beyond its level, as it would on live ticks. Option premiums are
  **estimated with Black-Scholes** plus the slippage and charges you set, so backtest P&L is indicative only.
  Broker candles are cached per day (`data/cache/candles/`): re-runs use identical data and each run offers a
  CSV download of the candles it used.

---

## 8. Caveats and things to know

- **`candle_close` mode and the hard exit.** With the defaults (2-candle window, exit at start + 15), the second
  window candle closes exactly at the hard exit, so it could never trigger a trade. The settings form refuses
  that combination and tells you the fix: an entry window of 1 candle, or an exit offset of at least 20 minutes.
  `tick` mode (the default) watches both candles and is unaffected.
- **Fast moves:** in `tick` mode, a gap through the trigger enters on the first tick beyond it. The fill can be
  worse than the trigger, because the order is MARKET.
- **The stop is the reference candle's range.** A wide reference candle means a wide stop and a bigger loss when
  it hits. Use `max_premium_per_trade` or the daily loss limits to cap exposure.
- **The hard exit is a market order** at a fixed time, whatever the P&L.
- **Expiry day:** `nearest_weekly` trades the contract expiring that day, which has fast time decay. Use
  `next_weekly` if you prefer to avoid it.
- **Angel One / BFO:** the Angel One adapter has not been run against live credentials yet. On the first real run,
  check that the SENSEX spot token resolves and that SL-M orders are accepted on BFO (the SL-limit fallback
  covers rejection).
