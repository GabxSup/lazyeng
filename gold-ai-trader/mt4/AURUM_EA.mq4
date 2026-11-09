//+------------------------------------------------------------------+
//| AURUM_EA.mq4 - local AURUM AI bridge for MetaTrader 4            |
//|                                                                  |
//| Attach to an XAUUSD chart. The EA polls the dashboard on         |
//| localhost port 80 (MQL4 WebRequest supports http port 80 only).  |
//| It only manages orders with InpMagic.                           |
//|                                                                  |
//| MQL4 is Windows software; on macOS the official MetaTrader 4     |
//| package runs it through Wine.                                    |
//+------------------------------------------------------------------+
#property strict
#property copyright "AURUM AI"
#property link      ""
#property version   "1.00"
#property description "Local MT4 bridge for AURUM AI. Use on a demo account first."

input string InpDashboardURL = "http://127.0.0.1/api/ea/poll"; // dashboard URL (port 80!)
input string InpToken        = "";                             // AURUM_EA_TOKEN from .env
input int    InpMagic        = 20261101;                       // AURUM order magic
input bool   InpAllowLive    = false;                          // must be true for real accounts
input int    InpPollSeconds  = 1;                              // poll interval
input int    InpHistoryCount = 25;                             // recent closed orders to send

string g_results[128];
int    g_resultCount = 0;
string g_candles[1024];
int    g_candleCount = 0;
datetime g_lastErrorPrint = 0;
datetime g_lastPollOk = 0;
bool   g_announced = false;

//+------------------------------------------------------------------+
int OnInit()
{
   if(StringLen(InpToken) < 16)
   {
      Print("AURUM EA ERROR: InpToken is empty or too short. Copy AURUM_EA_TOKEN from the dashboard .env.");
      return(INIT_PARAMETERS_INCORRECT);
   }
   if(StringFind(InpDashboardURL, ":8080", 0) >= 0)
      Print("AURUM EA WARNING: MQL4 WebRequest uses port 80 for http://. Use http://127.0.0.1/api/ea/poll, not :8080.");
   if(StringFind(InpDashboardURL, "http://", 0) != 0)
      Print("AURUM EA WARNING: use an http:// dashboard URL. MQL4 WebRequest does not support arbitrary https setups.");
   EventSetTimer((int)MathMax(1, InpPollSeconds));
   Print("AURUM EA started on ", Symbol(), " magic ", InpMagic,
         ". Add http://127.0.0.1 to Tools > Options > Expert Advisors > Allow WebRequest for listed URL.");
   Print("Live orders on a real account are ", (InpAllowLive ? "ENABLED" : "BLOCKED"),
         ". Dashboard arming is still required.");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("AURUM EA stopped.");
}

void OnTick()
{
   // The bridge is timer-driven so it does not depend on incoming ticks.
}

void OnTimer()
{
   PollDashboard();
}

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string Safe(string value)
{
   StringReplace(value, "|", "/");
   StringReplace(value, "\r", " ");
   StringReplace(value, "\n", " ");
   return(value);
}

string D2S(double value, int digits)
{
   return(DoubleToString(value, digits));
}

int PeriodFromName(string name)
{
   if(name == "M5")  return(PERIOD_M5);
   if(name == "M15") return(PERIOD_M15);
   if(name == "M30") return(PERIOD_M30);
   if(name == "H1")  return(PERIOD_H1);
   return(0);
}

void AddResult(string id, bool ok, int ticket, double price, double lots, double profit, string message)
{
   if(g_resultCount >= 128)
   {
      Print("AURUM EA: result queue full; dropping a result.");
      return;
   }
   g_results[g_resultCount] = "RESULT|" + id + "|" + (ok ? "1" : "0") + "|" +
                              IntegerToString(ticket) + "|" + D2S(price, 2) + "|" +
                              D2S(lots, 2) + "|" + D2S(profit, 2) + "|" + Safe(message);
   g_resultCount++;
}

void AddCandle(string id, string timeframe, datetime stamp, double o, double h, double l, double c, double volume, int digits)
{
   if(g_candleCount >= 1024)
      return;
   g_candles[g_candleCount] = "CANDLE|" + id + "|" + timeframe + "|" +
                              IntegerToString((int)stamp) + "|" + D2S(o, digits) + "|" +
                              D2S(h, digits) + "|" + D2S(l, digits) + "|" +
                              D2S(c, digits) + "|" + D2S(volume, 0);
   g_candleCount++;
}

void LogError(string message)
{
   datetime now = TimeCurrent();
   if(now - g_lastErrorPrint >= 30)
   {
      g_lastErrorPrint = now;
      Print("AURUM EA: ", message);
   }
}

