# Work Notes — YouTube Downloader backend

## 2026-06-03 — Fix: "Video unavailable" blocked all downloads

**Symptom:** YTGet extension showed "Video unavailable" on videos that are clearly public.

**Root cause:** `/info` and `do_convert` use *disjoint* extractors. `/info` raced
Piped/oEmbed/Invidious/pytube/yt-dlp — all bot-blocked on Railway's datacenter IP
(health: 0 working Piped/Invidious, ~1/50 proxies) — and returned `400 "Video
unavailable"`. But the real download (`do_convert`) pulls through **y2mate/iotacloud**,
which works on that IP. The extension calls `/info` first and aborted, so users never
reached the working download.

**Fix (`server/app.py`):**
1. Added **iotacloud** as a 5th `/info` source — a single `GET iotacloud.org/api/?r=1&v=<id>`
   returns the real title fast (the proven Railway-working extractor).
2. A `__BOT__` block now degrades to a 200 stub (CDN thumbnail) instead of `400`, so a
   metadata miss never gates the working `/start` pipeline.

**Verified:** live `/info` → 200 "Me at the zoo"; full E2E → **497 KB MP3**. Boots clean (gthread).
**Shipped:** Railway `youtube-mp3-downloader` SUCCESS · GitHub `main` commit `31cb9a4`.

**Still open:** 320K request yields 128 kbps (y2mate/iotacloud is fixed-bitrate). Download works; quality not honored.
