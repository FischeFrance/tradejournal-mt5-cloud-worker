# Scanner pre-stato C0-C5

`Get-LabPreState.ps1` raccoglie soltanto indicatori locali e sanitizzati prima
di qualunque avvio di MetaTrader. Non esegue il vero esperimento e non rende una
macchina corrente adatta al laboratorio.

## Modalita sicure

Il default e `PlanOnly`: non legge filesystem, registry, processi, proxy o
Credential Manager. Produce su standard output un piano JSON con tutti i check
in stato `UNKNOWN`.

```powershell
.\Get-LabPreState.ps1
```

Su una futura VM Windows disposable, come utente dedicato, le sole letture si
attivano esplicitamente:

```powershell
.\Get-LabPreState.ps1 `
  -Mode ReadOnlyChecks `
  -PortableRoot 'C:\TJLab\<experiment-id>\C3\terminal' `
  -PrivateDirectory 'C:\TJLab\<experiment-id>\runs\<run-id>\C3\private'
```

Lo script non crea directory e non sovrascrive file. Per conservare il report
si deve indicare esplicitamente un nuovo file su volume locale, in una directory
gia esistente e senza reparse point:

```powershell
.\Get-LabPreState.ps1 `
  -Mode ReadOnlyChecks `
  -PortableRoot 'C:\TJLab\<experiment-id>\C3\terminal' `
  -PrivateDirectory 'C:\TJLab\<experiment-id>\runs\<run-id>\C3\private' `
  -OutputPath 'C:\TJLab\<experiment-id>\runs\<run-id>\C3\raw\pre-state.json'
```

Non usare `-OutputPath` in una cartella sincronizzata, share, mount remoto o
volume non cifrato. Il JSON contiene hash di path pseudonimi e quindi rimane
correlabile tra esecuzioni anche se non espone il path originale.

## Cosa misura

Ogni check restituisce `PASS`, `FAIL` oppure `UNKNOWN`:

- inventario completo della portable root con limite esplicito;
- presenza di `Config\accounts.dat`, `Config\servers.dat`, `Bases`, `Profiles`
  e `MQL5\Profiles`;
- presenza delle root MetaQuotes in Roaming/Local AppData e in HKCU;
- processi `terminal`, `terminal64`, `metaeditor` e `metaeditor64`;
- reparse point negli antenati e nella portable root, senza seguirli;
- drive fixed locale e assenza di una mappatura UNC visibile;
- proxy WinHTTP e WinINet leggendo soltanto configurazione locale;
- scope enumerabile di Credential Manager tramite `cmdkey /list`, senza
  esportare target, utenti o contenuti;
- marker Community riconoscibili senza esportarne i nomi;
- directory privata vuota/assente e assenza dell'input effimero
  `MQL5\Files\MT5DirectEndpointLab\expected-account.txt`.

I file sensibili noti non vengono aperti ne hashati. Il digest dell'inventario
deriva soltanto da hash dei path relativi, tipo e dimensione. Nel report non
entrano path, nomi registry, nomi credenziali, valori proxy, PID o command line.

Un inventario troncato, un errore ACL, un formato proxy non riconosciuto o una
localizzazione `cmdkey` non supportata produce `UNKNOWN`, mai un falso `PASS`.

## Campi che restano attestazioni dell'operatore

Lo scanner include `evidence_projection`, ma lascia deliberatamente `null` i
campi che una lettura locale puntuale non puo provare:

| Campo evidence | Perche non e automatizzato |
|---|---|
| `portable_root_new` | Richiede provenienza del clone/manifest immutabile, non solo assenza di file noti. |
| `disposable_clone_new` | Richiede attestazione snapshot/hypervisor. |
| `windows_user_new` | L'eta dell'account non prova che il profilo non sia stato riusato. |
| `credential_manager_empty` | `cmdkey` non copre in modo portabile ogni Vault/Web Credential. Un `FAIL` e probante; un `PASS` resta scoped. |
| `community_identity_absent` | I marker MetaQuotes noti non coprono ogni storage/versione. Un marker presente falsifica l'assenza. |
| `no_shared_storage` | Drive fixed e zero reparse non escludono VHD condivisi, pass-through o backing del clone. |
| `terminal_data_path_matches` | `TERMINAL_DATA_PATH` e osservabile soltanto dopo l'avvio futuro del probe. |

Anche `sensitive_bootstrap_absent` copre soltanto i due path dichiarati; la
procedura operatore deve attestare che non esistano copie in clipboard,
transcript, history, pagefile, crash dump o cartelle esterne al run.

## Limiti e localizzazione

- Lo scanner deve girare in PowerShell 64 bit 5.1+ sullo stesso utente Windows
  destinato al controllo; non carica hive di altri utenti.
- Il parser `cmdkey` riconosce soltanto etichette note inglesi, italiane,
  francesi, tedesche, spagnole, olandesi e portoghesi. Ogni output diverso e
  `UNKNOWN`. Non si deve aggiungere una lingua senza fixture verificata.
- `cmdkey` enumera il proprio scope, non tutti i Vault moderni. Per elevare il
  relativo campo a vero serve un helper nativo revisionato che usi
  `CredEnumerate` e le Vault API senza serializzare target o contenuti, oppure
  la provenienza attestata del nuovo utente disposable.
- Il blob `WinHttpSettings` usa i flag proxy al byte offset 8. Formati corti,
  flag sconosciuti o viste registry incoerenti restano `UNKNOWN`.
- L'ispezione e point-in-time: un processo o file creato subito dopo lo scan
  deve essere escluso dalla procedura tramite snapshot e sequenza controllata.
- Un drive `Fixed` non dimostra storage fisicamente indipendente.
- L'assenza di reparse point non prova l'assenza di hardlink. Il manifest
  immutabile/golden image resta necessario.

## Test senza rete e senza mutazioni

Il dry-run PowerShell esercita soltanto `PlanOnly`:

```powershell
.\tests\Invoke-PreStateScannerDryRunTest.ps1
```

Il test statico portabile si esegue anche da macOS/Linux:

```text
python3 -m unittest lab/mt5_direct_endpoint/windows/tests/test_pre_state_scanner_static.py -v
```

Nessuno dei due test avvia MetaTrader, interroga rete, scrive file o modifica il
sistema.

