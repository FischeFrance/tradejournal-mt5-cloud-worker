# syntax=docker/dockerfile:1
#
# Contesto di build atteso: la ROOT del repository (non docker/), perche' questo Dockerfile
# copia sia requirements.txt che worker/ dalla root. Vedi docker-compose.mock.yml / docker-compose.yml.
#
# Tre stage finali:
#   - mock (default): Fase 1 del POC (trade-sync worker, worker/main.py). Solo Python,
#     MOCK_MODE=true, nessuna dipendenza da Wine/MT5. E' lo stage usato da
#     docker-compose.mock.yml e testabile ovunque, incluso macOS/Ubuntu ARM64.
#   - real-mt5: Fase 2, PREDISPOSTA ma non pronta all'uso. Aggiunge Wine + Xvfb per eseguire in
#     futuro il terminale MetaTrader 5 reale su Linux. Vedi il commento sopra quello stage e il
#     README per i limiti e i passi manuali richiesti.
#   - research-market-data: modalita' research (worker/market_data_main.py, processo separato dal
#     trade-sync worker). Nessuna dipendenza da Wine/MT5: raccoglie dati di mercato mock e li
#     salva su Postgres. Mai avviata da un'installazione cliente: solo da
#     docker-compose.research.yml, esplicitamente. Vedi il commento sopra quello stage.

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
# Limiti noti/deliberati (vedi README, "Limiti noti Wine/MT5" per il dettaglio completo):
#   - NON scarica alcun installer MetaTrader 5: sarebbe una distribuzione non autorizzata di
#     software proprietario. L'installer (o una directory terminale gia' installata) va fornito
#     manualmente dall'utente (vedi README, sezione "Preparazione runtime MT5") e montato nel
#     volume /opt/mt5 (tipicamente ./runtime/mt5:/opt/mt5 da docker-compose.yml).
#   - docker/entrypoint-real.sh avvia Xvfb e verifica la presenza del terminale, ma NON installa
#     ne' avvia automaticamente Wine/il terminale MT5: quello resta un passo manuale, documentato
#     nel README, dato che non e' stato possibile validarlo end-to-end su questo ambiente (macOS
#     Apple Silicon, nessun MT5/Wine reale disponibile -- solo build/config sono stati validati).
#   - Il pacchetto Python `MetaTrader5` e' un'estensione nativa Windows: funziona solo se il
#     processo Python che lo importa gira anch'esso dentro Wine (o dietro un bridge dedicato),
#     non come processo Python Linux nativo verso un terminale Wine esterno. Questo Dockerfile
#     predispone Wine/Xvfb/la directory persistente, ma la scelta tra "Python dentro Wine" e "un
#     bridge IPC/HTTP" resta un lavoro architetturale aperto per il test reale su VPS Ubuntu.
########################################
FROM base AS real-mt5

USER root

# Repository WineHQ ufficiale (piu' aggiornato del pacchetto "wine" di Debian). Richiede
# accesso di rete in fase di build.
#   - xvfb + x11-utils (xdpyinfo): display virtuale headless per Wine/il terminale MT5.
#   - procps (pgrep): usato da docker/healthcheck-mt5.sh per verificare che Xvfb sia vivo.
#   - cabextract/unzip: tipicamente richiesti dagli script di installazione di app Windows sotto
#     Wine (es. componenti .cab dentro l'installer MT5), tenuti pronti anche se non usati qui.
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        wget \
        gnupg2 \
        ca-certificates \
        xvfb \
        x11-utils \
        procps \
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
    MT5_TERMINAL_DIR=/opt/mt5 \
    MOCK_MODE=false \
    MT5_CLIENT_SOURCE=direct

COPY docker/entrypoint-real.sh docker/healthcheck-mt5.sh ./docker/
RUN chmod +x ./docker/entrypoint-real.sh ./docker/healthcheck-mt5.sh

# /opt/mt5: directory persistente per il terminale MT5 (installer fornito manualmente o
# directory terminale gia' installata, montata dall'host -- vedi docker-compose.yml e README,
# sezione "Preparazione runtime MT5"). Mai popolata durante la build: nessun installer incluso
# nell'immagine.
RUN mkdir -p /opt/mt5 /home/worker/.wine && chown -R worker:worker /opt/mt5 /home/worker /app
VOLUME ["/opt/mt5", "/home/worker/.wine"]

USER worker

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/app/docker/healthcheck-mt5.sh"]

ENTRYPOINT ["/app/docker/entrypoint-real.sh"]

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

########################################
# Stage: research-market-data -- market-data-worker, modalita' research (Postgres locale).
#
# Processo separato dal trade-sync worker (stage mock/real-mt5 sopra): esegue
# worker/market_data_main.py, mai worker/main.py. Non collegata a nessun docker-compose
# "cliente": va avviata esplicitamente aggiungendo docker-compose.research.yml (vedi README).
# Nessuna dipendenza da Wine/MT5/ARM64: usa solo MARKET_DATA_SOURCE=mock in questa fase, quindi
# gira nativamente ovunque, incluso Ubuntu ARM64.
########################################
FROM base AS research-market-data

USER root

COPY requirements-research.txt ./
RUN pip install --no-cache-dir -r requirements-research.txt

# db/migrations: solo qui, non nello stage base/mock/real-mt5 -- le immagini cliente non hanno
# alcun motivo di contenere DDL Postgres che non eseguiranno mai (vedi worker/db_migrate.py).
COPY db/ ./db/
COPY docker/entrypoint-market-data.sh docker/healthcheck-market-data.sh ./docker/
RUN chmod +x ./docker/entrypoint-market-data.sh ./docker/healthcheck-market-data.sh \
    && chown -R worker:worker /app/db

ENV APP_MODE=research \
    ENABLE_MARKET_DATA=true

USER worker

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/app/docker/healthcheck-market-data.sh"]

ENTRYPOINT ["/app/docker/entrypoint-market-data.sh"]
