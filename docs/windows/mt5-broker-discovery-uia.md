# Phase 0: broker discovery con Windows UI Automation

La Phase 0 usa il terminale MetaTrader 5 ufficiale come motore di discovery e pilota soltanto i
controlli Windows UI Automation. Non replica il protocollo MetaQuotes, non usa coordinate e non
riceve login, account, password, token o envelope cifrati.

Il modulo è `windows_agent.worker.mt5_broker_discovery`. L'API diretta, destinata a un processo già
in una sessione Windows interattiva, è:

```python
WindowsMt5BrokerDiscovery(adapter=None).discover(
    terminal_path=terminal_path,
    candidate_pids=(pid,),
    expected_server="Broker-Live",
    queries=("Broker Ltd", "Broker"),
    timeout_seconds=30,
)
```

Il percorso deve terminare in `terminal64.exe`. I PID sono verificati contro quel percorso prima di
collegarsi alla finestra. Le query vengono eseguite nell'ordine ricevuto; un risultato viene
accettato soltanto quando, dopo normalizzazione Unicode, spazi e maiuscole, esiste un unico server
uguale a `expected_server`. Non sono ammessi match parziali o fuzzy. Zero risultati, duplicati,
timeout e layout UI inattesi falliscono in modo chiuso e con messaggi sanificati.

## Helper per la sessione interattiva

Un servizio Windows gira in Session 0 e non può controllare direttamente il desktop dell'utente.
Quando `TRADEJOURNAL_MT5_INTERACTIVE_USER` è configurato, il runtime crea un secondo scheduled task
`/IT` nella stessa identità interattiva ed esegue:

```powershell
python -m windows_agent.worker.mt5_broker_discovery `
  --request C:\ProgramData\TradeJournal\discovery-request.json `
  --result C:\ProgramData\TradeJournal\discovery-result.json
```

La request JSON ammette esclusivamente:

```json
{
  "terminal_path": "C:\\...\\terminal64.exe",
  "candidate_pids": [1234],
  "expected_server": "Broker-Live",
  "queries": ["Broker Ltd", "Broker"],
  "timeout_seconds": 30
}
```

Chiavi aggiuntive vengono rifiutate. Il risultato atomico contiene soltanto `ok`, `code`, il
messaggio sanificato in caso di errore e, in caso di successo, il server canonico. L'integrazione è
sorvegliata da `NativeMt5Runtime`: crea request e launcher senza credenziali, attende il risultato
con timeout e cancella task e file in ogni esito. Se l'agente gira già nella sessione desktop,
chiama invece l'adapter direttamente.

I codici d'uscita sono `0` per successo, `2` per request non valida, `3` per assenza del match
esatto, `4` per match ambiguo, `5` per timeout e `6` per terminale/finestra/UI non riconosciuti o
errore interno. Il risultato non ripete percorso, PID o query in caso di errore.

## Limiti intenzionali

- L'adapter iniziale riconosce i nomi inglesi `Open an Account`, `Find your broker` e `Next`.
  Localizzazioni o nuove gerarchie UIA falliscono come `ui_unknown` invece di tentare coordinate.
- Alcune build o terminali personalizzati possono non esporre righe e server come controlli UIA.
- La discovery aggiorna la cache della singola istanza; non autorizza la copia di `servers.dat` tra
  clienti o build.
- Dopo la discovery restano obbligatori login tramite terminale ufficiale, match esatto del server
  restituito dall'account e verifica investor/read-only.
- `pywinauto` viene importato in modo lazy. La logica di normalizzazione e selezione è testabile con
  adapter falsi anche su macOS/Linux.

`pywinauto` 0.6.9 usa licenza BSD-3-Clause. Non introduce canoni o licenze commerciali; restano i
costi operativi della macchina e il gate EULA per un servizio gestito multi-cliente.
