# syntax=docker/dockerfile:1
#
# Contesto di build atteso: la ROOT del repository (non docker/), perche' questo Dockerfile
# copia sia requirements.txt che worker/ dalla root. Vedi docker-compose.mock.yml / docker-compose.yml.
#
# Due stage finali:
#   - mock (default): Fase 1 del POC. Solo Python, MOCK_MODE=true, nessuna dipendenza da
#     Wine/MT5. E' lo stage usato da docker-compose.mock.yml e testabile ovunque, incluso macOS.
#   - real-mt5: Fase 2, PREDISPOSTA ma non pronta all'uso. Aggiunge Wine + Xvfb per eseguire in
#     futuro il terminale MetaTrader 5 reale su Linux. Vedi il commento sopra quello stage e il
#     README per i limiti e i passi manuali richiesti.

########################################
# Stage: base
########################################
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Utente non-root: il worker non ha mai bisogno di privilegi di root per girare.
RUN groupadd --gid 1000 worker \
    && useradd --uid 1000 --gid worker --create-home --shell /usr/sbin/nologin worker

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ ./worker/
COPY docker/entrypoint.sh docker/healthcheck.sh ./docker/
RUN chmod +x ./docker/entrypoint.sh ./docker/healthcheck.sh \
    && mkdir -p /app/data \
    && chown -R worker:worker /app

########################################
# Stage: real-mt5 -- Fase 2, PREDISPOSTA per il test reale su Ubuntu, NON pronta all'uso.
#
# Non viene costruita ne' avviata di default da nessun docker-compose in questo repository.
# Per costruirla esplicitamente (senza avviarla):
#   docker build -f docker/Dockerfile --target real-mt5 -t tradejournal-mt5-worker:real-mt5 .
#
# Limiti noti/deliberati:
#   - NON scarica alcun installer MetaTrader 5: sarebbe una distribuzione non autorizzata di
#     software proprietario. L'installer va fornito manualmente dall'utente (vedi README,
#     sezione "Fornire l'installer MT5 manualmente") e montato nel volume /mt5/installer.
#   - L'inizializzazione di Wine (creazione WINEPREFIX, avvio di Xvfb, esecuzione
#     dell'installer MT5, avvio del terminale) NON e' automatizzata qui: richiede passi manuali
#     documentati nel README, dato che non e' stato possibile validarli su questo ambiente
#     (macOS, nessun MT5/Wine disponibile).
#   - Il pacchetto Python MetaTrader5 (usato da worker/mt5_client.py:RealMt5Client) funziona
#     solo quando il terminale MT5 e' effettivamente in esecuzione e raggiungibile.
########################################
FROM base AS real-mt5

USER root

# Repository WineHQ ufficiale (piu' aggiornato del pacchetto "wine" di Debian). Richiede
# accesso di rete in fase di build.
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        wget \
        gnupg2 \
        ca-certificates \
        xvfb \
        cabextract \
        unzip \
    && mkdir -p /etc/apt/keyrings \
    && wget -nv -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -nv -O /etc/apt/sources.list.d/winehq.sources https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends winehq-stable \
    && rm -rf /var/lib/apt/lists/*

ENV WINEPREFIX=/home/worker/.wine \
    WINEARCH=win64 \
    DISPLAY=:99 \
    MOCK_MODE=false

# Punto di mount per l'installer MT5 fornito manualmente (mai incluso nell'immagine, vedi README).
VOLUME ["/mt5/installer"]

RUN mkdir -p /home/worker/.wine && chown -R worker:worker /home/worker /app

USER worker

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/app/docker/healthcheck.sh"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]

########################################
# Stage: mock (default) -- Fase 1 del POC.
########################################
FROM base AS mock

ENV MOCK_MODE=true \
    DRY_RUN=true

USER worker

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/docker/healthcheck.sh"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
