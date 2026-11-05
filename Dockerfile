# Container image for Google Cloud Run.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/tmp/store.db

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home store
USER store

# Cloud Run sends traffic to $PORT (8080 by default) and terminates HTTPS in
# front of the container; --proxy-headers makes the store see https URLs.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'
