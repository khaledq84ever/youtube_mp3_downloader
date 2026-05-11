FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && pip install -U yt-dlp

COPY server/ ./

EXPOSE 8080

CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --worker-class gthread \
    --threads 16 \
    --timeout 360 \
    --graceful-timeout 30 \
    --log-level info
