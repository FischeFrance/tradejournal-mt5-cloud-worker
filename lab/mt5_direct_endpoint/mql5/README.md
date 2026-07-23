# MT5 identity probe one-shot

`TradeJournalIdentityProbe.mq5` e' un Expert Advisor minimale, read-only e
riservato al laboratorio C0-C5. Non fa discovery, non apre socket, non carica
DLL, non condivide file tramite `FILE_COMMON` e non usa API di trading. Il
terminale resta l'unico processo che potra' effettuare connessioni nella futura
esecuzione autorizzata; il probe osserva soltanto proprieta' esposte da MQL5.

## Input effimeri e separati

Prima di collegare il probe, l'orchestratore dovra' creare due file distinti
nella sandbox della singola istanza MT5:

```text
MQL5\Files\MT5DirectEndpointLab\expected-account.txt
MQL5\Files\MT5DirectEndpointLab\run-id.txt
```

`expected-account.txt` contiene soltanto il numero dell'account demo atteso in
cifre ASCII. E' un identificatore sensibile: deve avere ACL ristrette, non deve
entrare in trace/report e non deve essere riutilizzato. Il probe lo legge una
volta, tenta immediatamente di eliminarlo prima di validarlo e conserva il
numero soltanto per il confronto numerico in memoria. Il login non viene mai
convertito in stringa, scritto nel JSON o inviato a `Print()`.

`run-id.txt` contiene un UUID opaco, non sensibile, canonico lowercase (versione
1-5 e variante RFC 4122), per esempio `550e8400-e29b-41d4-a716-446655440000`.
Anche questo file viene consumato e cancellato. Il run ID non deve derivare da
account, broker, endpoint o credenziali e deve essere generato separatamente per
ogni prova. Il valore `00000000-0000-4000-8000-000000000000` e' una sentinella
riservata: non e' un input valido e puo' comparire soltanto nell'evidenza di un
errore di input/pubblicazione, per rendere serializzabile un errore avvenuto
prima del binding.

La cancellazione non costituisce secure erase. A fine prova il clone VM/disco
che ha contenuto input e configurazione di bootstrap dovra' essere distrutto
crittograficamente secondo il runbook del laboratorio.

## Timeout e macchina a stati

`InpTimeoutSeconds` e' configurabile tra 1 e 3600 secondi; il default e' 120.
Il tempo trascorso e' misurato con il contatore monotono di MT5. Prima del
deadline non viene scritto alcun file di evidenza. Al primo snapshot con
identita' completa, oppure al deadline, viene deciso uno e un solo stato:

| Stato | Condizione terminale |
| --- | --- |
| `CONNECTED_IDENTITY_AVAILABLE` | Terminale connesso, identita' completa e login uguale a quello atteso. |
| `IDENTITY_MISMATCH` | Terminale connesso, identita' completa e login diverso da quello atteso. |
| `TIMEOUT` | Al campione finale il terminale e' connesso, ma login/server/company/trade mode non formano ancora un'identita' completa. |
| `NOT_CONNECTED` | Al campione finale `TERMINAL_CONNECTED` e' falso. |
| `INPUT_INVALID` | Run ID, expected account o timeout manca/non e' valido/non puo' essere consumato. |
| `OUTPUT_FAILURE` | Preparazione o pubblicazione dell'output non e' disponibile; se anche l'evidenza di errore non puo' essere pubblicata, il run resta privo di risultato e quindi inconcludente. |

La distinzione e' dunque deterministica: `TIMEOUT` implica
`terminal_connected=true`; `NOT_CONNECTED` implica
`terminal_connected=false`. Entrambi vengono decisi soltanto allo scadere del
timeout, non su campioni intermedi.

## Unica pubblicazione atomica

Il risultato e' costruito in memoria una sola volta come stato terminale e reso
visibile tramite:

```text
MQL5\Files\MT5DirectEndpointLab\identity-probe.json.tmp
    ->
MQL5\Files\MT5DirectEndpointLab\identity-probe.json
```