bool TradingReady(string &why)
{
   if(!IsTradeAllowed())
   {
      why = "AutoTrading is disabled in MT4";
      return(false);
   }
   if(IsTradeContextBusy())
   {
      why = "trade context is busy";
      return(false);
   }
   if(!IsDemo() && !InpAllowLive)
   {
      why = "real account blocked by InpAllowLive=false";
      return(false);
   }
   return(true);
}

//+------------------------------------------------------------------+
//| Snapshot                                                         |
//+------------------------------------------------------------------+
string BuildSnapshot()
{
   RefreshRates();
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   if(digits <= 0) digits = 2;
   string body = "AURUM|1\r\n";
   body += "SERVERTIME|" + IntegerToString((int)TimeCurrent()) + "\r\n";
   body += "SYMBOL|" + Safe(Symbol()) + "\r\n";
   body += "ACCOUNT|" + IntegerToString(AccountNumber()) + "|" +
           D2S(AccountBalance(), 2) + "|" + D2S(AccountEquity(), 2) + "|" +
           D2S(AccountMargin(), 2) + "|" + D2S(AccountFreeMargin(), 2) + "|" +
           IntegerToString(AccountLeverage()) + "|" + Safe(AccountCurrency()) + "|" +
           Safe(AccountName()) + "|" + Safe(AccountServer()) + "|" +
           Safe(AccountCompany()) + "|" + (IsDemo() ? "demo" : "real") + "\r\n";
   double bid = MarketInfo(Symbol(), MODE_BID);
   double ask = MarketInfo(Symbol(), MODE_ASK);
   body += "QUOTE|" + Safe(Symbol()) + "|" + D2S(bid, digits) + "|" + D2S(ask, digits) + "\r\n";

   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderMagicNumber() != InpMagic || OrderSymbol() != Symbol()) continue;
      int type = OrderType();
      if(type != OP_BUY && type != OP_SELL) continue;
      double current = (type == OP_BUY ? MarketInfo(Symbol(), MODE_BID) : MarketInfo(Symbol(), MODE_ASK));
      body += "POS|" + IntegerToString(OrderTicket()) + "|" + IntegerToString(type) + "|" +
              D2S(OrderLots(), 2) + "|" + D2S(OrderOpenPrice(), digits) + "|" +
              D2S(current, digits) + "|" + D2S(OrderStopLoss(), digits) + "|" +
              D2S(OrderTakeProfit(), digits) + "|" + D2S(OrderProfit(), 2) + "|" +
              IntegerToString((int)OrderOpenTime()) + "|" + IntegerToString(OrderMagicNumber()) + "|" +
              Safe(OrderComment()) + "\r\n";
   }

   int historyTotal = OrdersHistoryTotal();
   int addedClosed = 0;
   int maxClosed = (int)MathMax(1, MathMin(InpHistoryCount, 50));
   datetime cutoff = TimeCurrent() - 7 * 86400;
   for(int j = historyTotal - 1; j >= 0 && addedClosed < maxClosed; j--)
   {
      if(!OrderSelect(j, SELECT_BY_POS, MODE_HISTORY)) continue;
      if(OrderMagicNumber() != InpMagic || OrderSymbol() != Symbol()) continue;
      int htype = OrderType();
      if(htype != OP_BUY && htype != OP_SELL) continue;
      if(OrderCloseTime() < cutoff) continue;
      body += "CLOSED|" + IntegerToString(OrderTicket()) + "|" +
              (htype == OP_BUY ? "BUY" : "SELL") + "|" + D2S(OrderLots(), 2) + "|" +
              D2S(OrderOpenPrice(), digits) + "|" + D2S(OrderClosePrice(), digits) + "|" +
              D2S(OrderProfit(), 2) + "|" + D2S(OrderCommission(), 2) + "|" +
              D2S(OrderSwap(), 2) + "|" + IntegerToString((int)OrderOpenTime()) + "|" +
              IntegerToString((int)OrderCloseTime()) + "\r\n";
      addedClosed++;
   }

   for(int r = 0; r < g_resultCount; r++)
      body += g_results[r] + "\r\n";
   for(int k = 0; k < g_candleCount; k++)
      body += g_candles[k] + "\r\n";
   return(body);
}

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
int PostSnapshot(const string body, string &response)
{
   char request[];
   char result[];
   int length = StringToCharArray(body, request, 0, StringLen(body), CP_UTF8);
   if(length <= 0)
   {
      LogError("could not encode request");
      return(-1);
   }
   if(length > 1)
      ArrayResize(request, length - 1);
   string headers = "Content-Type: text/plain\r\nX-EA-Token: " + InpToken + "\r\n";
   string responseHeaders = "";
   ResetLastError();
   int status = WebRequest("POST", InpDashboardURL, headers, 3000, request, result, responseHeaders);
   if(status == -1)
   {
      LogError("WebRequest failed (error " + IntegerToString(GetLastError()) +
               "). Add http://127.0.0.1 to the WebRequest allowlist.");
      return(-1);
   }
   if(status != 200)
   {
      LogError("dashboard returned HTTP " + IntegerToString(status) + ". Check the EA token and dashboard URL.");
      return(status);
   }
   int size = ArraySize(result);
   while(size > 0 && result[size - 1] == 0)
      size--;
   response = CharArrayToString(result, 0, size, CP_UTF8);
   return(200);
}

