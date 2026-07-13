# syntax=docker/dockerfile:1
#
# Immagine del fake bridge (bridge/fake/fake_bridge.py): SOLO per test/validazione locale su
# qualunque architettura, incluso Ubuntu ARM64. Nessuna dipendenza da Wine/MT5, solo Python
# standard library (vedi bridge/common.py). NON e' il bridge reale: quello e'
# bridge/windows/mt5_bridge.py, predisposto per un Windows Python sotto Wine su AMD64, non
# ancora impacchettato in un'immagine Docker (vedi README).
#
# Contesto di build atteso: la ROOT del repository (non bridge/fake/), perche' serve sia
# bridge/common.py sia bridge/fake/*.py. Vedi docker-compose.research-mt5-fake.yml.

FROM python:3.11-slim AS mt5-bridge-fake

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 bridge \
    && useradd --uid 1000 --gid bridge --create-home --shell /usr/sbin/nologin bridge

WORKDIR /app

COPY bridge/common.py ./bridge/common.py
COPY bridge/fake/fake_bridge.py bridge/fake/healthcheck.py ./bridge/fake/
RUN chown -R bridge:bridge /app

USER bridge

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "bridge/fake/healthcheck.py"]

ENTRYPOINT ["python", "bridge/fake/fake_bridge.py"]
