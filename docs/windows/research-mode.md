# Research mode

Disabilitato per default. Solo una decisione server-side allowlisted può abilitarlo; una
richiesta client da sola viene rifiutata. La fixture SQLite conserva simbolo, timeframe,
timestamp UTC, bid/ask, spread, OHLC, tick volume e real volume. Il precedente adapter
PostgreSQL in `worker/market_data_store.py` è preservato. Nessuna raccolta reale parte durante
bootstrap o POC.

