FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server .

RUN pip install --no-cache-dir -r requirements.txt && \
    pip install -U yt-dlp

EXPOSE $PORT

CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-13000} \
    --workers 1 \
    --worker-class gthread \
    --threads 16 \
    --timeout 360 \
    --graceful-timeout 30
