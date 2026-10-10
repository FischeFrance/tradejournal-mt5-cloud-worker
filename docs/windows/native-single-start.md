# Provisioning MT5 con un avvio

Il percorso sperimentale si abilita esplicitamente con
`TRADEJOURNAL_MT5_SINGLE_START_ENABLED=1`. Il default è disabilitato. La factory
del daemon passa il gate e la directory cache a `NativeMt5Runtime`; un canary
isolato può passare `single_start_enabled=True` e
`bootstrap_symbol_cache=percorso_cache_isolato` senza cambiare l'ambiente del
servizio. L'attivazione operativa richiede prima la verifica
con credenziali investor su un'istanza nuova e isolata, senza pubblicare eventi.

Il gate ammette soltanto la build PE 6249. Le altre build, inclusa 6032, conservano
il bootstrap esistente a due avvii. Un terminale non ispezionabile non accede al
percorso sperimentale. Anche il risultato Discovery deve dichiarare 6249 prima
che venga pubblicato il marker che abilita il Bridge.

Per accedere al percorso serve inoltre un hint verificato per lo stesso server
canonico, build PE e preferenza di simbolo. La factory usa
`instances_root.parent/state/broker-bootstrap-symbols`, normalmente
`C:\TradeJournal\state\broker-bootstrap-symbols`. Senza cache configurata o con
hint assente, corrotto, scaduto o non coincidente, viene usato il bootstrap legacy
a due avvii. Non esiste un suffisso broker applicato globalmente.

La cache contiene soltanto server canonico, build, preferenza richiesta, simbolo
reale verificato e timestamp. Un record è valido per sette giorni; timestamp oltre
cinque minuti nel futuro sono rifiutati. Ogni chiave ha un file JSON atomico
distinto, protetto su Windows da ACL SYSTEM/Administrators; symlink e reparse point
non sono accettati. Un avvio legacy completato apprende l'hint dopo Discovery,
identità/readonly/connessione del produttore e coincidenza della build Discovery
con il PE. Un errore di cache non rende fallita una connessione verificata.

Il primo avvio riceve il login e la password tramite il consueto INI protetto,
con Discovery sul chart gestito e simbolo iniziale scelto dall'hint. La preferenza
richiesta, per esempio `EURUSD`, resta distinta dal simbolo reale del chart,
per esempio il suffisso verificato soltanto per quel broker. Discovery verifica
nuovamente il catalogo: la cache non autorizza l'account. Il runtime verifica autenticazione, persistenza
cifrata dell'account, sincronizzazione investor e risultato Discovery correlato
all'istanza/login/server. Soltanto allora pubblica template e handoff Bridge.
L'heartbeat finale deve confermare identità, connessione e accesso readonly.
Il PID restituito è quello del produttore finale, anche dopo un eventuale retry.

Un timeout di autenticazione, sincronizzazione, Discovery o readiness consente
un solo secondo avvio della medesima istanza riscaldata. Il primo processo viene
fermato e verificato; configurazione, profilo, esempi generati e marker precedenti
vengono puliti prima di usare l'eventuale simbolo reale presente nella cache.
Il totale massimo è due avvii: il fallback non richiama il bootstrap legacy.
Credenziali rifiutate, accesso master, identità incoerente, endpoint non valido,
Discovery malformato, build diversa, crash, cleanup fallito e cancellazione della
lease non attivano il retry. Le configurazioni contenenti credenziali vengono
rimosse anche in caso di errore; la password resta in memoria soltanto durante
il tentativo e l'eventuale retry.

I polling dei due tentativi condividono il budget `timeout`; una fase non
riavvia il conteggio. Le operazioni Windows sincrone, fra cui `schtasks /Delete`,
mantengono il comportamento precedente e possono ancora occupare tempo fuori
dai limiti dei polling. Il cleanup del processo resta necessario anche quando
il budget è scaduto. `resume` e il runtime della manutenzione non cambiano.

Il bootstrap originario è un workaround documentato nei commit del luglio 2026:
`cec69b5` e `c84e0ab` descrivono il blocco oltre la ricompilazione sulla build6032;
`03cddfc` separa login, Discovery e Loader; `748b5f6` riduce tre avvii a due tramite
handoff sullo stesso chart. Questi resoconti motivano la compatibilità legacy;
non dimostrano che 6249 richieda il bootstrap separato. I test locali verificano
controlli e lifecycle; la compatibilità del cold start con il broker va confermata
nel canary operativo prima dell'attivazione.

## Esito del canary del 10 ottobre 2026

Il primo cold start isolato su FPMTrading-Live, build 6249, con chart `EURUSD`,
ha accettato le credenziali investor ma non ha pubblicato Discovery entro il
budget di 180 secondi. L'istanza è stata fermata e rimossa; PID e tempi di
creazione delle sei istanze preesistenti sono rimasti invariati.

Il journal mostra Discovery caricato e l'accesso investor confermato mentre
MetaEditor continuava la ricompilazione degli esempi rigenerati dal terminale.
Questo riscontro non dimostra che la ricompilazione sia la causa del timeout o
che le connessioni 6249 richiedano due avvii.

Il successivo canary con chart reale `EURUSD.raw` e tutti gli helper del runtime
inclusi ha completato autenticazione, Discovery, heartbeat con identità corretta,
readonly e connessione in un solo avvio: 88,578 secondi complessivi. Le sei istanze
preesistenti hanno mantenuto PID e tempi di creazione. Il canary è stato fermato e
rimosso senza chiamate API. Questo conferma il percorso per FPM con quel chart;
La variante finale con cache ha poi mantenuto la preferenza `EURUSD`, ricavato
`EURUSD.raw` dall'hint e ottenuto la conferma di Discovery nello stesso avvio:
87,641 secondi, un solo processo avviato, account readonly e produttore connesso.
Anche questa istanza è stata fermata e rimossa; i sei terminali esistenti sono
rimasti invariati. Il gate del servizio era disabilitato durante queste prove isolate.

La misura di `_release_interactive_task` è stata 0,063 secondi. In questo test il
ritardo non è attribuibile alla cancellazione dell'attività pianificata; non è
quindi confermata la precedente ipotesi di una pausa di circa 60 secondi lì.
I polling e l'eventuale retry sono verificati nei test locali, ma il canary a
180 secondi ha esaurito il budget prima di poter usare il secondo tentativo.
I canary non hanno modificato il servizio né le istanze attive. L'attivazione del
gate del daemon è un'operazione di rilascio separata, con verifica del manifest,
adozione dei processi esistenti e possibilità di rollback del solo servizio.
