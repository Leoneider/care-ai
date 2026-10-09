# Bandeja del ejecutivo (src.web, :8000).
# Build desde la raiz:  docker build -f src/web/Dockerfile -t care-bandeja .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CARE_DB=/app/data/care.db \
    CARE_SISTEMAS_DB=/app/data/sistemas.db \
    CARE_FEEDBACK_JSONL=/app/data/feedback.jsonl

WORKDIR /app

# Mismo UID en las tres imagenes: comparten el volumen de data/.
RUN useradd --uid 10001 --create-home care \
 && mkdir -p /app/data && chown care:care /app/data

# Dependencias primero: la capa se reutiliza mientras requirements.txt no cambie.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=care:care src/ src/

USER care
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

# 0.0.0.0 dentro del contenedor; quien decide la exposicion es el mapeo de puertos
# (en docker-compose.yml solo se publica en 127.0.0.1: la UI no tiene login).
CMD ["python", "-m", "src.web", "--host", "0.0.0.0", "--port", "8000"]