Il probe scrive il temporaneo, esegue il flush, chiude l'handle e lo rinomina
senza flag di sovrascrittura. Se esiste gia' un finale, non lo sostituisce. Una
prima pubblicazione fallita puo' tentare di materializzare `OUTPUT_FAILURE`, ma
la guardia sul finale garantisce che al massimo un solo risultato finale sia
visibile. Se nessun finale viene creato, il run non deve essere interpretato
come successo.

Il contratto chiuso e' definito da `identity-probe.schema.json`:
`schema_version=3`, `probe_version=3.0.0`, sei soli stati terminali e
esattamente i campi autorizzati. Non contiene account/login, password, nome
cliente, saldo, equity, ordini, posizioni, deal o storico. Il consumatore deve
confrontare `run_id` con il valore atteso del run. Compone
`CONNECTED_IDENTITY_AVAILABLE` come identita valida e `IDENTITY_MISMATCH` come
contraddizione valutabile; `TIMEOUT`/`NOT_CONNECTED` non producono identita,
mentre `INPUT_INVALID`/`OUTPUT_FAILURE` e ogni run-ID mismatch vengono rifiutati.

### Binding build, path e artefatto

`terminal_build`, `terminal_path` e `terminal_data_path` sono osservazioni
prodotte direttamente dal terminale. I due path raw sono ammessi soltanto
nell'artefatto locale `identity-probe.json`: non devono essere copiati in
evidence normalizzata, report o ZIP di revisione.

Il consumer fidato deve:

1. leggere i byte del file finale soltanto dopo la rinomina atomica;
2. validare JSON, schema/probe v3 e `run_id`;
3. confrontare `terminal_build` con il build legato al manifest della run;
4. validare e rendere canonici i due path Windows;
5. derivare ciascun path digest con:

   ```text
   contract_digest(
     "WINDOWS_PATH",
     1,
     {"canonical_path": <path_windows_canonico>}
   )
   ```

   dove `contract_digest` usa il prefisso binario
   `MT5_DIRECT_ENDPOINT\0`, seguito dal tipo, da `\0`, dalla versione
   decimale, da `\0` e dal JSON canonico UTF-8;
6. calcolare separatamente `identity_probe_output_sha256` secondo il contratto
   autorevole del consumer sull'artefatto v3 validato.

Il digest dell'output non viene inserito nel file stesso: un file non puo'
auto-attestare senza circolarita' il digest dei propri byte finali. La presenza
del `run_id` nel payload rende l'artefatto specifico della run; il verifier deve
inoltre rifiutare il riuso dello stesso digest fra C2, C3 e C5.

`account_trade_allowed=false` da solo non dimostra l'uso di una password
investor: la provenienza della credenziale deve essere attestata separatamente
dal runbook del laboratorio.

Dopo la decisione il timer viene fermato, le variabili di account/run/tempo
vengono azzerate, gli input e il temporaneo vengono rimossi e l'EA chiama
`ExpertRemove()`. `OnDeinit` ripete la pulizia in modo idempotente.

## Verifica consentita in questa fase

Eseguire esclusivamente le guardie statiche, senza avviare MetaTrader:

```text
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -v
```

Stato compilazione MQL5 di questa fase: `NOT_RUN_PLATFORM_REQUIRED`.

MetaEditor non e' disponibile nell'ambiente corrente. Su una macchina Windows
in cui MetaTrader 5 sia gia' installato, la sola compilazione autorizzata si
esegue da PowerShell con il comando esatto seguente, adattando soltanto la root
`C:\TJLab` se il clone si trova altrove:

```powershell
& 'C:\Program Files\MetaTrader 5\metaeditor64.exe' '/compile:C:\TJLab\lab\mt5_direct_endpoint\mql5\TradeJournalIdentityProbe.mq5' '/log:C:\TJLab\evidence\TradeJournalIdentityProbe.compile.log'
```

Il log deve dichiarare `0 errors`; la sola presenza del file `.ex5` non basta.
La compilazione non richiede e non autorizza l'avvio di `terminal64.exe`.

Il file e' un Expert Advisor (`OnInit`/`OnTimer`): nella futura configurazione
di startup andra' referenziato con
`Expert=TradeJournal\TradeJournalIdentityProbe`, non con `Script=`.
