# Poller de Gmail (src.poller). Corre el tramo 1 del grafo.
# Build desde la raiz:  docker build -f src/poller/Dockerfile -t care-poller .
#
# Una sola instancia: dos pollers duplican llamadas al modelo.
# Necesita credentials.json y token.json montados en /app/gmail (ver docker-compose.yml).
# El token se genera una vez fuera del contenedor:  .\care.cmd gmail

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CARE_DB=/app/data/care.db \
    CARE_SISTEMAS_DB=/app/data/sistemas.db \
    GMAIL_CREDENTIALS=/app/gmail/credentials.json \
    GMAIL_TOKEN=/app/gmail/token.json \
    POLL_INTERVALO=60

WORKDIR /app

RUN useradd --uid 10001 --create-home care \
 && mkdir -p /app/data /app/gmail && chown care:care /app/data /app/gmail

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=care:care src/ src/

USER care
VOLUME ["/app/data"]

# Forma shell para expandir POLL_INTERVALO; exec deja a python como PID 1 y
# recibe el SIGTERM de `docker stop`.
CMD ["sh", "-c", "exec python -m src.poller --loop ${POLL_INTERVALO}"]
