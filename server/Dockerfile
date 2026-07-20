# Build context is the REPO ROOT (see docker-compose.yml) so shared/ is available.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Deps first for layer caching.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

# App code + the vendored catalog (single source of truth, read as data).
COPY server/ /app/server/
COPY shared/ /app/shared/

WORKDIR /app/server

# Single worker on purpose: the live "latest" map + WebSocket hub are in-process
# and must not be split across workers.
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
