//+------------------------------------------------------------------+
//| Dhurandhar_XAUUSD.mq5                                            |
//| "Dhurandhar: hourly 15-min Breakout" (algo platform,             |
//| strategies/dhurandhar.py, dhurandhar.xlsx) on gold.              |
//|                                                                  |
//| Rules (times are IST, winter; 1 h earlier in US daylight time):  |
//|  - Reference candles: 15-min candles every hour, from the London |
//|    open through New York:                                        |
//|    13:30 14:30 15:30 16:30 17:30 18:30 19:30 20:30 21:30          |
//|  - From the reference close until the slot's exit:               |
//|      price > ref high + buffer  -> BUY                           |
//|      price < ref low  - buffer  -> SELL                          |
//|    first breakout only, one trade per slot, one position at once  |
//|  - Stop-loss: the reference candle's other end                    |
//|  - Exit: one hour after the reference start (13:30 -> 14:29:58);  |
//|    no take-profit by default (on NIFTY, holding to the hourly     |
//|    exit beat every TP setting: +2.95 lakh vs +0.80 with 4 TPs)    |
//|  - Optional TPs (InpTpMode): TP1-TP4 as % of price or in $        |
//|                                                                  |
//| Buffer: NIFTY's 5 points on ~25,800 = 0.02 % of price (about      |
//| $0.80 at 4,000).                                                  |
//| Clock: automatic. The server time is read as the usual gold      |
//| broker clock (UTC+2, UTC+3 during US daylight time) and turned    |
//| into IST; in summer every slot moves 1 hour earlier in IST.       |
//| Set InpIstOffsetMin >= 0 to give the offset by hand instead.      |
//+------------------------------------------------------------------+
#property copyright "Algo platform port"
#property version   "1.00"

#include <Trade/Trade.mqh>

enum ENUM_TP_MODE
  {
   TP_NONE = 0,            // no take-profit: hold to the hourly exit (or the SL)
   TP_PERCENT = 1,         // TP1-TP4 as % of the entry price
   TP_PRICE = 2            // TP1-TP4 as fixed price distances ($)
  };

//--- schedule
input string InpSlots            = "13:30,14:30,15:30,16:30,17:30,18:30,19:30,20:30,21:30"; // Reference candle starts (winter IST, HH:MM)
input int    InpTimeframeMin     = 15;     // Reference candle timeframe (minutes)
input int    InpEntryWindowBars  = 3;      // Entry window (candles after the reference close: 3 x 15 = until the exit)
input int    InpExitOffsetMin    = 60;     // Hard exit = reference start + minutes
input int    InpExitEarlySec     = 2;      // Exit this many seconds early (entry window ends then too)
input int    InpFinalizeDelaySec = 5;      // Wait after the reference close before reading it (s)
input int    InpIstOffsetMin     = -1;     // Server -> IST minutes; -1 = auto (broker UTC+2 winter / UTC+3 summer)
input bool   InpSummerShift      = true;   // Summer (US daylight time): all slots 1 hour earlier in IST
//--- size
input double InpVolumePerLot     = 0.01;   // Volume per platform lot (5 lots x 0.01 = 0.05 lot per trade)
input int    InpTp1Lots          = 2;      // TP1 lots (with TPs on; without TPs only the total counts)
input int    InpTp2Lots          = 1;      // TP2 lots
input int    InpTp3Lots          = 1;      // TP3 lots
input int    InpTp4Lots          = 1;      // TP4 lots (the last TP closes the rest)
//--- buffer and take-profits
input double InpBufferPct        = 0.02;   // Buffer beyond ref high / low (% of price; NIFTY 5 pts)
input ENUM_TP_MODE InpTpMode     = TP_NONE;
input double InpTp1Pct           = 0.25;   // [% mode] TP1 (% of the entry price)
input double InpTp2Pct           = 0.40;   // [% mode] TP2
input double InpTp3Pct           = 0.55;   // [% mode] TP3
input double InpTp4Pct           = 0.75;   // [% mode] TP4
input double InpTp1Price         = 10.0;   // [price mode] TP1 ($ from the entry price)
input double InpTp2Price         = 16.0;   // [price mode] TP2
input double InpTp3Price         = 22.0;   // [price mode] TP3
input double InpTp4Price         = 30.0;   // [price mode] TP4
//--- execution
input bool   InpBrokerStopLoss   = true;   // Also place the SL on the position at the broker
input int    InpSlippagePoints   = 50;     // Max deviation for market orders (points)
input long   InpMagic            = 777001; // Magic number

