#!/usr/bin/env bash
cd /home/dev/youtube-mp3-downloader
export PATH="$HOME/.local/node/node-v22.23.2-linux-x64/bin:$HOME/.local/bin:$PATH"
export PORT=4320
export YTDLP_PATH=/home/dev/youtube-mp3-downloader/.venv/bin/yt-dlp
exec .venv/bin/python -m gunicorn wsgi:app -c gunicorn.conf.py
