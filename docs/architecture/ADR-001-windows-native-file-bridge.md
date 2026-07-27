# ADR-001 — Windows native file bridge

- Stato: Accepted
- Data: 2026-07-27

## Contesto

Il progetto ha sperimentato tre percorsi per leggere MT5:

1. Python Linux più Wine e pacchetto `MetaTrader5`;
2. bridge HTTP attorno a un runtime Wine;
3. Windows nativo con un'istanza portable per account e file prodotti da un
   EA MQL5 read-only.

Il pacchetto Python `MetaTrader5` ha mostrato incompatibilità IPC con build
recenti del terminale. Mantenere più percorsi implicitamente equivalenti rende
documentazione, test, incident response e gestione delle credenziali ambigui.

## Decisione

Il percorso canonico è:

```text
Windows Agent
  -> NativeMt5Runtime
     -> terminal64.exe /portable per account
        -> TradeJournalBridge.ex5 read-only
           -> MQL5/Files/TradeJournal
              -> Mql5FileMt5Adapter
                 -> core worker condiviso
```

`windows_agent.real_handlers.build_real_handlers()` deve continuare a
selezionare questo percorso quando non viene iniettato un adapter esplicito.

Il laboratorio `lab/mt5_direct_endpoint` non è produzione. Dimostra proprietà
di isolamento, lifecycle, evidence e onboarding senza modificare il runtime
pubblico.

## Onboarding e discovery

- L'AI può ricevere esclusivamente un server non segreto e restituire un
  suggerimento di broker e testo di ricerca.
- La label AI non è autorevole.
- La GUI MT5 può selezionare un risultato solo quando esiste un unico candidato
  contenente l'esatto server richiesto.
- Un risultato pubblico o AI non può diventare `VERIFIED`.
- La promozione richiede un metodo di verifica MT5 ammesso e artifact
  provenance valida.

## Percorsi rimossi

Docker/Wine, provisioning systemd, bridge HTTP, ricerca market-data e adapter
Python IPC diretto sono stati rimossi dopo la validazione del percorso Windows.
Non esiste quindi un fallback implicito capace di aggirare isolamento,
pinning dei binari o gestione DPAPI.

## Conseguenze

- La CI deve separare core, Windows Agent e laboratorio.
- La documentazione non deve presentare il wheel `MetaTrader5` come default.
- I componenti product-like nati nel laboratorio devono ottenere un contratto
  stabile prima di essere integrati nel Windows Agent.
- Actual launch del Wizard resta hard-disabled finché selettori, build MT5 e
  rollback non sono validati su Windows.
