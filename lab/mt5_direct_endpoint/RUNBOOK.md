# MT5 Direct Endpoint Lab — Runbook

## Stato autorizzato

```text
OFFLINE CONTRACT/ASSEMBLER/VERIFIER: GO
INNOCUOUS C012/JOB OBJECT SMOKE: GO
UI AUTOMATION: PARTIALLY_READY, HARD_DISABLED
MT5 / CREDENTIALS / BROKER NETWORK: NO-GO SENZA AUTORIZZAZIONE
FIREWALL / WFP / BOOTSTRAP / PROMOTION: NO-GO
```

## 1. Pre-flight offline

1. Verificare working tree e branch.
2. Verificare l'assenza di `terminal`, `terminal64`, `metaeditor`,
   `metaeditor64`, `metatester` e `metatester64`.
3. Non usare file runtime, credenziali, endpoint reali o directory di account.
4. Eseguire prima i test mirati, poi la suite completa una sola volta.

## 2. Suite

```bash
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -q
PYTHONPYCACHEPREFIX=/tmp/mt5-pycache python3 -m compileall -q lab/mt5_direct_endpoint
```

## 3. C012 innocuo

Il solo launcher reale ammesso dal test è `self-sleeper`. C0 crea il Job e la
root generation, C1 interroga la stessa sessione e C2 esegue teardown. Il
contratto richiede `C012_SINGLE_PROCESS_SESSION`,
`initial_c012_pre_state_sha256` e timeline/QPC coerenti.

Il comando pubblico `c012-host start` usa il launcher non implementato. Il
verbo `start-innocuous` è test-only. Non sostituirne il target con un percorso
arbitrario.

## 4. Evidence offline

L'Assembler legge soltanto path dichiarati nel manifest controllato e produce
atomicamente evidence, artifact manifest e report. Il Verifier pubblico:

- ricomputa digest e canonical JSON;
- verifica provenance e preimage;
- non usa il verdetto del produttore;
- rifiuta path assoluti, traversal, symlink e run ID invalidi.

Il descriptor del candidate handoff vincola
`direct_campaign_manifest_sha256`. L'identità del probe include
`probe_source_sha256`, ma source e binary non sono considerati equivalenti
senza verifica di deployment.

## 5. Endpoint registry e config

Un record `VERIFIED` richiede:

- endpoint valido;
- metodo MT5 allowlisted;
- artifact path relativo;
- digest presente e corrispondente;
- sessione di verifica valida;
- TTL non scaduto.

`CANDIDATE`, `METAQUOTES_CDN`, `EXPIRED`, digest alterati o più record
`VERIFIED` correnti sono rifiutati.

Il dry-run genera un config temporaneo senza account/password, descrive
`/portable /config:<file>` e cancella il file al termine. Non lancia MT5.

## 6. Resolver e Wizard

Il resolver accetta soltanto un server non segreto e restituisce una
suggestion. L'output non può contenere `PASS`, `VERIFIED` o autorizzazione alla
promozione.

Il Wizard:

1. usa `SearchText`;
2. legge candidati e server dalla UI;
3. seleziona solo un unico candidato contenente `ExpectedServerName`;
4. tratta `SuggestedBrokerLabel` come non autorevole;
5. verifica nuovamente i server censiti dopo la selezione;
6. fallisce in sicurezza su zero, più risultati o dati invalidi.

Actual UI automation resta `HARD_DISABLED`: nessun runbook offline può
abilitarla.

## 7. Abort e cleanup

Ogni errore deve:

- impedire pubblicazioni parziali;
- chiudere handle e processi innocui;
- lasciare lo stato `FAILED_CLOSED` quando richiesto;
- non includere command line, variabili d'ambiente o secret nei metadata.

Un test Windows termina soltanto dopo la verifica dell'assenza di tutti i
processi MT5/MetaEditor/MetaTester.
