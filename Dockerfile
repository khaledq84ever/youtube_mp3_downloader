FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install -U yt-dlp

COPY . .

EXPOSE 8080

CMD ["gunicorn", "app:app", "-c", "gunicorn.conf.py"]