//--- slot states
#define ST_PENDING   0
#define ST_WATCHING  1
#define ST_DONE      2   // traded, skipped or window closed: never trades again today

struct Slot
  {
   int      hh, mm;
   datetime refStart, refEnd, entryEnd, exitAt;   // IST
   int      state;
   double   refHigh, refLow, longTrig, shortTrig;
  };

Slot     g_slots[];
int      g_day = 0;            // yyyymmdd (IST) the slots are built for
CTrade   g_trade;
ENUM_TIMEFRAMES g_tf = PERIOD_M5;
double   g_tpVal[4];           // TP1-TP4: % of price or $ (see InpTpMode)
double   g_tpDist[4];          // TP distances in price, set at each entry
int      g_tpLots[4];

//--- the open trade (also kept in terminal global variables, so a restart continues it)
bool     g_inTrade = false;
int      g_dir = 0;            // +1 buy, -1 sell
int      g_slotNo = -1;
double   g_entry = 0, g_stop = 0;
datetime g_exitAt = 0;
int      g_tpMask = 0;         // bit i = TP(i+1) sold

//+------------------------------------------------------------------+
string GvName(const string key) { return StringFormat("DHRX_%I64d_%s", InpMagic, key); }

// the n-th Sunday of a month, 02:00
datetime NthSunday(int year, int mon, int n)
  {
   MqlDateTime d; d.year = year; d.mon = mon; d.day = 1; d.hour = 2; d.min = 0; d.sec = 0;
   datetime first = StructToTime(d);
   MqlDateTime f; TimeToStruct(first, f);
   return first + ((7 - f.day_of_week) % 7 + 7 * (n - 1)) * 86400;
  }
// US daylight time: 2nd Sunday of March .. 1st Sunday of November (gold brokers move their UTC+2 / +3 clock with it)
bool UsDst(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return t >= NthSunday(d.year, 3, 2) && t < NthSunday(d.year, 11, 1);
  }
int  OffsetMin(datetime server) { return InpIstOffsetMin >= 0 ? InpIstOffsetMin : (UsDst(server) ? 150 : 210); }
datetime IstNow() { datetime s = TimeTradeServer(); return s + OffsetMin(s) * 60; }
// slots are written in winter IST; in summer London / New York open 1 hour earlier in IST
int  SessShift(datetime ist) { return (InpSummerShift && UsDst(ist)) ? 3600 : 0; }

int DayKey(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return d.year * 10000 + d.mon * 100 + d.day;
  }

ENUM_TIMEFRAMES TfFromMinutes(int m)
  {
   switch(m)
     {
      case 1:  return PERIOD_M1;  case 2:  return PERIOD_M2;  case 3:  return PERIOD_M3;
      case 4:  return PERIOD_M4;  case 5:  return PERIOD_M5;  case 6:  return PERIOD_M6;
      case 10: return PERIOD_M10; case 12: return PERIOD_M12; case 15: return PERIOD_M15;
      case 20: return PERIOD_M20; case 30: return PERIOD_M30; case 60: return PERIOD_H1;
     }
   return WRONG_VALUE;
  }

