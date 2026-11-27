//+------------------------------------------------------------------+
//| SmcPlaybook_XAUUSD.mq5                                           |
//| "SMC: Sweep + MSS + FVG/OB" from the algo platform               |
//| (strategies/smc_playbook.py, live run #23 settings) on gold.     |
//|                                                                  |
//| Two timeframes: trend (M15) and entry (M1).                      |
//| Trend: fractal swings (2 candles each side); HH+HL = bull,       |
//|   LH+LL = bear, else no trade; a trend close beyond the last     |
//|   swing against the trend = no trade until new swings confirm.   |
//|   Dealing range = last swing low .. last swing high, 50% = EQ.   |
//| Reversal (long; short mirrors):                                  |
//|   1 trend bull  2 the sweep candle closes in DISCOUNT (< EQ)     |
//|   3 its low goes below a liquidity level (an M1 swing low of the |
//|     last 5 h, the previous day's low, the session opening-range  |
//|     low) and it closes back above it                             |
//|   4+5 within 50 min: a bullish candle with body >= 1.2 x ATR(14) |
//|     and a close above the last M1 swing high (MSS); trading      |
//|     below the sweep low first cancels                            |
//|   6 zone: newest bullish FVG since the sweep, else the order     |
//|     block (last bearish candle before the displacement)          |
//|   7 a candle trades into the zone, closes bullish above the zone |
//|     bottom -> BUY at its close                                   |
//|   8 stop: sweep low - 0.2 x ATR   9 target: 2R                   |
//|   cancel: close below the zone, 30 candles without entry, trend  |
//|     change, entry window end                                     |
//|   +1R: stop to entry + 0.3R                                      |
//| Continuation (trend days, price stays in premium):               |
//|   trend bull, close above EQ but <= 3 x M15 ATR above it, an M1  |
//|   candle with body >= 1.2 ATR closes above the last swing high   |
//|   (break of structure) -> zone as step 6 -> zone touched, then    |
//|   within 3 candles a bullish close ABOVE the zone -> BUY;         |
//|   stop below the impulse's swing low; no target: the stop trails |
//|   the latest M15 swing low; max 2 a day                          |
//| One position at a time; hard exit at the square-off time.        |
//|                                                                  |
//| Sessions moved to gold's active hours (IST, winter; 1 h earlier  |
//| during US/UK daylight time): opening range 13:30-13:45 (London   |
//| open), new entries 13:45-22:30, continuation 14:00-22:00,        |
//| square-off 23:00. Distances are in ATR, so no price scaling.     |
//| Clock: automatic. The server time is read as the usual gold      |
//| broker clock (UTC+2, UTC+3 during US daylight time) and turned    |
//| into IST; in summer every session moves 1 hour earlier in IST.    |
//| Set InpIstOffsetMin >= 0 to give the offset by hand instead.      |
//+------------------------------------------------------------------+
#property copyright "Algo platform port"
#property version   "1.00"

#include <Trade/Trade.mqh>

//--- timeframes / structure
input int    InpTrendTfMin        = 15;     // Trend timeframe (minutes)
input int    InpEntryTfMin        = 1;      // Entry timeframe (minutes)
input int    InpSwingStrength     = 2;      // Swing strength (candles each side)
input double InpDisplacementAtr   = 1.2;    // Displacement: body >= x * ATR(14)
input int    InpConfirmWindowMin  = 50;     // Displacement + MSS within (minutes after the sweep)
input int    InpSetupValidCandles = 30;     // Entry within (candles after the zone)
input double InpStopBufferAtr     = 0.2;    // Stop buffer beyond the sweep (x ATR)
input double InpRewardRisk        = 2.0;    // Target (x risk)
input bool   InpBreakevenAt1R     = true;   // Move stop at +1R
input double InpBreakevenLockR    = 0.3;    // At +1R lock x R (0 = entry)
input double InpMaxStopAtr        = 0.0;    // Skip if stop wider than x * ATR (0 = off)
input bool   InpUsePrevDayLevels  = true;   // Liquidity: previous day high / low
input bool   InpUseOpeningRange   = true;   // Liquidity: session opening range high / low
input bool   InpAllowLong         = true;   // Take long trades
input bool   InpAllowShort        = true;   // Take short trades
//--- sessions (IST)
input int    InpIstOffsetMin      = -1;     // Server -> IST minutes; -1 = auto (broker UTC+2 winter / UTC+3 summer)
input bool   InpSummerShift       = true;   // Summer (US daylight time): all sessions 1 hour earlier in IST
input string InpOpeningRangeStart = "13:30"; // Opening range start (IST), 15 minutes long (London open)
input string InpEntryStart        = "13:45"; // New entries from (IST)
input string InpEntryEnd          = "22:30"; // New entries until (IST)
input string InpSquareOff         = "23:00"; // Hard exit of any open trade (IST)
//--- continuation
input bool   InpContinuation      = true;   // Also continuation entries (reversal_continuation)
input bool   InpContStopZone      = false;  // Continuation stop: false = impulse swing, true = zone
input bool   InpContTrail         = true;   // Continuation exit: true = trail M15 swings, false = 2R target
input bool   InpContBreakeven     = false;  // Continuation: breakeven at +1R
input int    InpContMaxPerDay     = 2;      // Continuation trades per day (max)
input string InpContStart         = "14:00"; // Continuation entries from (IST)
input string InpContEnd           = "22:00"; // Continuation entries until (IST)
input double InpContMaxStretchAtr = 3.0;    // Continuation: skip if more than x * trend ATR past EQ
input int    InpContTouchCandles  = 3;      // Continuation: entry within x candles after the zone touch
//--- size / execution
input double InpVolume            = 0.05;   // Volume per trade (lots)
input int    InpWarmTrendBars     = 300;    // Warm-up trend candles
input int    InpWarmEntryBars     = 800;    // Warm-up entry candles
input int    InpSlippagePoints    = 50;     // Max deviation for market orders (points)
input long   InpMagic             = 23001;  // Magic number

