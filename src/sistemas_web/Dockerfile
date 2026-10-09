# Consola del mini sistema legado (src.sistemas_web, :8002).
# Build desde la raiz:  docker build -f src/sistemas_web/Dockerfile -t care-sistemas .
#
# Es el sistema simulado de la demo: en produccion se reemplaza por los sistemas
# reales y esta imagen no se despliega.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CARE_DB=/app/data/care.db \
    CARE_SISTEMAS_DB=/app/data/sistemas.db

WORKDIR /app

RUN useradd --uid 10001 --create-home care \
 && mkdir -p /app/data && chown care:care /app/data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=care:care src/ src/

USER care
VOLUME ["/app/data"]
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/', timeout=4)"

CMD ["python", "-m", "src.sistemas_web", "--host", "0.0.0.0", "--port", "8002"]