double NormVolume(double v)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   if(step <= 0) step = 0.01;
   v = MathFloor(v / step + 1e-9) * step;
   return (v < vmin) ? 0.0 : NormalizeDouble(v, 8);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = TfFromMinutes(InpTimeframeMin);
   if(g_tf == WRONG_VALUE) { Print("Unsupported timeframe: ", InpTimeframeMin); return INIT_PARAMETERS_INCORRECT; }
   if(InpExitOffsetMin * 60 - InpExitEarlySec <= InpTimeframeMin * 60)
     { Print("Hard exit must come after the reference candle closes"); return INIT_PARAMETERS_INCORRECT; }
   g_tpLots[0] = InpTp1Lots; g_tpLots[1] = InpTp2Lots; g_tpLots[2] = InpTp3Lots; g_tpLots[3] = InpTp4Lots;
   int total = InpTp1Lots + InpTp2Lots + InpTp3Lots + InpTp4Lots;
   if(total <= 0) { Print("Set TP lots"); return INIT_PARAMETERS_INCORRECT; }
   if(NormVolume(InpVolumePerLot) <= 0) { Print("Volume per lot is below the symbol's minimum"); return INIT_PARAMETERS_INCORRECT; }

   double v[4] = {0, 0, 0, 0};
   if(InpTpMode == TP_PERCENT)    { v[0] = InpTp1Pct; v[1] = InpTp2Pct; v[2] = InpTp3Pct; v[3] = InpTp4Pct; }
   else if(InpTpMode == TP_PRICE) { v[0] = InpTp1Price; v[1] = InpTp2Price; v[2] = InpTp3Price; v[3] = InpTp4Price; }
   if(InpTpMode != TP_NONE)
      for(int i = 0; i < 4; i++)
        {
         if(v[i] <= 0 || (i > 0 && v[i] <= v[i - 1])) { Print("TPs must rise TP1 < TP2 < TP3 < TP4"); return INIT_PARAMETERS_INCORRECT; }
         g_tpVal[i] = v[i];
        }
   if(InpBufferPct < 0) { Print("Buffer must be >= 0"); return INIT_PARAMETERS_INCORRECT; }
   datetime srv = TimeTradeServer();
   PrintFormat("Dhurandhar gold: buffer %.3f%%, %s, volume %.2f; CLOCK server %s = IST %s (%s offset, %s slots)",
               InpBufferPct, InpTpMode == TP_NONE ? "no TPs (hourly exit)" : StringFormat("TPs %.2f/%.2f/%.2f/%.2f %s",
               v[0], v[1], v[2], v[3], InpTpMode == TP_PERCENT ? "%" : "$"), total * InpVolumePerLot,
               TimeToString(srv, TIME_DATE | TIME_MINUTES), TimeToString(IstNow(), TIME_DATE | TIME_MINUTES),
               InpIstOffsetMin >= 0 ? "manual" : "auto", SessShift(IstNow()) ? "summer: 1 h earlier" : "winter");

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   RestoreTrade();
   EventSetTimer(1);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTick()  { Step(); }
void OnTimer() { Step(); }

//+------------------------------------------------------------------+
void BuildDay(datetime nowIst)
  {
   g_day = DayKey(nowIst);
   string parts[];
   int n = StringSplit(InpSlots, ',', parts);
   ArrayResize(g_slots, 0);
   MqlDateTime d; TimeToStruct(nowIst, d);
   for(int i = 0; i < n; i++)
     {
      string p = parts[i]; StringTrimLeft(p); StringTrimRight(p);
      string hm[];
      if(StringSplit(p, ':', hm) != 2) continue;
      Slot s;
      s.hh = (int)StringToInteger(hm[0]); s.mm = (int)StringToInteger(hm[1]);
      MqlDateTime t = d; t.hour = s.hh; t.min = s.mm; t.sec = 0;
      s.refStart = StructToTime(t);
      s.refStart -= SessShift(s.refStart);
      s.refEnd   = s.refStart + InpTimeframeMin * 60;
      s.exitAt   = s.refStart + InpExitOffsetMin * 60 - InpExitEarlySec;
      datetime winEnd = s.refEnd + InpEntryWindowBars * InpTimeframeMin * 60;
      s.entryEnd = (winEnd < s.exitAt) ? winEnd : s.exitAt;
      s.state = ST_PENDING;
      s.refHigh = s.refLow = s.longTrig = s.shortTrig = 0;
      // a slot that already finished today (traded / skipped before a restart) stays done
      if(GlobalVariableCheck(GvName(StringFormat("%d_%d", g_day, i)))) s.state = ST_DONE;
      int k = ArraySize(g_slots); ArrayResize(g_slots, k + 1); g_slots[k] = s;
     }
  }

void MarkDone(int i)
  {
   g_slots[i].state = ST_DONE;
   GlobalVariableSet(GvName(StringFormat("%d_%d", g_day, i)), 1);
  }