//+------------------------------------------------------------------+
//| Commands                                                         |
//+------------------------------------------------------------------+
void ExecuteOpen(string id, string side, string lotsText, string slText, string tpText, string symbol)
{
   if(symbol != Symbol())
   {
      AddResult(id, false, 0, 0, 0, 0, "EA chart symbol does not match order symbol");
      return;
   }
   string why = "";
   if(!TradingReady(why))
   {
      AddResult(id, false, 0, 0, 0, 0, why);
      return;
   }
   double lots = StringToDouble(lotsText);
   double sl = StringToDouble(slText);
   double tp = StringToDouble(tpText);
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   if(digits <= 0) digits = 2;
   double point = MarketInfo(Symbol(), MODE_POINT);
   double step = MarketInfo(Symbol(), MODE_LOTSTEP);
   double minLot = MarketInfo(Symbol(), MODE_MINLOT);
   double maxLot = MarketInfo(Symbol(), MODE_MAXLOT);
   if(step <= 0) step = 0.01;
   lots = MathFloor(lots / step + 0.0000001) * step;
   lots = NormalizeDouble(lots, 2);
   if(lots < minLot || lots > maxLot || lots <= 0)
   {
      AddResult(id, false, 0, 0, 0, 0, "invalid lot size");
      return;
   }
   RefreshRates();
   double price = (side == "BUY" ? MarketInfo(Symbol(), MODE_ASK) : MarketInfo(Symbol(), MODE_BID));
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);
   double stopLevel = MarketInfo(Symbol(), MODE_STOPLEVEL) * point;
   if(side == "BUY")
   {
      if(!(sl < price && tp > price && price - sl >= stopLevel && tp - price >= stopLevel))
      {
         AddResult(id, false, 0, 0, 0, 0, "invalid BUY bracket or stop distance");
         return;
      }
   }
   else if(side == "SELL")
   {
      if(!(sl > price && tp < price && sl - price >= stopLevel && price - tp >= stopLevel))
      {
         AddResult(id, false, 0, 0, 0, 0, "invalid SELL bracket or stop distance");
         return;
      }
   }
   else
   {
      AddResult(id, false, 0, 0, 0, 0, "invalid side");
      return;
   }
   double marginPerLot = MarketInfo(Symbol(), MODE_MARGINREQUIRED);
   if(marginPerLot > 0 && marginPerLot * lots > AccountFreeMargin())
   {
      AddResult(id, false, 0, 0, 0, 0, "insufficient free margin");
      return;
   }
   int type = (side == "BUY" ? OP_BUY : OP_SELL);
   color arrow = (side == "BUY" ? clrBlue : clrRed);
   int ticket = OrderSend(Symbol(), type, lots, price, 30, sl, tp, "AURUM " + id, InpMagic, 0, arrow);
   if(ticket <= 0)
   {
      AddResult(id, false, 0, 0, 0, 0, "OrderSend error " + IntegerToString(GetLastError()));
      return;
   }
   AddResult(id, true, ticket, price, lots, 0, "filled");
}

void ExecuteClose(string id, string ticketText)
{
   int ticket = (int)StringToInteger(ticketText);
   string why = "";
   if(!TradingReady(why))
   {
      AddResult(id, false, 0, 0, 0, 0, why);
      return;
   }
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES) || OrderMagicNumber() != InpMagic || OrderSymbol() != Symbol())
   {
      AddResult(id, false, 0, 0, 0, 0, "order not found for AURUM magic");
      return;
   }
   int type = OrderType();
   if(type != OP_BUY && type != OP_SELL)
   {
      AddResult(id, false, 0, 0, 0, 0, "not a market position");
      return;
   }
   RefreshRates();
   double price = (type == OP_BUY ? MarketInfo(Symbol(), MODE_BID) : MarketInfo(Symbol(), MODE_ASK));
   double lots = OrderLots();
   double profit = OrderProfit() + OrderCommission() + OrderSwap();
   if(!OrderClose(ticket, lots, price, 30, clrGray))
   {
      AddResult(id, false, 0, 0, 0, 0, "OrderClose error " + IntegerToString(GetLastError()));
      return;
   }
   AddResult(id, true, ticket, price, lots, profit, "closed");
}

