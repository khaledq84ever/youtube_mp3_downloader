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

## 2026-07-12 — Fix: watchdog was killing a healthy bgutil server → "All sources are busy"

**Symptom:** all real conversions failed ("All sources are busy"); a plain
redeploy 2h earlier (backend reorder + cache-bust b) did not help.

**Root cause (from railway logs + /proxy-status + /health):** the bgutil
watchdog killed a *live, working* PO-token server. `/tmp/bgutil.log` showed it
mid-"Generating POT" when the watchdog logged `DOWN (exit=None)` — exit=None
means the process was alive, but minting a POT pegs node's single-threaded
event loop, so `/ping` missed its 2s timeout. Watchdog killed it, replacement
took >10s to boot ("not responding within 10 s"), `_bgutil_ready` stuck False
→ yt-dlp built commands with non-PO clients → instant bot-block (ytdlp
"failed" in 4s). Meanwhile y2mate iotacloud DNS is dead (Errno -5), y2mate.nu
returns `{}`, webshare proxies 0/50 alive (402 expired) — so no fallback.

**Fix (server/app.py):** ping timeout 2s→8s; watchdog only restarts a live
process after 3 consecutive misses (~90s); startup wait 10s→30s; yt-dlp
stderr tail now logged to /proxy-status on every failed attempt (outage was
invisible — log only said "ytdlp returned False"); backends reordered ytdlp
first (dead y2mate paths wasted ~10s/job). Dockerfile CACHE_DATE→2026-07-12c
for fresh yt-dlp@master.

## 2026-07-12 (late) — YouTube blocks datacenter IPs even WITH PO tokens → etacloud backend revived

**Symptom:** every conversion "All sources are busy" from 21:15 UTC (convfix:
OK at 21:00, broken at 21:30; no deploy in between). Redeploy didn't help.

**Diagnosis:** reproduced OUTSIDE Railway on the VPS with fresh yt-dlp@master
+ locally-built bgutil POT server: bgutil minted a valid token, yt-dlp used it
("Retrieved a player PO Token for web client") and YouTube STILL returned
"Sign in to confirm you're not a bot" — for every client (web, mweb, tv,
tv_embedded, web_safari, web_embedded, visionos, android), with and without
POT. No upstream issue spike → not global; YouTube flagged datacenter IP
ranges (both Hostinger VPS and Railway US-West egress). No yt-dlp bump fixes
this; cookies or residential proxies would, neither available.

**Working path found:** eta.etacloud.org (y2mate.gs engine) direct flow works
from datacenter IPs again — IF every request sends UA + Referer:
https://y2mate.gs/ + Origin: https://y2mate.gs (missing Origin → empty 200 on
/init; that's why the old "direct" path failed and KDL's relay fallback is now
404). Verified end-to-end with curl: auth→init→convert(redirect)→progress≥3→
download = real 459KB MP3.

**Fix (server/app.py):** _y2mate_resolve rewritten iotacloud→etacloud
(auth/init/convert/progress, Origin header everywhere, follows redirectURL,
mp3+mp4); y2mate backend moved FIRST (converts on etacloud's servers, our IP
never touches YouTube), per-backend leash 240s for it; ytdlp kept as fallback.
bgutil watchdog: never restart a LIVE process (even 3-miss kill murdered
healthy servers mid-mint). Full yt-dlp stderr now printed to stdout for
railway logs.