bool LoadReference(int i)
  {
   // the reference bar's open time in server time
   datetime approx = g_slots[i].refStart - 210 * 60;
   datetime serverStart = g_slots[i].refStart - OffsetMin(approx) * 60;
   int shift = iBarShift(_Symbol, g_tf, serverStart, true);
   if(shift < 1) return false;      // not there yet (or still forming): retried next second
   g_slots[i].refHigh = iHigh(_Symbol, g_tf, shift);
   g_slots[i].refLow  = iLow(_Symbol, g_tf, shift);
   double mid = (g_slots[i].refHigh + g_slots[i].refLow) / 2.0;
   double buf = mid * InpBufferPct / 100.0;
   g_slots[i].longTrig  = NormalizeDouble(g_slots[i].refHigh + buf, _Digits);
   g_slots[i].shortTrig = NormalizeDouble(g_slots[i].refLow - buf, _Digits);
   g_slots[i].state = ST_WATCHING;
   PrintFormat("REF slot %s IST high=%.2f low=%.2f long>%.2f short<%.2f window to %s exit %s",
               TimeToString(g_slots[i].refStart, TIME_MINUTES), g_slots[i].refHigh, g_slots[i].refLow, g_slots[i].longTrig,
               g_slots[i].shortTrig, TimeToString(g_slots[i].entryEnd, TIME_SECONDS),
               TimeToString(g_slots[i].exitAt, TIME_SECONDS));
   return true;
  }

//+------------------------------------------------------------------+
void Step()
  {
   datetime now = IstNow();
   if(DayKey(now) != g_day) BuildDay(now);
   ManageTrade(now);

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid <= 0) return;
   for(int i = 0; i < ArraySize(g_slots); i++)
     {
      if(g_slots[i].state == ST_DONE) continue;
      if(g_slots[i].state == ST_PENDING)
        {
         if(now >= g_slots[i].entryEnd) { MarkDone(i); continue; }            // no reference candle in time
         if(now < g_slots[i].refEnd + InpFinalizeDelaySec) continue;
         if(!LoadReference(i)) continue;
        }
      if(g_slots[i].state == ST_WATCHING)
        {
         if(now >= g_slots[i].entryEnd) { MarkDone(i); Print("slot ", i + 1, ": no breakout"); continue; }
         int dir = 0;
         if(bid > g_slots[i].longTrig) dir = 1;
         else if(bid < g_slots[i].shortTrig) dir = -1;
         if(dir == 0) continue;
         MarkDone(i);                                                        // first breakout only
         if(g_inTrade || HasPosition()) { Print("slot ", i + 1, ": skipped, already in a position"); continue; }
         Enter(i, dir);
        }
     }
  }

bool HasPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return true;
     }
   return false;
  }

double PositionVolume()
  {
   double v = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         v += PositionGetDouble(POSITION_VOLUME);
     }
   return v;
  }

void Enter(int i, int dir)
  {
   int lots = InpTp1Lots + InpTp2Lots + InpTp3Lots + InpTp4Lots;
   double vol = NormVolume(lots * InpVolumePerLot);
   double stop = (dir > 0) ? g_slots[i].refLow : g_slots[i].refHigh;
   double sl = 0;
   if(InpBrokerStopLoss)
     {
      double px = (dir > 0) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
      if((dir > 0 && px - stop > minDist) || (dir < 0 && stop - px > minDist)) sl = NormalizeDouble(stop, _Digits);
     }
   string cmt = StringFormat("DHR %02d:%02d", g_slots[i].hh, g_slots[i].mm);
   bool ok = (dir > 0) ? g_trade.Buy(vol, _Symbol, 0, sl, 0, cmt) : g_trade.Sell(vol, _Symbol, 0, sl, 0, cmt);
   if(!ok || (g_trade.ResultRetcode() != TRADE_RETCODE_DONE && g_trade.ResultRetcode() != TRADE_RETCODE_PLACED))
     {
      PrintFormat("slot %d entry failed: %d %s (vol %.2f; 10019 = not enough money: lower InpVolumePerLot)", i + 1,
                  g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription(), vol);
      return;
     }
   g_inTrade = true; g_dir = dir; g_slotNo = i;
   g_entry = (g_trade.ResultPrice() > 0) ? g_trade.ResultPrice() : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   g_stop = stop; g_exitAt = g_slots[i].exitAt; g_tpMask = 0;
   SetTpDistances();
   SaveTrade();
   string tps = (InpTpMode == TP_NONE) ? "no TP (hourly exit)"
                : StringFormat("TP=%.2f/%.2f/%.2f/%.2f", g_entry + dir * g_tpDist[0], g_entry + dir * g_tpDist[1],
                               g_entry + dir * g_tpDist[2], g_entry + dir * g_tpDist[3]);
   PrintFormat("ENTRY %s slot %s vol=%.2f entry=%.2f SL=%.2f (risk %.2f) %s exit %s", dir > 0 ? "BUY" : "SELL",
               TimeToString(g_slots[i].refStart, TIME_MINUTES), vol, g_entry, g_stop, MathAbs(g_entry - g_stop), tps,
               TimeToString(g_exitAt, TIME_SECONDS));
  }

