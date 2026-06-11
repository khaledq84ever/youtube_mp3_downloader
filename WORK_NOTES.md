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

## 2026-06-11 — Fix: "All sources are busy" on fresh (uncached) videos

**Symptom:** popular/cached videos converted fine; anything not already in
iotacloud's cache failed with "Conversion failed. All sources are busy."

**Root cause:** three stacked timeouts all assumed instant conversions:
GLOBAL_JOB_TTL=30s for the whole job, 25s per backend, and only 6 iotacloud
polls (~10s). Fresh videos convert server-side in 30-120s → every backend
"failed" within 30s.

**Fix (server/app.py):** GLOBAL_JOB_TTL 30→180; iotacloud polls 6→35 (~100s);
per-backend timeout now 150s for y2mate/y2mate_web (bot-blocked fallbacks stay
at 25s). Frontend already polls indefinitely, no change needed.
