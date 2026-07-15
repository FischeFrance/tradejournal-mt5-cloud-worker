# Managed agent

Il client accetta HTTPS (HTTP solo loopback nei test), non segue redirect e rifiuta redirect
cross-host. Il runner persiste soltanto job_id, action, connection_id e stato, mai claim o
token completi. Errori inviati sono nomi di classe sanificati, senza stack trace. Heartbeat è
separato e una lease persa impedisce `complete`. Il wrapper pywin32 è predisposto ma il servizio
non viene installato automaticamente.