#define SWING_LOOKBACK_MIN 300
#define OR_MINUTES         15
#define KEEP_ENTRY         1200
#define KEEP_TREND         400
#define TRAIL_MIN_STEP_ATR 0.05             // smallest trail move, x ATR (platform: 0.5 NIFTY points)

// setup states
#define S_SWEPT    0
#define S_ARMING   1
#define S_ZONE     2
#define S_CARMING  3
#define S_CZONE    4
#define S_ENDED    9
// liquidity kinds
#define LK_SWING   0
#define LK_PREVDAY 1
#define LK_OR      2
#define LK_CONT    3

struct SBar   { int n; datetime t; double o, h, l, c; };   // t = bar open, IST
struct SSwing { int n; int conf; double px; int kind; };    // kind +1 high, -1 low

//+------------------------------------------------------------------+
class CSeries
  {
public:
   SBar     bars[];
   SSwing   sw[];
   int      keep, k, next;
   double   atr;         // 0 until ready
   double   seedSum;
   int      seedCnt;
   double   prevClose;
   bool     hasPrev;

   void Init(int keep_, int k_)
     {
      keep = keep_; k = k_; next = 0; atr = 0; seedSum = 0; seedCnt = 0; hasPrev = false; prevClose = 0;
      ArrayResize(bars, 0); ArrayResize(sw, 0);
     }

   int Add(datetime t, double o, double h, double l, double c)
     {
      SBar b; b.n = next++; b.t = t; b.o = o; b.h = h; b.l = l; b.c = c;
      int sz = ArraySize(bars); ArrayResize(bars, sz + 1); bars[sz] = b;
      // ATR(14), Wilder (RMA), seeded with the simple mean like ta.atr
      double tr = hasPrev ? MathMax(h - l, MathMax(MathAbs(h - prevClose), MathAbs(l - prevClose))) : h - l;
      prevClose = c; hasPrev = true;
      if(atr == 0)
        {
         seedSum += tr; seedCnt++;
         if(seedCnt == 14) atr = seedSum / 14.0;
        }
      else atr = (tr + 13.0 * atr) / 14.0;
      // the candle k bars back is a confirmed swing if it is the extreme
      sz = ArraySize(bars);
      if(sz >= 2 * k + 1)
        {
         int m = sz - 1 - k;
         double lh = -DBL_MAX, ll = DBL_MAX, rh = -DBL_MAX, rl = DBL_MAX;
         for(int i = m - k; i < m; i++)         { lh = MathMax(lh, bars[i].h); ll = MathMin(ll, bars[i].l); }
         for(int i = m + 1; i <= m + k; i++)    { rh = MathMax(rh, bars[i].h); rl = MathMin(rl, bars[i].l); }
         if(bars[m].h > lh && bars[m].h >= rh) PushSwing(bars[m].n, b.n, bars[m].h, 1);
         if(bars[m].l < ll && bars[m].l <= rl) PushSwing(bars[m].n, b.n, bars[m].l, -1);
        }
      if(sz > keep + 200)
        {
         int drop = sz - keep;
         ArrayRemove(bars, 0, drop);
         int first = bars[0].n, j = 0;
         for(int i = 0; i < ArraySize(sw); i++) if(sw[i].n >= first) sw[j++] = sw[i];
         ArrayResize(sw, j);
        }
      return b.n;
     }

   void PushSwing(int n, int conf, double px, int kind)
     {
      int s = ArraySize(sw); ArrayResize(sw, s + 1);
      sw[s].n = n; sw[s].conf = conf; sw[s].px = px; sw[s].kind = kind;
     }

   bool Get(int n, SBar &out)
     {
      if(ArraySize(bars) == 0) return false;
      int i = n - bars[0].n;
      if(i < 0 || i >= ArraySize(bars)) return false;
      out = bars[i];
      return true;
     }

   SBar Last() { return bars[ArraySize(bars) - 1]; }

   // up to `count` most recent swing prices of `kind` (oldest first); knownBy / since: -1 = no limit
   int LastSwings(int kind, int count, int knownBy, int since, double &out[])
     {
      double tmp[]; int c = 0;
      for(int i = 0; i < ArraySize(sw); i++)
         if(sw[i].kind == kind && (knownBy < 0 || sw[i].conf <= knownBy) && (since < 0 || sw[i].n >= since))
           { ArrayResize(tmp, c + 1); tmp[c++] = sw[i].px; }
      int from = MathMax(0, c - count);
      ArrayResize(out, c - from);
      for(int i = from; i < c; i++) out[i - from] = tmp[i];
      return c - from;
     }

   // the latest swing of `kind` confirmed by bar knownBy (index n and price); false if none
   bool LatestSwing(int kind, int knownBy, int &n, double &px)
     {
      for(int i = ArraySize(sw) - 1; i >= 0; i--)
         if(sw[i].kind == kind && sw[i].conf <= knownBy) { n = sw[i].n; px = sw[i].px; return true; }
      return false;
     }
  };

