FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SIGNALFORGE_DB_PATH=/var/data/signalforce.db \
    SIGNALFORGE_COOKIE_SECURE=true

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --create-home --uid 10001 signalforce \
    && mkdir -p /var/data \
    && chown -R signalforce:signalforce /app /var/data

COPY --chown=signalforce:signalforce . .

USER signalforce
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["sh", "-c", "uvicorn app.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000}"]
