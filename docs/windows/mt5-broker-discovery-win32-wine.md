# Discovery broker MT5 via Win32 in Wine

## Scopo

`windows_agent/worker/wine_mt5_broker_discovery.py` è l'adapter che evita SDK e
API commerciali per il terminale MT5 eseguito in Wine. Deve essere avviato da un Python
Windows a 64 bit nello stesso utente, Wine prefix e desktop del terminale. Non usa
`pywinauto`, UI Automation, coordinate, screenshot, OCR o un protocollo di rete
MetaQuotes implementato dal progetto.

L'adapter implementa il contratto `BrokerDiscoveryUiAdapter` già usato da
`WindowsMt5BrokerDiscovery`; non riceve login, password o token. L'API diretta
accetta soltanto percorso esatto di `terminal64.exe`, PID Win32 candidati, server
atteso, query e timeout. L'entry point destinato a Linux non accetta PID dal
chiamante: enumera le finestre dentro Wine, verifica il percorso dell'eseguibile e
richiede un solo processo compatibile.

## Compatibilità osservata

Il worker riconosce esclusivamente la gerarchia Win32 verificata nel laboratorio:

| Controllo | Classe | ID |
| --- | --- | ---: |
| Query | `Edit` | 10814 |
| Find | `Button` | 10815 |
| Risultati owner-data | `SysListView32` | 10729 |
| Back | `Button` | 12323 |
| Next | `Button` | 12324 |
| Cancel | `Button` | 2 |
| Server canonico | `ComboBox` | 10139 |

Il wizard di prima apertura deve essere già visibile. Se il percorso del processo,
la finestra, una classe, un ID o la cardinalità dei controlli non coincide,
l'adapter fallisce in modo chiuso con un errore sanificato.

Il modulo è anche un entry point per il Python Windows a 64 bit del Wine prefix:

```powershell
python.exe -m windows_agent.worker.wine_mt5_broker_discovery `
  --request broker-discovery-request.json `
  --result broker-discovery-result.json
```

Il JSON usa lo stesso schema stretto del worker Windows. Campi aggiuntivi, incluse
eventuali credenziali, vengono rifiutati prima di aprire il backend Win32.

## Launcher Linux preparato

`windows_agent/worker/linux_wine_broker_discovery.py` definisce il confine del
launcher Ubuntu. Il repository del helper e la directory di scambio devono essere
confinati sotto il vero `drive_c` di un `WINEPREFIX` privato; symlink intermedi e
la mappatura Wine `Z:` vengono rifiutati. Il processo figlio riceve un ambiente
costruito da zero, file JSON con permessi privati e nessuna credenziale. Timeout,
eccezioni e interruzioni Python attivano la terminazione del process group e il
cleanup. Il futuro entrypoint deve inoltre intercettare `SIGTERM`, fermare prima il
helper e soltanto dopo terminale, Wineserver e desktop virtuale.
Un lock `flock` non bloccante sul `WINEPREFIX` impedisce inoltre che retry o job
concorrenti guidino contemporaneamente la stessa finestra MT5.

Il launcher non è ancora collegato all'entrypoint Docker esistente. Quell'entrypoint
legge oggi il bootstrap segreto troppo presto e la golden image non include Python
Windows né il helper. La futura sequenza deve essere: desktop Wine, terminale senza
credenziali, discovery esatta, bootstrap investor soltanto dopo il match, bridge.
Fino allo smoke end-to-end questa è un'integrazione preparata, non un percorso di
produzione. Un timeout, un errore UI o un cleanup non confermato rende l'istanza
usa-e-getta: l'orchestratore deve distruggere terminale, Wineserver e desktop e
ricrearli prima di introdurre qualsiasi credenziale.

Il confinamento dei path in `C:` e l'assenza di `Z:` sono controlli applicativi,
non una sandbox Wine: un processo eseguito con lo stesso UID può comunque raggiungere
risorse Unix. In produzione l'intero desktop Wine deve quindi vivere in un container
o mount namespace dedicato, con UID esclusivo, filesystem minimo e nessun secret
montato durante la discovery. Il namespace di rete deve inoltre bloccare loopback,
link-local e reti private; soltanto dopo l'uscita del helper può essere introdotto il
bootstrap investor destinato alla stessa istanza MT5.

## Sequenza di selezione

Per ogni query il worker usa `WM_SETTEXT`, `BM_CLICK` e richiede il ciclo
osservabile di Find da disabilitato a nuovamente abilitato. Poiché la lista è
owner-data, non considera affidabile il testo delle righe. Itera invece gli
indici restituiti da `LVM_GETITEMCOUNT`, seleziona ogni
riga con `LVM_SETITEMSTATE`, apre la pagina successiva e legge il server dalla
ComboBox con `WM_GETTEXT`, poi torna indietro.

`LVM_SETITEMSTATE` contiene un puntatore e non viene automaticamente serializzato
tra processi. La struttura `LVITEMW` viene quindi allocata temporaneamente nel
processo del terminale con `VirtualAllocEx` e copiata con `WriteProcessMemory`;
non viene iniettato o eseguito codice. La memoria è liberata dopo ogni messaggio
completato. Se il messaggio scade, l'allocazione resta valida fino al teardown del
terminale: liberarla mentre il thread UI è sospeso potrebbe causare un uso dopo il
rilascio quando MT5 riprende.

La selezione riesce soltanto se il server atteso ha un unico match esatto dopo
normalizzazione Unicode e spazi. Prima di restituire successo, il worker preme
nuovamente Next e verifica ancora la ComboBox. Infine chiude il wizard tramite il
controllo Cancel e verifica che la finestra sia realmente scomparsa.

## Gate prima dell'uso

Gli ID dei controlli sono un confine di compatibilità legato alla build MT5/Wine.
Una nuova immagine deve superare uno smoke test senza credenziali in un container
usa-e-getta prima di abilitare questo adapter. Il test deve confermare anche che
la stessa istanza del terminale consumi il bootstrap successivo. Prima del rollout
servono inoltre isolamento egress contro reti interne e verifica dei termini
MetaQuotes/broker per l'impiego commerciale multi-cliente. Nessun probe deve essere
eseguito sul container cliente in produzione.
