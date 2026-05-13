FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install -U yt-dlp pytubefix

# bgutil PO token server — lets yt-dlp bypass YouTube's bot detection
# without cookies (needed for popular/famous videos)
RUN git clone --depth=1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil-ytdlp-pot-provider && \
    cd /app/bgutil-ytdlp-pot-provider/server && \
    npm install --production --no-audit --no-fund && \
    (npx tsc 2>/dev/null || true)

COPY . .

EXPOSE 8080

CMD ["gunicorn", "wsgi:app", "-c", "gunicorn.conf.py"]
