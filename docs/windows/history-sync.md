# History sync

Le modalità sono `new_only`, `from_date`, `all_available`. Ordini e deal vengono letti per
finestre UTC fino a 31 giorni con checkpoint dopo ogni finestra, resume e sink indipendente dal
live sync. Deal multipli, aperture/chiusure parziali, commissioni e swap restano record distinti
per consentire la ricostruzione. Il progresso contiene solo conteggi e timestamp UTC.

