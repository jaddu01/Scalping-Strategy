//+------------------------------------------------------------------+
//| RamsFavBreakout.mq5                                              |
//| "9:45 - Ram's Fav - TP1/TP2" from the algo platform               |
//| (template nifty_ref_breakout_tp, run #4 settings) as an MT5 EA.  |
//|                                                                  |
//| Rules (times are IST):                                           |
//|  - Reference candles: 5-min candles starting at the slot times    |
//|    10:15 10:45 11:15 11:45 12:10 12:45 13:15 13:45 14:10 14:35   |
//|  - After the reference candle closes, until the entry window ends |
//|    (2 candles after its close, never past the hard exit):        |
//|      price > ref high + 2.25  -> BUY  (platform: buy the ATM CE)  |
//|      price < ref low  - 2.25  -> SELL (platform: buy the ATM PE)  |
//|    first breakout only, one trade per slot, one position at once  |
//|  - Stop-loss: the reference candle's other end (low for a buy,    |
//|    high for a sell)                                              |
//|  - 4 take-profits, lots 2 / 1 / 1 / 1 (5 lots); the last TP       |
//|    closes what is left                                           |
//|  - Hard exit: reference start + 15 min - 2 s (10:15 -> 10:29:58)  |
//|                                                                  |
//| MT5 brokers do not list NSE options, so this EA trades the        |
//| chart's own symbol (a NIFTY future / CFD): long for a CE signal,  |
//| short for a PE signal. The platform's TPs are % of the option     |
//| premium; here they are price points. TP_PREMIUM_APPROX converts   |
//| the platform's % to points: points = % x premium / delta.         |
//+------------------------------------------------------------------+
#property copyright "Algo platform port"
#property version   "1.00"

#include <Trade/Trade.mqh>

enum ENUM_TP_MODE
  {
   TP_POINTS = 0,          // TPs in price points (TP1-TP4 points below)
   TP_PREMIUM_APPROX = 1   // platform % of premium -> points (premium, delta, bands below)
  };

//--- schedule
input string InpSlots            = "10:15,10:45,11:15,11:45,12:10,12:45,13:15,13:45,14:10,14:35"; // Reference candle starts (IST, HH:MM)
input int    InpTimeframeMin     = 5;      // Reference candle timeframe (minutes)
input double InpBuffer           = 2.25;   // Buffer beyond ref high / low (points)
input int    InpEntryWindowBars  = 2;      // Entry window (candles after the reference close)
input int    InpExitOffsetMin    = 15;     // Hard exit = reference start + minutes
input int    InpExitEarlySec     = 2;      // Exit this many seconds early (entry window ends then too)
input int    InpFinalizeDelaySec = 5;      // Wait after the reference close before reading it (s)
input int    InpIstOffsetMin     = 0;      // Minutes to ADD to server time to get IST (server UTC+2 -> 210)
//--- size
input double InpVolumePerLot     = 1.0;    // Volume per platform lot (e.g. 1.0 or 0.01)
input int    InpTp1Lots          = 2;      // TP1 lots
input int    InpTp2Lots          = 1;      // TP2 lots
input int    InpTp3Lots          = 1;      // TP3 lots
input int    InpTp4Lots          = 1;      // TP4 lots (the last TP closes the rest)
//--- take-profits
input ENUM_TP_MODE InpTpMode     = TP_POINTS;
input double InpTp1Points        = 30.0;   // TP1 (points from the entry price)
input double InpTp2Points        = 45.0;   // TP2
input double InpTp3Points        = 57.0;   // TP3
input double InpTp4Points        = 76.0;   // TP4
input double InpOptionPremium    = 190.0;  // [premium mode] typical ATM option premium
input double InpOptionDelta      = 0.5;    // [premium mode] ATM delta (index points per premium point = 1/delta)
input double InpBand1Max         = 80.0;   // [premium mode] low band: premium below
input double InpBand2Max         = 150.0;  // [premium mode] middle band: premium up to
input string InpPctsLow          = "15,22,30,40"; // [premium mode] TP1-TP4 % for the low band
input string InpPctsMid          = "12,15,20,25"; // [premium mode] TP1-TP4 % for the middle band
input string InpPctsHigh         = "8,12,15,20";  // [premium mode] TP1-TP4 % for the high band
//--- execution
input bool   InpBrokerStopLoss   = true;   // Also place the SL on the position at the broker
input int    InpSlippagePoints   = 50;     // Max deviation for market orders (points)
input long   InpMagic            = 945001; // Magic number

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
double   g_tpDist[4];          // TP distances in price units
int      g_tpLots[4];

//--- the open trade (also kept in terminal global variables, so a restart continues it)
bool     g_inTrade = false;
int      g_dir = 0;            // +1 buy, -1 sell
int      g_slotNo = -1;
double   g_entry = 0, g_stop = 0;
datetime g_exitAt = 0;
int      g_tpMask = 0;         // bit i = TP(i+1) sold

//+------------------------------------------------------------------+
string GvName(const string key) { return StringFormat("RFB_%I64d_%s", InpMagic, key); }

