# Ciclo di vita istanza

`create-instance.ps1` valida un UUID canonico e crea `terminal`, `worker`, `secrets`, `state`,
`logs`, `data`. Il terminale è copiato per account e si può avviare `/portable`; PID e percorso
eseguibile sono verificati prima dello stop. Provision/deprovision fake sono idempotenti.
Deprovision elimina logicamente i blob DPAPI e i runtime, conservando un marker di stato.
Recovery legge state/checkpoint atomici; non condividere mai directory tra connection ID.

