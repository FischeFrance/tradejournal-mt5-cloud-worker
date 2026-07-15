# Live sync

`windows_agent.worker.live_sync` riusa detector, normalizer e snapshot store esistenti e aggiunge
volume/chiusura parziale e nuovi deal (commissione/swap inclusi). L'identity check precede ogni
poll. Event ID deterministici e dedup SQLite rendono il resume idempotente. Polling è
configurabile, senza busy loop, con backoff+jitter limitato e stop pulito. Nei test il sink è
locale; non è configurato alcun endpoint reale.