datetime IstNow() { return TimeTradeServer() + InpIstOffsetMin * 60; }

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

bool ParsePcts(const string s, double &out[])
  {
   string parts[];
   if(StringSplit(s, ',', parts) != 4) return false;
   ArrayResize(out, 4);
   for(int i = 0; i < 4; i++)
     {
      out[i] = StringToDouble(parts[i]);
      if(out[i] <= 0 || (i > 0 && out[i] <= out[i - 1])) return false;
     }
   return true;
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

   if(InpTpMode == TP_POINTS)
     {
      double pts[4] = {0, 0, 0, 0};
      pts[0] = InpTp1Points; pts[1] = InpTp2Points; pts[2] = InpTp3Points; pts[3] = InpTp4Points;
      for(int i = 0; i < 4; i++)
        {
         if(pts[i] <= 0 || (i > 0 && pts[i] <= pts[i - 1])) { Print("TP points must rise TP1 < TP2 < TP3 < TP4"); return INIT_PARAMETERS_INCORRECT; }
         g_tpDist[i] = pts[i];
        }
     }
   else
     {
      double low[], mid[], high[];
      if(!ParsePcts(InpPctsLow, low) || !ParsePcts(InpPctsMid, mid) || !ParsePcts(InpPctsHigh, high))
        { Print("Band % lists need 4 rising numbers, e.g. 8,12,15,20"); return INIT_PARAMETERS_INCORRECT; }
      if(InpOptionDelta <= 0 || InpOptionPremium <= 0) { Print("Premium and delta must be > 0"); return INIT_PARAMETERS_INCORRECT; }
      // the platform's band rule: below band 1, up to and including band 2, above
      for(int i = 0; i < 4; i++)
        {
         double pct = (InpOptionPremium < InpBand1Max) ? low[i] : ((InpOptionPremium <= InpBand2Max) ? mid[i] : high[i]);
         g_tpDist[i] = pct / 100.0 * InpOptionPremium / InpOptionDelta;
        }
     }
   PrintFormat("TP distances (points): %.2f / %.2f / %.2f / %.2f, lots %d/%d/%d/%d x %.2f",
               g_tpDist[0], g_tpDist[1], g_tpDist[2], g_tpDist[3], InpTp1Lots, InpTp2Lots, InpTp3Lots, InpTp4Lots,
               InpVolumePerLot);

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
   datetime serverStart = g_slots[i].refStart - InpIstOffsetMin * 60;
   int shift = iBarShift(_Symbol, g_tf, serverStart, true);
   if(shift < 1) return false;      // not there yet (or still forming): retried next second
   g_slots[i].refHigh = iHigh(_Symbol, g_tf, shift);
   g_slots[i].refLow  = iLow(_Symbol, g_tf, shift);
   g_slots[i].longTrig  = NormalizeDouble(g_slots[i].refHigh + InpBuffer, _Digits);
   g_slots[i].shortTrig = NormalizeDouble(g_slots[i].refLow - InpBuffer, _Digits);
   g_slots[i].state = ST_WATCHING;
   PrintFormat("REF slot %02d:%02d high=%.2f low=%.2f long>%.2f short<%.2f window to %s exit %s",
               g_slots[i].hh, g_slots[i].mm, g_slots[i].refHigh, g_slots[i].refLow, g_slots[i].longTrig,
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
   string cmt = StringFormat("RFB %02d:%02d", g_slots[i].hh, g_slots[i].mm);
   bool ok = (dir > 0) ? g_trade.Buy(vol, _Symbol, 0, sl, 0, cmt) : g_trade.Sell(vol, _Symbol, 0, sl, 0, cmt);
   if(!ok || (g_trade.ResultRetcode() != TRADE_RETCODE_DONE && g_trade.ResultRetcode() != TRADE_RETCODE_PLACED))
     {
      PrintFormat("slot %d entry failed: %d %s", i + 1, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
      return;
     }
   g_inTrade = true; g_dir = dir; g_slotNo = i;
   g_entry = (g_trade.ResultPrice() > 0) ? g_trade.ResultPrice() : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   g_stop = stop; g_exitAt = g_slots[i].exitAt; g_tpMask = 0;
   SaveTrade();
   PrintFormat("ENTRY %s slot %02d:%02d vol=%.2f entry=%.2f SL=%.2f TP=%.2f/%.2f/%.2f/%.2f exit %s",
               dir > 0 ? "BUY" : "SELL", g_slots[i].hh, g_slots[i].mm, vol, g_entry, g_stop,
               g_entry + dir * g_tpDist[0], g_entry + dir * g_tpDist[1], g_entry + dir * g_tpDist[2],
               g_entry + dir * g_tpDist[3], TimeToString(g_exitAt, TIME_SECONDS));
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
   PrintFormat("restored open trade: dir=%d entry=%.2f SL=%.2f TPs sold mask=%d exit %s", g_dir, g_entry, g_stop,
               g_tpMask, TimeToString(g_exitAt, TIME_SECONDS));
  }
//+------------------------------------------------------------------+
