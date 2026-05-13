FROM python:3.11-slim

# System deps + node 20 (needed for bgutil's TypeScript build)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# TypeScript globally so `npx tsc` always works for bgutil
RUN npm install -g --no-audit --no-fund typescript@5

WORKDIR /app

# Python deps (stable layer — pip cache busts only on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install -U yt-dlp pytubefix bgutil-ytdlp-pot-provider

# bgutil PO token server — lets yt-dlp bypass YouTube's bot detection
# without cookies (needed for popular/famous videos).
# This step VERIFIES the build artifact exists; build fails loudly if missing.
RUN git clone --depth=1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil-ytdlp-pot-provider && \
    cd /app/bgutil-ytdlp-pot-provider/server && \
    npm install --no-audit --no-fund && \
    (npm run build 2>/dev/null || tsc) && \
    test -f build/main.js && echo "✅ bgutil build OK" || (echo "❌ bgutil build FAILED" && exit 1)

# Verify the yt-dlp plugin is installed (it lives at yt_dlp_plugins/extractor/getpot_bgutil*)
RUN python -c "from yt_dlp_plugins.extractor import getpot_bgutil_http; print('✅ bgutil yt-dlp plugin OK')"

COPY . .

EXPOSE 8080

CMD ["gunicorn", "wsgi:app", "-c", "gunicorn.conf.py"]