void ExecuteModify(string id, string ticketText, string slText, string tpText)
{
   int ticket = (int)StringToInteger(ticketText);
   string why = "";
   if(!TradingReady(why))
   {
      AddResult(id, false, 0, 0, 0, 0, why);
      return;
   }
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES) || OrderMagicNumber() != InpMagic || OrderSymbol() != Symbol())
   {
      AddResult(id, false, 0, 0, 0, 0, "order not found for AURUM magic");
      return;
   }
   int type = OrderType();
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   if(digits <= 0) digits = 2;
   double point = MarketInfo(Symbol(), MODE_POINT);
   double stopLevel = MarketInfo(Symbol(), MODE_STOPLEVEL) * point;
   double sl = NormalizeDouble(StringToDouble(slText), digits);
   double tp = NormalizeDouble(StringToDouble(tpText), digits);
   RefreshRates();
   double bid = MarketInfo(Symbol(), MODE_BID);
   double ask = MarketInfo(Symbol(), MODE_ASK);
   if(type == OP_BUY)
   {
      if(!(sl < bid && tp > bid && bid - sl >= stopLevel && tp - bid >= stopLevel))
      {
         AddResult(id, false, 0, 0, 0, 0, "invalid BUY protection update");
         return;
      }
   }
   else
   {
      if(!(sl > ask && tp < ask && sl - ask >= stopLevel && ask - tp >= stopLevel))
      {
         AddResult(id, false, 0, 0, 0, 0, "invalid SELL protection update");
         return;
      }
   }
   if(!OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrGray))
   {
      AddResult(id, false, 0, 0, 0, 0, "OrderModify error " + IntegerToString(GetLastError()));
      return;
   }
   AddResult(id, true, ticket, 0, 0, 0, "protection updated");
}

void ExecuteCandles(string id, string timeframe, string countText)
{
   int period = PeriodFromName(timeframe);
   if(period == 0)
   {
      AddResult(id, false, 0, 0, 0, 0, "unsupported timeframe");
      return;
   }
   int count = (int)StringToInteger(countText);
   if(count < 1) count = 1;
   if(count > 500) count = 500;
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   if(digits <= 0) digits = 2;
   int added = 0;
   for(int i = count - 1; i >= 0; i--)
   {
      datetime stamp = iTime(Symbol(), period, i);
      if(stamp <= 0) continue;
      AddCandle(id, timeframe, stamp, iOpen(Symbol(), period, i), iHigh(Symbol(), period, i),
                iLow(Symbol(), period, i), iClose(Symbol(), period, i), iVolume(Symbol(), period, i), digits);
      added++;
   }
   if(added <= 0)
   {
      AddResult(id, false, 0, 0, 0, 0, "no candles; load the XAUUSD chart history");
      return;
   }
   AddResult(id, true, 0, 0, 0, 0, "candles");
}

void ProcessCommands(string response)
{
   if(StringLen(response) == 0) return;
   string lines[];
   int count = StringSplit(response, StringGetCharacter("\n", 0), lines);
   for(int i = 0; i < count; i++)
   {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringFind(line, "CMD|", 0) != 0) continue;
      string parts[];
      int n = StringSplit(line, StringGetCharacter("|", 0), parts);
      if(n < 4) continue;
      string id = parts[1];
      string action = parts[2];
      if(action == "OPEN" && n >= 8)
         ExecuteOpen(id, parts[3], parts[4], parts[5], parts[6], parts[7]);
      else if(action == "CLOSE" && n >= 4)
         ExecuteClose(id, parts[3]);
      else if(action == "MODIFY" && n >= 6)
         ExecuteModify(id, parts[3], parts[4], parts[5]);
      else if(action == "CANDLES" && n >= 5)
         ExecuteCandles(id, parts[3], parts[4]);
      else
         AddResult(id, false, 0, 0, 0, 0, "unknown command");
   }
}

//+------------------------------------------------------------------+
void PollDashboard()
{
   if(StringLen(InpToken) < 16) return;
   string body = BuildSnapshot();
   string response = "";
   int status = PostSnapshot(body, response);
   if(status != 200) return;
   g_resultCount = 0;
   g_candleCount = 0;
   if(!g_announced)
   {
      g_announced = true;
      Print("AURUM EA: dashboard connection established.");
   }
   ProcessCommands(response);
}
//+------------------------------------------------------------------+
