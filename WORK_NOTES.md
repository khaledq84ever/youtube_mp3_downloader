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

## 2026-07-31 — App is DOWN: Railway trial expired (not a code bug) + bgutil wedge fixed

**Symptom reported:** conversions failing with "All sources are busy"; a plain
redeploy in the last 2h did not help.

**What the logs actually showed — two separate things:**

1. **The site is off for billing, not code.** `railway status` → service
   *Failed*, `activeDeployments: []`, latest deployment is an old June FAILED
   one; the URL returns Railway's own `{"code":404,"Application not found"}`.
   Deploy logs end with `Stopping Container` at **06:38:20 UTC 2026-07-31**
   with no replacement deployment. `railway up` / `railway redeploy` both
   refuse: **"Your trial has expired. Please select a plan to continue using
   Railway."** Nothing can deploy until a plan is chosen (billing = owner's
   call). Real conversions were still succeeding at 03:51–06:20 UTC that same
   morning, which is why this reads as a backend failure but is not one.

2. **bgutil watchdog wedge (real bug, fixed).** The 2026-07-12 rule "never
   restart a LIVE process" over-corrected: a bgutil server that is alive but
   *wedged* never recovers. Production logged **26,432 consecutive**
   `[bgutil] ping miss N (process alive, busy minting) — tolerating` lines
   (~9 days), while `_bgutil_ready` stayed **True** — so every yt-dlp command
   still passed `fetch_pot=always` + `base_url=` for a POT provider that never
   answered. Result: `/info` 504s and the ytdlp backend burning its full 150s
   leash on every job, i.e. "All sources are busy" the moment etacloud hiccups.

**Fix (server/app.py):** new `BGUTIL_STALL_MISSES = 8` (8 × 30s = 4 min) —
long enough for any genuine mint (mints take seconds), short enough that a
wedge self-heals; a live-but-unresponsive process is now restarted like a dead
one; `_bgutil_ready` is cleared on restart so yt-dlp stops advertising a dead
provider; miss log throttled (was 26k identical lines). Dockerfile
`CACHE_DATE` → `2026-07-31a` for a fresh yt-dlp@master + bgutil layer.

**Verified (locally on the VPS, since Railway won't accept a deploy):** ran the
patched server on :8099 → `POST /start {jNQXAC9IVRw, mp3, 320K}` → status
`done` in **~15s**, `/download` = **459,056-byte real MP3**, ffmpeg reads it as
`mp3 44100 Hz mono 192 kb/s, Duration 00:00:19.10` ("Me at the zoo" is 19s).
`/proxy-status` shows **y2mate/etacloud served it in 2s** — the upstream
backend is healthy, so the conversion pipeline is fine.

**Still open:** (a) pick a Railway plan, then `railway up --detach` — the code
is committed and ready; (b) 320K still yields 192 kbps (etacloud is
fixed-bitrate, pre-existing).

## 2026-08-16 — Recurrence: same billing block, not a code regression

**Symptom reported:** conversions failing "All sources are busy"; a plain
redeploy in the last 2h did not help (identical framing to 2026-07-31).

**Diagnosis:** `curl /` on the live URL returned Railway's own
`{"code":404,"message":"Application not found"}` — the app was never running,
so "All sources are busy" was never actually reachable; users/monitors were
hitting Railway's edge, not the Flask app. `railway up --detach` confirmed:
**"Usage limit exceeded. Please increase or remove the hard limit to resume
resource provisioning."** This is the same account-wide Railway usage cap
that's currently blocking taxiapp, five-m, backupbot, and the Discord bots
(active since 2026-08-14) — not the bgutil/yt-dlp issue this repo has chased
before. `git status` confirms the tree is clean and already in sync with
`origin/main`: the 2026-07-31 bgutil wedge fix and the XFF rate-limit fix are
both committed and pushed, so no code change was made or needed this round.

**Still open:** raise/remove the Railway hard usage limit (dashboard, owner's
call) — the moment that's done, `railway up --detach` should deploy the
already-fixed code with no further changes.

## 2026-08-17 — Third recurrence: same usage-limit block, no code bug

**Symptom reported:** conversions failing "All sources are busy"; a plain
redeploy in the last 2h did not help (identical framing to 2026-07-31 and
2026-08-16).

**Diagnosis:** `railway status` → service **Failed**, deployment
`a8cf9293…` (from the `CACHE_DATE=2026-08-17a` bump earlier today, commit
`3f44bb2`); `curl /health` on the live URL returns Railway's own
`{"code":404,"message":"Application not found"}` — the app isn't running, so
real users are hitting Railway's edge, not the Flask backend. `railway logs
--build` for that deployment shows the Docker build **and healthcheck both
succeeded** (image built, `[1/1] Healthcheck succeeded!`), so the code and
Dockerfile are fine. `railway up --detach` was attempted fresh and immediately
returned: **"Usage limit exceeded. Please increase or remove the hard limit to
resume resource provisioning."** — the same account-wide Railway cap that has
blocked this app (and taxiapp/five-m/backupbot/Discord bots) since
2026-08-14. This is why the earlier same-day redeploy "didn't help": it never
got a chance to run.

**Code check (no changes made — nothing was broken):** confirmed still in
place from prior fixes: `yt-dlp@master` pin (Dockerfile:24), `CACHE_DATE`
already bumped today, `BGUTIL_STALL_MISSES=8` watchdog fix
(server/app.py:108). `git status` clean, in sync with `origin/main`.

**Still open:** raise/remove the Railway hard usage limit (dashboard, owner's
call). Until then no `railway up` can provision resources, regardless of code
state.

## 2026-08-17 (re-check same day) — Re-verified: still the same usage-limit block

Re-ran the full diagnosis (same symptom report). `git status` clean/in sync
with `origin/main` (no drift since the last entry above). `curl /health` on
the live URL → still Railway's `{"code":404,"Application not found"}`.
`railway up --detach` → immediately **"Usage limit exceeded. Please increase
or remove the hard limit to resume resource provisioning."**, before any
upload/build happens. Nothing to fix in code; this is purely the account-wide
Railway cap. Not re-touching yt-dlp/bgutil/proxy code until this repro
changes shape (e.g. build fails, or app runs but conversions themselves
error) — that would point to a real regression instead of billing.
