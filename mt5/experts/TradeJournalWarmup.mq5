//+------------------------------------------------------------------+
//| TradeJournalWarmup.mq5                                           |
//|                                                                  |
//| EA minimale usato solo per aprire un grafico e consentire a MT5  |
//| di completare la sincronizzazione iniziale dell'account.          |
//| Non chiama MAI OrderSend o altre funzioni di trading, non importa|
//| DLL e non legge ne' scrive dati dell'account.                     |
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property version   "1.00"
#property strict

void ProbeAccountCache()
  {
   datetime to_time = TimeCurrent();
   HistorySelect(to_time - 3600, to_time);
   PositionsTotal();
   OrdersTotal();
  }

int OnInit()
  {
   ProbeAccountCache();
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTick()
  {
  }

void OnTimer()
  {
   ProbeAccountCache();
  }
