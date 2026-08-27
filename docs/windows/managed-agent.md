# Managed agent

Il client accetta HTTPS (HTTP solo loopback nei test), non segue redirect e rifiuta redirect
cross-host. Il runner persiste soltanto job_id, action, connection_id e stato, mai claim o
token completi. Errori inviati sono nomi di classe sanificati, senza stack trace. Heartbeat è
separato e una lease persa impedisce `complete`. Il wrapper pywin32 è predisposto ma il servizio
non viene installato automaticamente.

La credenziale lunga `tjagent_...` resta nel DPAPI e viene usata solo per ottenere una sessione
Realtime breve. Il JWT di sessione contiene UUID e scope dell'agente, scade dopo un'ora e apre
esclusivamente i topic privati `mt5-agent:any` e `mt5-agent:<uuid>`. I messaggi Broadcast non
contengono credenziali né payload del job: comunicano solo che la coda durevole va riletta.
