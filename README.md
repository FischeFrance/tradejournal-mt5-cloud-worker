# TradeJournal MT5 Cloud Worker

Worker read-only per acquisire dati da MetaTrader 5 e sincronizzarli con
TradeJournal. Il repository contiene un solo percorso operativo: Windows
Server nativo con istanze MT5 portable isolate.

## Architettura corrente

```text
Control plane
  -> Windows Agent
     -> istanza MT5 portable isolata per account
        -> EA MQL5 read-only
           -> file JSON sotto MQL5/Files/TradeJournal
              -> Mql5FileMt5Adapter
                 -> detector / normalizer / outbox
                    -> API TradeJournal
```

Il runtime non usa Docker, Wine o il pacchetto Python `MetaTrader5`.

Decisione architetturale:
[ADR-001 — Windows native file bridge](docs/architecture/ADR-001-windows-native-file-bridge.md).

## Aree del repository

| Percorso | Responsabilità | Stato |
|---|---|---|
| `windows_agent/` | agente, provisioning DPAPI, runtime e lifecycle per account | corrente |
| `worker/` | detector, normalizzatore, outbox e sender condivisi | corrente |
| `mt5/experts/` | EA MQL5 read-only | corrente |
| `contracts/mt5-agent-v1/` | contratto col control plane | corrente, sync manuale |
| `lab/mt5_direct_endpoint/` | harness C0–C5, evidence e prototipi fail-closed | laboratorio |
| `scripts/windows/` | installazione, servizio e diagnostica Windows | corrente |

## Confini di sicurezza

- Solo account DEMO e password investor nei test autorizzati.
- Nessuna primitiva di apertura, modifica o chiusura ordini.
- Nessuna credenziale in argomenti processo, log, metadata o repository.
- Ogni account usa una directory portable dedicata.
- Endpoint scoperti o suggeriti restano `CANDIDATE`.
- Un endpoint diventa `VERIFIED` solo dopo una verifica MT5 esplicita ammessa
  dal registry.
- OpenAI può suggerire il testo di ricerca del broker, ma non verifica endpoint
  e non promuove record.
- Nel laboratorio actual launch, firewall, WFP, bootstrap e registry promotion
  restano `HARD_DISABLED`.

## Onboarding broker

Il flusso previsto è:

```text
server MT4/MT5 fornito dal cliente
  -> resolver AI suggestion-only
  -> ricerca controllata nella GUI "Find your broker"
  -> selezione solo se un unico risultato MT5 contiene l'esatto server
  -> login DEMO/investor separatamente autorizzato
  -> registrazione endpoint verificato con provenance
```

Il nome restituito dall'AI è soltanto un suggerimento di ricerca. Il server
mostrato da MT5 è il dato autorevole. L'automazione GUI reale resta
hard-disabled finché non viene approvato e versionato un profilo UI acquisito
da una build MT5 controllata.

## Avvio sviluppo

Core Python:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

Windows Agent:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe -m pytest tests\windows -q
```

Laboratorio offline:

```bash
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -q
PYTHONPYCACHEPREFIX=/tmp/mt5-pycache python3 -m compileall -q lab/mt5_direct_endpoint
```

I test del laboratorio non autorizzano MT5, credenziali, rete broker,
firewall, WFP o bootstrap.

## Documentazione

- [Architettura Windows](docs/windows/architecture.md)
- [Installazione Windows](docs/windows/installation.md)
- [Sicurezza](docs/windows/security.md)
- [Lifecycle per account](docs/windows/mt5-instance-lifecycle.md)
- [Managed agent](docs/windows/managed-agent.md)
- [Laboratorio direct endpoint](lab/mt5_direct_endpoint/README.md)
- [Modello account/worker del laboratorio](lab/mt5_direct_endpoint/docs/ACCOUNT_WORKER_MODEL.md)

## Stato operativo

Il core Windows, il Coordinator C012, il Job Object, l'Assembler e il Verifier
dispongono di copertura offline. Il percorso pubblico non abilita ancora
l'automazione GUI MT5. Qualunque test reale richiede autorizzazione separata,
ambiente Windows disposable e verifica finale dell'assenza di processi
residui.