struct SSetup
  {
   int      no, dir, state, levelKind;
   datetime sweepTime;
   int      sweepN, dispN, mssN, readyN, touchN;
   double   sweepPx, level, refSwing, atr, zLo, zHi;
   string   zoneKind;
  };

//+------------------------------------------------------------------+
CSeries  g_trend, g_entry;
SSetup   g_setups[];
CTrade   g_trade;
ENUM_TIMEFRAMES g_tfTrend, g_tfEntry;
datetime g_lastTrend = 0, g_lastEntry = 0;   // server open time of the last processed bar
int      g_day = 0;                          // IST yyyymmdd of the setups list
int      g_dayKeys[];                        // per-IST-day high / low of the entry bars
double   g_dayHi[], g_dayLo[];
int      g_orStart, g_entryStart, g_entryEnd, g_squareOff, g_contStart, g_contEnd;   // minutes of the IST day

// the open trade
bool     g_in = false, g_beMoved = false, g_cont = false;
int      g_dir = 0, g_setupNo = 0;
double   g_entryPx = 0, g_stop = 0, g_target = 0, g_oneR = 0, g_atr = 0;
int      g_contToday = 0, g_contDay = 0;

//+------------------------------------------------------------------+
int  HmToMin(const string s)
  {
   string p[]; if(StringSplit(s, ':', p) != 2) return -1;
   return (int)StringToInteger(p[0]) * 60 + (int)StringToInteger(p[1]);
  }
// the n-th Sunday of a month, 02:00
datetime NthSunday(int year, int mon, int n)
  {
   MqlDateTime d; d.year = year; d.mon = mon; d.day = 1; d.hour = 2; d.min = 0; d.sec = 0;
   datetime first = StructToTime(d);
   MqlDateTime f; TimeToStruct(first, f);
   int add = (7 - f.day_of_week) % 7;                 // days to the first Sunday
   return first + (add + 7 * (n - 1)) * 86400;
  }
// US daylight time: 2nd Sunday of March .. 1st Sunday of November (gold brokers move their UTC+2 / +3 clock with it)
bool UsDst(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return t >= NthSunday(d.year, 3, 2) && t < NthSunday(d.year, 11, 1);
  }
// server -> IST: manual offset, or auto for the common broker clock (UTC+2 winter, UTC+3 summer); IST = UTC+5:30
datetime ToIst(datetime server)
  {
   if(InpIstOffsetMin >= 0) return server + InpIstOffsetMin * 60;
   return server + (UsDst(server) ? 150 : 210) * 60;
  }
datetime IstNow() { return ToIst(TimeTradeServer()); }
// sessions are written in winter IST; in summer London / New York open 1 hour earlier in IST
int  SessShift(datetime ist) { return (InpSummerShift && UsDst(ist)) ? 3600 : 0; }
int  SessMin(datetime ist) { MqlDateTime d; TimeToStruct(ist + SessShift(ist), d); return d.hour * 60 + d.min; }
int  DayKey(datetime ist) { MqlDateTime d; TimeToStruct(ist, d); return d.year * 10000 + d.mon * 100 + d.day; }
int  MinOfDay(datetime ist) { MqlDateTime d; TimeToStruct(ist, d); return d.hour * 60 + d.min; }
string Hm(datetime ist) { return TimeToString(ist, TIME_MINUTES); }
string Side(int dir) { return dir > 0 ? "LONG" : "SHORT"; }
string GvName(const string key) { return StringFormat("SMCX_%I64d_%s", InpMagic, key); }

