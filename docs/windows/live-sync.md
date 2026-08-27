# Live sync

`windows_agent.worker.live_sync` riusa detector e normalizer come operazione singola, non come
loop remoto. `Mt5EventSupervisor` la esegue quando l'EA pubblica un file evento atomico. L'identity
check precede ogni snapshot; event ID deterministici, dedup SQLite e outbox JSON atomico rendono
il resume idempotente e lossless anche durante un'interruzione di rete.

Il file heartbeat dell'EA resta un controllo locale economico. Solo una variazione effettiva
`connected -> disconnected` o `disconnected -> connected` viene accodata verso
`trading-mt5-events`; un account quieto genera zero chiamate HTTP.
