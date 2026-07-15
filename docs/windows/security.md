# Sicurezza read-only

Usare soltanto un conto DEMO e la password investor. L'adapter espone una allowlist di metodi
di lettura e non offre operazioni di apertura, modifica o chiusura. Il test statico scandisce
tutto `windows_agent`. Login, server e password sono blob DPAPI current-user, scritti
atomicamente con DACL limitata all'identità Windows corrente; non passano negli argomenti dei
processi. State e log rifiutano/redigono secret.

Non disabilitare Firewall/Defender, non aprire porte inbound e non eseguire il servizio sotto
un'altra identità senza migrare i blob DPAPI. Ruotare token scrivendo un nuovo blob e
riavviando ordinatamente il worker. I backup dei blob sono utilizzabili solo nello stesso
contesto DPAPI; cifrare comunque il backup e proteggere le ACL.