ENUM_TIMEFRAMES TfFromMinutes(int m)
  {
   switch(m)
     {
      case 1:  return PERIOD_M1;  case 2:  return PERIOD_M2;  case 3:  return PERIOD_M3;
      case 5:  return PERIOD_M5;  case 10: return PERIOD_M10; case 15: return PERIOD_M15;
      case 30: return PERIOD_M30; case 60: return PERIOD_H1;  case 240: return PERIOD_H4;
     }
   return WRONG_VALUE;
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tfTrend = TfFromMinutes(InpTrendTfMin);
   g_tfEntry = TfFromMinutes(InpEntryTfMin);
   if(g_tfTrend == WRONG_VALUE || g_tfEntry == WRONG_VALUE || InpTrendTfMin <= InpEntryTfMin)
     { Print("Timeframes: trend must be longer than entry (supported 1,2,3,5,10,15,30,60,240)"); return INIT_PARAMETERS_INCORRECT; }
   g_orStart = HmToMin(InpOpeningRangeStart); g_entryStart = HmToMin(InpEntryStart); g_entryEnd = HmToMin(InpEntryEnd);
   g_squareOff = HmToMin(InpSquareOff); g_contStart = HmToMin(InpContStart); g_contEnd = HmToMin(InpContEnd);
   if(g_orStart < 0 || g_entryStart < 0 || g_entryEnd <= g_entryStart || g_squareOff <= g_entryStart || g_contStart < 0
      || g_contEnd <= g_contStart)
     { Print("Check the session times (HH:MM, start before end)"); return INIT_PARAMETERS_INCORRECT; }
   if(!(InpAllowLong || InpAllowShort)) { Print("Allow long, short or both"); return INIT_PARAMETERS_INCORRECT; }
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   if(!WarmUp()) Print("Warm-up history not complete yet: retried on the next bar");
   RestoreTrade();
   EventSetTimer(1);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTick()  { Step(); }
void OnTimer() { Step(); }

bool g_warm = false;

bool WarmUp()
  {
   MqlRates tr[], en[];
   ArraySetAsSeries(tr, false); ArraySetAsSeries(en, false);
   int nt = CopyRates(_Symbol, g_tfTrend, 1, InpWarmTrendBars, tr);    // shift 1: closed bars only
   int ne = CopyRates(_Symbol, g_tfEntry, 1, InpWarmEntryBars, en);
   if(nt < 4 * (2 * InpSwingStrength + 1) || ne < 100) return false;
   g_trend.Init(KEEP_TREND, InpSwingStrength);
   g_entry.Init(KEEP_ENTRY, InpSwingStrength);
   ArrayResize(g_dayKeys, 0); ArrayResize(g_dayHi, 0); ArrayResize(g_dayLo, 0);
   for(int i = 0; i < nt; i++) g_trend.Add(ToIst(tr[i].time), tr[i].open, tr[i].high, tr[i].low, tr[i].close);
   for(int i = 0; i < ne; i++) AddEntry(en[i]);
   g_lastTrend = tr[nt - 1].time; g_lastEntry = en[ne - 1].time;
   g_warm = true;
   string t; double hi, lo; Trend(t, hi, lo);
   PrintFormat("WARMUP %d x M%d, %d x M%d; trend=%s range=%.2f-%.2f", nt, InpTrendTfMin, ne, InpEntryTfMin, t, lo, hi);
   datetime srv = TimeTradeServer();
   PrintFormat("CLOCK server %s = IST %s (%s offset, %s sessions)", TimeToString(srv, TIME_DATE | TIME_MINUTES),
               TimeToString(ToIst(srv), TIME_DATE | TIME_MINUTES), InpIstOffsetMin >= 0 ? "manual" : "auto",
               SessShift(ToIst(srv)) ? "summer: 1 h earlier" : "winter");
   return true;
  }

int AddEntry(const MqlRates &r)
  {
   datetime ist = ToIst(r.time);
   int n = g_entry.Add(ist, r.open, r.high, r.low, r.close);
   int key = DayKey(ist), i = ArraySize(g_dayKeys) - 1;
   if(i < 0 || g_dayKeys[i] != key)
     {
      i++; ArrayResize(g_dayKeys, i + 1); ArrayResize(g_dayHi, i + 1); ArrayResize(g_dayLo, i + 1);
      g_dayKeys[i] = key; g_dayHi[i] = r.high; g_dayLo[i] = r.low;
      if(i > 10) { ArrayRemove(g_dayKeys, 0, 1); ArrayRemove(g_dayHi, 0, 1); ArrayRemove(g_dayLo, 0, 1); }
     }
   else { g_dayHi[i] = MathMax(g_dayHi[i], r.high); g_dayLo[i] = MathMin(g_dayLo[i], r.low); }
   return n;
  }

//+------------------------------------------------------------------+
// bull / bear / side / none and the dealing range (last swing high, last swing low) on the trend timeframe
void Trend(string &t, double &hi, double &lo)
  {
   double hs[], ls[];
   t = "none"; hi = 0; lo = 0;
   if(g_trend.LastSwings(1, 2, -1, -1, hs) < 2 || g_trend.LastSwings(-1, 2, -1, -1, ls) < 2) return;
   hi = hs[1]; lo = ls[1];
   double last = g_trend.Last().c;
   if(hs[1] > hs[0] && ls[1] > ls[0])      t = (last < ls[1]) ? "side" : "bull";
   else if(hs[1] < hs[0] && ls[1] < ls[0]) t = (last > hs[1]) ? "side" : "bear";
   else                                    t = "side";
  }

//+------------------------------------------------------------------+
void Step()
  {
   if(!g_warm) { if(!WarmUp()) return; }
   ManageTrade();
   // new closed bars: trend bars that closed by the entry bar's close first, then the entry bar
   int se = iBarShift(_Symbol, g_tfEntry, g_lastEntry, true);
   if(se < 0) { g_warm = false; return; }                       // history gap: rebuild
   for(int s = se - 1; s >= 1; s--)
     {
      MqlRates r[];
      if(CopyRates(_Symbol, g_tfEntry, s, 1, r) != 1) return;
      datetime closeSrv = r[0].time + InpEntryTfMin * 60;
      int st = iBarShift(_Symbol, g_tfTrend, g_lastTrend, true);
      for(int k = st - 1; k >= 1; k--)
        {
         MqlRates q[];
         if(CopyRates(_Symbol, g_tfTrend, k, 1, q) != 1) break;
         if(q[0].time + InpTrendTfMin * 60 > closeSrv) break;
         g_trend.Add(ToIst(q[0].time), q[0].open, q[0].high, q[0].low, q[0].close);
         g_lastTrend = q[0].time;
        }
      g_lastEntry = r[0].time;
      int n = AddEntry(r[0]);
      OnEntryBar(n, ToIst(closeSrv));
     }
  }

void OnEntryBar(int n, datetime closeIst)
  {
   int day = DayKey(closeIst);
   if(day != g_day)
     {
      for(int i = 0; i < ArraySize(g_setups); i++) if(g_setups[i].state != S_ENDED) End(i, "day_end");
      ArrayResize(g_setups, 0); g_day = day;
     }
   int mod = SessMin(closeIst);
   if(mod >= g_entryEnd)
      for(int i = 0; i < ArraySize(g_setups); i++) if(g_setups[i].state != S_ENDED) End(i, "entry_window_closed");
   SBar b; g_entry.Get(n, b);
   Advance(b, closeIst);
   if(!g_in && mod < g_entryEnd) DetectSweep(b, closeIst);
   if(InpContinuation)
     {
      ContAdvance(b, closeIst);
      if(!g_in) ContDetect(b, closeIst);
     }
   ContTrail();
  }

void End(int i, const string why)
  {
   if(g_setups[i].state == S_ENDED) return;
   g_setups[i].state = S_ENDED;
   PrintFormat("SETUP #%d %s%s ended: %s", g_setups[i].no, Side(g_setups[i].dir),
               g_setups[i].levelKind == LK_CONT ? " continuation" : "", why);
  }

//+------------------------------------------------------------------+
void DetectSweep(const SBar &b, datetime closeIst)
  {
   string t; double hi, lo; Trend(t, hi, lo);
   if(t != "bull" && t != "bear") return;
   bool lng = (t == "bull");
   if((lng && !InpAllowLong) || (!lng && !InpAllowShort)) return;
   double eq = (hi + lo) / 2.0;
   if(lng ? (b.c >= eq) : (b.c <= eq)) return;                   // not in discount (long) / premium (short)
   int day = DayKey(b.t);
   MqlDateTime d; TimeToStruct(b.t, d); d.hour = 0; d.min = 0; d.sec = 0;
   datetime orStart = StructToTime(d) + g_orStart * 60 - SessShift(b.t), orEnd = orStart + OR_MINUTES * 60;
   if(closeIst <= orEnd) return;   // the session's opening range is still forming (platform: nothing before 09:30)
   double lv[]; int lk[]; int c = 0;
   int since = b.n - SWING_LOOKBACK_MIN / InpEntryTfMin;
   double sw[];
   int ns = g_entry.LastSwings(lng ? -1 : 1, 3, b.n - 1, since, sw);
   for(int i = 0; i < ns; i++) { ArrayResize(lv, c + 1); ArrayResize(lk, c + 1); lv[c] = sw[i]; lk[c++] = LK_SWING; }
   if(InpUsePrevDayLevels)
      for(int i = ArraySize(g_dayKeys) - 1; i >= 0; i--)
         if(g_dayKeys[i] < day)
           { ArrayResize(lv, c + 1); ArrayResize(lk, c + 1); lv[c] = lng ? g_dayLo[i] : g_dayHi[i]; lk[c++] = LK_PREVDAY; break; }
   if(InpUseOpeningRange && closeIst > orEnd)
     {
      double orH = -DBL_MAX, orL = DBL_MAX; bool any = false;
      for(int i = 0; i < ArraySize(g_entry.bars); i++)
         if(g_entry.bars[i].t >= orStart && g_entry.bars[i].t < orEnd)
           { any = true; orH = MathMax(orH, g_entry.bars[i].h); orL = MathMin(orL, g_entry.bars[i].l); }
      if(any) { ArrayResize(lv, c + 1); ArrayResize(lk, c + 1); lv[c] = lng ? orL : orH; lk[c++] = LK_OR; }
     }
   int hit = -1;
   for(int i = 0; i < c; i++)
      if(lng ? (b.l < lv[i] && lv[i] < b.c) : (b.c < lv[i] && lv[i] < b.h)) { hit = i; break; }
   if(hit < 0) return;
   double ref[];
   if(g_entry.LastSwings(lng ? 1 : -1, 1, b.n - 1, since, ref) < 1 || g_entry.atr <= 0) return;
   for(int i = 0; i < ArraySize(g_setups); i++)
      if(g_setups[i].state <= S_ZONE && g_setups[i].sweepN == b.n) return;
   SSetup x;
   x.no = ArraySize(g_setups) + 1; x.dir = lng ? 1 : -1; x.state = S_SWEPT; x.levelKind = lk[hit];
   x.sweepTime = b.t; x.sweepN = b.n; x.sweepPx = lng ? b.l : b.h; x.level = lv[hit]; x.refSwing = ref[0];
   x.atr = g_entry.atr; x.dispN = -1; x.mssN = -1; x.readyN = -1; x.touchN = -1; x.zLo = 0; x.zHi = 0; x.zoneKind = "";
   int k = ArraySize(g_setups); ArrayResize(g_setups, k + 1); g_setups[k] = x;
   PrintFormat("SWEEP #%d %s %s %s=%.2f swept %s %.2f, closed %.2f; trend=%s range=%.2f-%.2f eq=%.2f; MSS level %.2f",
               x.no, Side(x.dir), Hm(b.t), lng ? "low" : "high", x.sweepPx,
               x.levelKind == LK_SWING ? "swing" : (x.levelKind == LK_PREVDAY ? "prev_day" : "opening_range"),
               x.level, b.c, t, lo, hi, eq, x.refSwing);
   CheckConfirm(k, b);                                            // the sweep candle itself can confirm
  }

void CheckConfirm(int i, const SBar &b)
  {
   bool lng = g_setups[i].dir > 0;
   double atr = g_entry.atr > 0 ? g_entry.atr : g_setups[i].atr;
   bool bull = b.c > b.o, bear = b.c < b.o;
   if(g_setups[i].dispN < 0 && MathAbs(b.c - b.o) >= InpDisplacementAtr * atr && (lng ? bull : bear)) g_setups[i].dispN = b.n;
   if(g_setups[i].mssN < 0 && (lng ? b.c > g_setups[i].refSwing : b.c < g_setups[i].refSwing)) g_setups[i].mssN = b.n;
   if(g_setups[i].dispN >= 0 && g_setups[i].mssN >= 0)
     {
      g_setups[i].state = S_ARMING;
      g_setups[i].readyN = MathMax(g_setups[i].dispN, g_setups[i].mssN) + 1;
      PrintFormat("MSS #%d %s: displacement + structure break", g_setups[i].no, Side(g_setups[i].dir));
     }
  }

bool FindZone(int i, const SBar &b)
  {
   bool lng = g_setups[i].dir > 0;
   for(int n = b.n; n > g_setups[i].sweepN + 1; n--)               // newest fair-value gap since the sweep
     {
      SBar c1, c3;
      if(!g_entry.Get(n - 2, c1) || !g_entry.Get(n, c3) || c1.n < g_setups[i].sweepN) continue;
      if(lng && c1.h < c3.l)  { g_setups[i].zoneKind = "FVG"; g_setups[i].zLo = c1.h; g_setups[i].zHi = c3.l; return true; }
      if(!lng && c1.l > c3.h) { g_setups[i].zoneKind = "FVG"; g_setups[i].zLo = c3.h; g_setups[i].zHi = c1.l; return true; }
     }
   int from = (g_setups[i].dispN >= 0 ? g_setups[i].dispN : b.n) - 1;
   for(int n = from; n >= g_setups[i].sweepN; n--)                // else the order block
     {
      SBar c;
      if(!g_entry.Get(n, c)) continue;
      if(lng ? (c.c < c.o) : (c.c > c.o)) { g_setups[i].zoneKind = "OB"; g_setups[i].zLo = c.l; g_setups[i].zHi = c.h; return true; }
     }
   return false;
  }

void Advance(const SBar &b, datetime closeIst)
  {
   int window = MathMax(1, InpConfirmWindowMin / InpEntryTfMin);
   string t; double hi, lo; Trend(t, hi, lo);
   bool bull = b.c > b.o, bear = b.c < b.o;
   for(int i = 0; i < ArraySize(g_setups); i++)
     {
      if(g_setups[i].levelKind == LK_CONT && g_setups[i].state != S_ZONE) continue;   // continuation: ContAdvance
      bool lng = g_setups[i].dir > 0;
      if(g_setups[i].state == S_SWEPT)
        {
         if(lng ? b.l < g_setups[i].sweepPx : b.h > g_setups[i].sweepPx) End(i, "beyond_sweep");
         else if(b.n - g_setups[i].sweepN > window)                    End(i, "no_displacement_mss");
         else if(b.n > g_setups[i].sweepN)                             CheckConfirm(i, b);
         continue;
        }
      if(g_setups[i].state == S_ARMING && b.n >= g_setups[i].readyN)
        {
         if(!FindZone(i, b)) { End(i, "no_zone"); continue; }
         g_setups[i].state = S_ZONE;
         PrintFormat("ZONE #%d %s %s %.2f-%.2f", g_setups[i].no, Side(g_setups[i].dir), g_setups[i].zoneKind,
                     g_setups[i].zLo, g_setups[i].zHi);
        }
      if(g_setups[i].state == S_ZONE && g_setups[i].levelKind != LK_CONT)
        {
         double zl = g_setups[i].zLo, zh = g_setups[i].zHi;
         if(t != (lng ? "bull" : "bear"))                         End(i, "trend_changed");
         else if(lng ? b.c < zl : b.c > zh)                       End(i, "closed_through_zone");
         else if(b.n - g_setups[i].readyN >= InpSetupValidCandles) End(i, "no_retrace");
         else if(lng ? (b.l <= zh && bull && b.c > zl) : (b.h >= zl && bear && b.c < zh))
           { if(!g_in) Enter(i, b, closeIst); }
        }
     }
  }

//+------------------------------------------------------------------+
void ContDetect(const SBar &b, datetime closeIst)
  {
   int mod = SessMin(closeIst), day = DayKey(closeIst);
   if(g_contDay != day) { g_contDay = day; g_contToday = 0; }
   if(mod < g_contStart || mod > g_contEnd || g_contToday >= InpContMaxPerDay) return;
   string t; double hi, lo; Trend(t, hi, lo);
   if(t != "bull" && t != "bear") return;
   bool lng = (t == "bull");
   if((lng && !InpAllowLong) || (!lng && !InpAllowShort)) return;
   double eq = (hi + lo) / 2.0, tatr = g_trend.atr, atr = g_entry.atr;
   if(tatr <= 0 || atr <= 0) return;
   double stretch = InpContMaxStretchAtr * tatr;
   if(lng ? !(eq < b.c && b.c <= eq + stretch) : !(eq - stretch <= b.c && b.c < eq)) return;
   bool bull = b.c > b.o, bear = b.c < b.o;
   if(MathAbs(b.c - b.o) < InpDisplacementAtr * atr || !(lng ? bull : bear)) return;
   int swN, oppN; double lvl, oppPx; SBar prev;
   if(!g_entry.LatestSwing(lng ? 1 : -1, b.n - 1, swN, lvl) || !g_entry.LatestSwing(lng ? -1 : 1, b.n - 1, oppN, oppPx)
      || !g_entry.Get(b.n - 1, prev)) return;
   if(!(lng ? (b.c > lvl && lvl >= prev.c) : (b.c < lvl && lvl <= prev.c))) return;   // no fresh break of structure
   for(int i = 0; i < ArraySize(g_setups); i++)
      if(g_setups[i].levelKind == LK_CONT && g_setups[i].refSwing == lvl) return;
   SSetup x;
   x.no = ArraySize(g_setups) + 1; x.dir = lng ? 1 : -1; x.state = S_CARMING; x.levelKind = LK_CONT;
   x.sweepTime = b.t; x.sweepN = oppN; x.sweepPx = oppPx; x.level = lvl; x.refSwing = lvl; x.atr = atr;
   x.dispN = b.n; x.mssN = b.n; x.readyN = b.n + 1; x.touchN = -1; x.zLo = 0; x.zHi = 0; x.zoneKind = "";
   int k = ArraySize(g_setups); ArrayResize(g_setups, k + 1); g_setups[k] = x;
   PrintFormat("BOS #%d %s continuation %s close=%.2f broke %.2f; impulse from %.2f; trend=%s eq=%.2f", x.no,
               Side(x.dir), Hm(b.t), b.c, lvl, oppPx, t, eq);
  }

void ContAdvance(const SBar &b, datetime closeIst)
  {
   string t; double hi, lo; Trend(t, hi, lo);
   bool bull = b.c > b.o, bear = b.c < b.o;
   for(int i = 0; i < ArraySize(g_setups); i++)
     {
      if(g_setups[i].levelKind != LK_CONT) continue;
      bool lng = g_setups[i].dir > 0;
      if(g_setups[i].state == S_CARMING && b.n >= g_setups[i].readyN)
        {
         if(!FindZone(i, b)) { End(i, "no_zone"); continue; }
         g_setups[i].state = S_CZONE;
         PrintFormat("ZONE #%d %s continuation %s %.2f-%.2f", g_setups[i].no, Side(g_setups[i].dir),
                     g_setups[i].zoneKind, g_setups[i].zLo, g_setups[i].zHi);
        }
      if(g_setups[i].state != S_CZONE) continue;
      double zl = g_setups[i].zLo, zh = g_setups[i].zHi;
      if(t != (lng ? "bull" : "bear"))                         End(i, "trend_changed");
      else if(lng ? b.c < zl : b.c > zh)                       End(i, "closed_through_zone");
      else if(b.n - g_setups[i].readyN >= InpSetupValidCandles) End(i, "no_retrace");
      else
        {
         if(lng ? b.l <= zh : b.h >= zl) g_setups[i].touchN = b.n;
         if(g_setups[i].touchN >= 0 && b.n - g_setups[i].touchN <= InpContTouchCandles
            && (lng ? (bull && b.c > zh) : (bear && b.c < zl)) && !g_in)
           {
            if(InpContStopZone) g_setups[i].sweepPx = lng ? zl : zh;
            g_setups[i].state = S_ZONE;
            Enter(i, b, closeIst);
           }
        }
     }
  }

//+------------------------------------------------------------------+
void Enter(int i, const SBar &b, datetime closeIst)
  {
   int mod = SessMin(closeIst);
   if(mod < g_entryStart || mod > g_entryEnd) { End(i, "outside_entry_window"); return; }
   bool lng = g_setups[i].dir > 0, cont = g_setups[i].levelKind == LK_CONT;
   double spot = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(spot <= 0) spot = b.c;
   double atr = g_setups[i].atr;
   double stop = lng ? g_setups[i].sweepPx - InpStopBufferAtr * atr : g_setups[i].sweepPx + InpStopBufferAtr * atr;
   double risk = lng ? spot - stop : stop - spot;
   if(risk <= 0) { End(i, "price_beyond_stop"); return; }
   if(InpMaxStopAtr > 0 && risk > InpMaxStopAtr * atr) { End(i, StringFormat("stop_too_wide_%.1fatr", risk / atr)); return; }
   if(HasPosition()) { End(i, "already_in_position"); return; }
   int sg = lng ? 1 : -1;
   bool trailed = cont && InpContTrail;
   double target = trailed ? 0 : spot + sg * InpRewardRisk * risk;
   double sl = NormalizeDouble(stop, _Digits), tp = trailed ? 0 : NormalizeDouble(target, _Digits);
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double px = lng ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(MathAbs(px - sl) <= minDist) sl = 0;                     // too close for the broker: watched by the EA
   if(tp != 0 && MathAbs(tp - px) <= minDist) tp = 0;
   string cmt = StringFormat("SMC #%d%s", g_setups[i].no, cont ? " cont" : "");
   bool ok = lng ? g_trade.Buy(InpVolume, _Symbol, 0, sl, tp, cmt) : g_trade.Sell(InpVolume, _Symbol, 0, sl, tp, cmt);
   uint rc = g_trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
     {
      PrintFormat("ENTRY #%d failed: %d %s (10019 = not enough money: lower InpVolume)", g_setups[i].no, rc,
                  g_trade.ResultRetcodeDescription());
      End(i, "rejected");
      return;
     }
   g_in = true; g_dir = sg; g_cont = cont; g_beMoved = false; g_setupNo = g_setups[i].no; g_atr = atr;
   g_entryPx = spot; g_stop = stop; g_target = target; g_oneR = spot + sg * risk;
   if(cont) g_contToday++;
   SaveTrade();
   PrintFormat("ENTRY #%d %s%s %s %.2f-%.2f spot=%.2f SL=%.2f (%.2f, %.1f ATR) %s +1R=%.2f", g_setups[i].no, Side(sg),
               cont ? " continuation" : "", g_setups[i].zoneKind, g_setups[i].zLo, g_setups[i].zHi, spot, stop, risk,
               risk / atr, trailed ? StringFormat("no target (trailed on M%d swings)", InpTrendTfMin)
                                   : StringFormat("target=%.2f", target), g_oneR);
   g_setups[i].state = S_ENDED;
   for(int o = 0; o < ArraySize(g_setups); o++) if(o != i && g_setups[o].state != S_ENDED) End(o, "position_taken");
  }

//+------------------------------------------------------------------+
bool HasPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic) return true;
     }
   return false;
  }