void SetTpDistances()
  {
   for(int k = 0; k < 4; k++)
      g_tpDist[k] = (InpTpMode == TP_PERCENT) ? g_entry * g_tpVal[k] / 100.0 : g_tpVal[k];   // unused with TP_NONE
  }

//+------------------------------------------------------------------+
void ManageTrade(datetime now)
  {
   if(!g_inTrade) return;
   if(!HasPosition()) { Print("position closed (broker SL or manual)"); ClearTrade(); return; }
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   // the spot SL, also checked here in case the broker SL could not be placed
   if((g_dir > 0 && bid <= g_stop) || (g_dir < 0 && ask >= g_stop)) { CloseAll("stop_loss"); return; }
   if(now >= g_exitAt) { CloseAll("timed_exit"); return; }
   if(InpTpMode == TP_NONE) return;            // held to the hourly exit (or the SL)
   // take-profits, in order; the last TP with lots closes the rest
   double px = (g_dir > 0) ? bid : ask;
   int last = 3;
   while(last > 0 && g_tpLots[last] <= 0) last--;
   for(int k = 0; k <= last; k++)
     {
      if((g_tpMask >> k) & 1) continue;
      double level = g_entry + g_dir * g_tpDist[k];
      if(!((g_dir > 0 && px >= level) || (g_dir < 0 && px <= level))) break;
      if(k == last) { CloseAll(StringFormat("tp%d", k + 1)); return; }
      if(g_tpLots[k] > 0) ClosePart(g_tpLots[k] * InpVolumePerLot, StringFormat("tp%d", k + 1));
      g_tpMask |= (1 << k);
      SaveTrade();
     }
  }

void ClosePart(double vol, const string why)
  {
   vol = NormVolume(MathMin(vol, PositionVolume()));
   if(vol <= 0) return;
   double want = vol;
   for(int i = PositionsTotal() - 1; i >= 0 && vol > 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double pv = PositionGetDouble(POSITION_VOLUME);
      double v = NormVolume(MathMin(vol, pv));
      if(v <= 0) continue;
      bool ok = (v >= pv) ? g_trade.PositionClose(t) : g_trade.PositionClosePartial(t, v);
      if(ok) vol -= v;
     }
   PrintFormat("%s: sold %.2f, %.2f left", why, want - MathMax(vol, 0.0), PositionVolume());
  }

void CloseAll(const string why)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         g_trade.PositionClose(t);
     }
   PrintFormat("EXIT %s", why);
   if(!HasPosition()) ClearTrade();
  }

//+------------------------------------------------------------------+
void SaveTrade()
  {
   GlobalVariableSet(GvName("in"), g_inTrade ? 1 : 0);
   GlobalVariableSet(GvName("dir"), g_dir);
   GlobalVariableSet(GvName("entry"), g_entry);
   GlobalVariableSet(GvName("stop"), g_stop);
   GlobalVariableSet(GvName("exit"), (double)g_exitAt);
   GlobalVariableSet(GvName("mask"), g_tpMask);
   GlobalVariableSet(GvName("slot"), g_slotNo);
  }

void ClearTrade()
  {
   g_inTrade = false; g_dir = 0; g_tpMask = 0; g_slotNo = -1;
   GlobalVariableSet(GvName("in"), 0);
  }

void RestoreTrade()
  {
   if(!GlobalVariableCheck(GvName("in")) || GlobalVariableGet(GvName("in")) < 1 || !HasPosition()) return;
   g_inTrade = true;
   g_dir    = (int)GlobalVariableGet(GvName("dir"));
   g_entry  = GlobalVariableGet(GvName("entry"));
   g_stop   = GlobalVariableGet(GvName("stop"));
   g_exitAt = (datetime)GlobalVariableGet(GvName("exit"));
   g_tpMask = (int)GlobalVariableGet(GvName("mask"));
   g_slotNo = (int)GlobalVariableGet(GvName("slot"));
   SetTpDistances();
   PrintFormat("restored open trade: dir=%d entry=%.2f SL=%.2f TPs sold mask=%d exit %s", g_dir, g_entry, g_stop,
               g_tpMask, TimeToString(g_exitAt, TIME_SECONDS));
  }
//+------------------------------------------------------------------+