void CloseAll(const string why)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         g_trade.PositionClose(t);
     }
   PrintFormat("EXIT #%d %s", g_setupNo, why);
   if(!HasPosition()) ClearTrade();
  }

// tighten the stop (never loosen) and move the broker SL with it
void Tighten(double level, const string why)
  {
   if(g_dir > 0 ? level <= g_stop : level >= g_stop) return;
   double old = g_stop; g_stop = level;
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double px = g_dir > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      if(MathAbs(px - level) > minDist)
         g_trade.PositionModify(t, NormalizeDouble(level, _Digits), PositionGetDouble(POSITION_TP));
     }
   SaveTrade();
   PrintFormat("%s #%d stop %.2f -> %.2f", why, g_setupNo, old, level);
  }

void ManageTrade()
  {
   if(!g_in) return;
   if(!HasPosition()) { PrintFormat("EXIT #%d closed at the broker (SL / TP / manual)", g_setupNo); ClearTrade(); return; }
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double px = g_dir > 0 ? bid : ask;
   if(g_dir > 0 ? bid <= g_stop : ask >= g_stop) { CloseAll("stop_loss"); return; }
   if(g_target != 0 && (g_dir > 0 ? px >= g_target : px <= g_target)) { CloseAll("take_profit"); return; }
   datetime now = IstNow();
   if(SessMin(now) >= g_squareOff) { CloseAll("square_off"); return; }
   if(InpBreakevenAt1R && !g_beMoved && (!g_cont || InpContBreakeven) && (g_dir > 0 ? px >= g_oneR : px <= g_oneR))
     {
      g_beMoved = true;
      Tighten(g_entryPx + (g_oneR - g_entryPx) * InpBreakevenLockR, "BREAKEVEN +1R:");
      SaveTrade();
     }
  }

// continuation trade with the trail exit: the stop follows the latest trend-timeframe swing (minus the buffer)
void ContTrail()
  {
   if(!g_in || !g_cont || !InpContTrail) return;
   double atr = g_entry.atr > 0 ? g_entry.atr : g_atr;
   for(int i = ArraySize(g_trend.sw) - 1; i >= 0; i--)
      if(g_trend.sw[i].kind == (g_dir > 0 ? -1 : 1))
        {
         double cand = g_dir > 0 ? g_trend.sw[i].px - InpStopBufferAtr * atr : g_trend.sw[i].px + InpStopBufferAtr * atr;
         double step = TRAIL_MIN_STEP_ATR * atr;
         if(g_dir > 0 ? cand >= g_stop + step : cand <= g_stop - step) Tighten(cand, "TRAIL");
         return;
        }
  }

//+------------------------------------------------------------------+
void SaveTrade()
  {
   GlobalVariableSet(GvName("in"), g_in ? 1 : 0);
   GlobalVariableSet(GvName("dir"), g_dir);
   GlobalVariableSet(GvName("cont"), g_cont ? 1 : 0);
   GlobalVariableSet(GvName("be"), g_beMoved ? 1 : 0);
   GlobalVariableSet(GvName("no"), g_setupNo);
   GlobalVariableSet(GvName("entry"), g_entryPx);
   GlobalVariableSet(GvName("stop"), g_stop);
   GlobalVariableSet(GvName("target"), g_target);
   GlobalVariableSet(GvName("oner"), g_oneR);
   GlobalVariableSet(GvName("atr"), g_atr);
  }

void ClearTrade()
  {
   g_in = false; g_dir = 0; g_cont = false; g_beMoved = false;
   GlobalVariableSet(GvName("in"), 0);
  }

void RestoreTrade()
  {
   if(!GlobalVariableCheck(GvName("in")) || GlobalVariableGet(GvName("in")) < 1 || !HasPosition()) return;
   g_in = true;
   g_dir = (int)GlobalVariableGet(GvName("dir"));
   g_cont = GlobalVariableGet(GvName("cont")) > 0;
   g_beMoved = GlobalVariableGet(GvName("be")) > 0;
   g_setupNo = (int)GlobalVariableGet(GvName("no"));
   g_entryPx = GlobalVariableGet(GvName("entry"));
   g_stop = GlobalVariableGet(GvName("stop"));
   g_target = GlobalVariableGet(GvName("target"));
   g_oneR = GlobalVariableGet(GvName("oner"));
   g_atr = GlobalVariableGet(GvName("atr"));
   PrintFormat("restored open trade #%d %s entry=%.2f SL=%.2f target=%.2f", g_setupNo, Side(g_dir), g_entryPx, g_stop,
               g_target);
  }
//+------------------------------------------------------------------+
