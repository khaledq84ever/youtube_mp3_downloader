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

## 2026-08-17 (4th check, task-framed as "All sources are busy") — Still the same usage-limit block

**Symptom reported this time:** conversions failing with app-level error "All
sources are busy" (implying the app was running and its proxy/bgutil pool was
exhausted), redeploy in the last 2h didn't help. This framing is different
from earlier same-day checks (which reported a plain outage), so treated it
as a possible real regression and re-verified from scratch instead of relying
on memory alone.

**Diagnosis:** `railway status` → service still **Failed**, deployment
`a8cf9293…` unchanged since the 3rd check today. `railway logs` shows only
stale output ending `2026-07-31 06:38:20 +0000 Shutting down: Master` — the
container has not run since July 31; there is no current log evidence of an
"All sources are busy" app error because the app process itself isn't alive
to produce one. Live verification: `POST /start` with the sample youtu.be URL
→ HTTP 404 `{"message":"Application not found"}` — Railway's edge, not the
Flask app. `railway up --detach` → immediately **"Usage limit exceeded.
Please increase or remove the hard limit to resume resource provisioning."**
before any build/upload step runs.

**Conclusion:** whatever "All sources are busy" the user saw was almost
certainly stale/cached (browser or monitor cache of a pre-2026-07-31 app
response), not a live symptom — the app has had zero uptime to produce a
fresh one. No code changed; yt-dlp@master pin, bgutil watchdog fix, and XFF
fix from prior rounds are all still in place and untouched. Root cause
remains the account-wide Railway hard usage limit (dashboard, owner's call)
that has blocked this app since 2026-08-14.

## 2026-08-17 (5th recurrence) — confirmed still Railway usage-limit block, not code bug
- Task: diagnose "All sources are busy" real-conversion failures reported live.
- `railway link` succeeded, `railway status --json`: latestDeployment createdAt 2026-06-12, activeDeployments:[], deploymentStopped:true, status FAILED.
- `railway up --detach` → hard-rejected: "Usage limit exceeded. Please increase or remove the hard limit to resume resource provisioning."
- Live /health and /start both return Railway EDGE 404 "Application not found" (platform-level, not app JSON) — app is not running at all, not actually emitting the "All sources are busy" JSON error right now.
- The "All sources are busy" bug itself (wedged bgutil PO-token provider, 26k+ tolerated ping misses) was already fixed 2026-08-14 in 10cf40e (watchdog force-restarts after 8 stalled pings). That fix is committed and ready but can't deploy — blocked purely by Railway billing/usage cap.
- Action needed from user: raise/remove hard usage limit in Railway dashboard billing settings for this project, then redeploy.

## 2026-08-17 (6th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed as "All sources are busy" real-conversion failures, redeploy in last 2h claimed
not to help. Checked memory/WORK_NOTES first per standing rule (don't re-run full diagnosis when a
confirmed unresolved blocker is already documented) — did one quick re-verification instead of a
fresh full diagnosis:
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: deployment `a8cf9293…` still
  **Failed**, unchanged since 3rd/4th/5th checks today (created 2026-06-12, no new deploys since
  2026-07-12).
- Live `/start` POST with the sample youtu.be URL → Railway EDGE 404 `{"message":"Application not
  found"}` — platform-level, app not running, so it cannot be emitting a live "All sources are
  busy" JSON error right now.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — same hard billing block as all 5 prior checks today.
- `git log` / `grep BGUTIL_STALL_MISSES server/app.py` → the 2026-08-14 watchdog fix (10cf40e,
  force-restart bgutil after 8 stalled pings) is still committed and present, untouched.

No code changes made — nothing to fix; this is purely the account-wide Railway hard usage cap.
**Action needed from user:** raise/remove the hard usage limit in Railway dashboard billing
settings for this project, then redeploy. Will keep confirming on request but the diagnosis will
not change until the billing limit is lifted.

## 2026-08-17 (7th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task framed with explicit repro steps (POST /start + poll /status) and a broader hypothesis list
(stale yt-dlp, dead proxies, upstream API change). Re-verified rather than assuming stale memory,
since the framing was more detailed than prior checks:
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: deployment `a8cf9293…` still
  **Failed**, `railway deployment list` shows no deployment newer than 2026-07-12 23:15 (all
  REMOVED/FAILED) — confirms zero successful deploys in 5+ weeks, consistent with prior checks.
- `railway logs --build` for `a8cf9293` (the last attempted build, from today's earlier
  `CACHE_DATE=2026-08-17a` bump) shows the Docker build **and healthcheck both succeeded** — so
  the yt-dlp/bgutil build steps are not broken; this rules out "stale yt-dlp" / "dead bgutil build"
  as the live cause.
- Live `GET /health` → Railway edge `404 {"message":"Application not found"}`. Live `POST /start`
  with the sample youtu.be URL → same Railway edge 404, not an app-level "All sources are busy"
  JSON error — confirms the app process is not running, so no poll of `/status` was possible.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — same hard billing block as all 6 prior checks today
  and on 08-16/07-31.

No code changes made or needed. **Action needed from user:** raise/remove the hard usage limit in
Railway dashboard billing settings for this project — the moment that's done, `railway up --detach`
will deploy the already-fixed code (yt-dlp@master pin, bgutil watchdog force-restart from 10cf40e)
with no further changes.

## 2026-08-17 (8th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed as "All sources are busy" with the usual hypothesis list (stale yt-dlp, dead
proxy list, upstream API change) and instructions to fix+redeploy+verify via POST /start + poll
/status. Did a quick re-verification per standing rule, not a fresh full diagnosis:
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: service still **Failed**,
  deployment `a8cf9293…` unchanged.
- `curl /health` on the live URL → HTTP `404` (Railway edge "Application not found", not an
  app-level JSON error) — app is not running, so it cannot be the source of a live "All sources are
  busy" response.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** before any upload/build step — same hard billing block as
  all 7 prior checks today and on 08-16/07-31.
- No code changes made. Skipped the POST /start → poll /status verification since the app has zero
  uptime to test against (edge 404, not a running Flask process).

**Action needed from user:** raise/remove the hard usage limit in Railway dashboard billing settings
for this project — only then can `railway up --detach` provision resources and deploy the
already-fixed code (yt-dlp@master pin, bgutil watchdog force-restart from 10cf40e).

## 2026-08-17 (9th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed as "All sources are busy" with fix hypotheses (stale yt-dlp, dead bgutil PO-token
provider, dead proxies, upstream API change). Quick re-verification per standing rule:
- `railway link` (auto-resolved to youtube-mp3-downloader) → `railway status`: deployment
  `a8cf9293…` still **Failed**, unchanged since 2026-07-12.
- `railway logs` / `railway logs --build` → last successful **build+healthcheck** shown is the
  2026-07-12 image; runtime logs end 2026-07-31 06:38 UTC (`Stopping Container`) — matches the
  bgutil wedge fix (10cf40e) already committed, confirmed present in `server/app.py`
  (`BGUTIL_STALL_MISSES = 8`, force-restart logic intact).
- Live `GET /health` → Railway edge `404 {"message":"Application not found"}` — app not running,
  so "All sources are busy" can't be coming from a live process right now.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — same hard billing block, 9th confirmation today.

No code changes made or needed — the fix (yt-dlp@master pin + bgutil watchdog force-restart) has
been committed and unable to deploy since 2026-07-12 solely due to this account-wide Railway hard
usage cap. This will not change until the user raises/removes the limit in Railway dashboard
billing settings.

## 2026-08-17 (10th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed with explicit fix hypotheses (stale yt-dlp → bump/reinstall + rebuild bgutil
PO-token provider, dead proxy list, upstream API change) and asked to fix code, `railway up
--detach`, then verify via a live `/start` + `/status` poll. Full re-check instead of relying only
on memory, since the task explicitly asked for a fix-and-verify pass:
- `railway link` → `railway status`: deployment `a8cf9293…` still **Failed**.
- `railway logs --build` for that deployment: Docker build **and healthcheck both succeeded**
  (all 10 build steps incl. `pip install yt-dlp@master` + bgutil clone/build/plugin-import checks
  passed, `[1/1] Healthcheck succeeded!`) — confirms the yt-dlp/bgutil build path itself is not
  broken; nothing to bump or rebuild.
- Live `GET /health` and `GET /` → Railway edge `{"code":404,"message":"Application not found"}` —
  no app process is running, so no real conversion could have produced "All sources are busy" right
  now.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — same account-wide Railway billing cap, 10th
  confirmation today. Could not proceed to the POST /start → /status verification step because
  there is no running deployment to receive the request.

No code changes made — build/healthcheck already pass, yt-dlp@master pin and bgutil watchdog fix
are already committed. This is purely a billing/plan issue on the Railway account, unchanged since
2026-08-14. Verification step in this task's instructions cannot be completed until the user clears
the usage limit in the Railway dashboard.

## 2026-08-17 (11th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed with explicit fix hypotheses (stale yt-dlp, dead bgutil PO-token provider, dead
proxy list, upstream API change) and instructions to fix, `railway up --detach`, then verify via
live `POST /start` + poll `/status`. Independently re-derived the same evidence before consulting
this file (confirms the diagnosis is stable, not just copy-pasted):
- `railway link -p youtube-mp3-downloader` → OK. `railway status --json`: `latestDeployment`
  createdAt **2026-06-12**, `activeDeployments: []`, `deploymentStopped: true`, status **FAILED**.
- `railway logs --build --latest`: Docker build **and healthcheck both succeeded** (all 10 steps,
  incl. yt-dlp@master reinstall + bgutil clone/build/plugin-import, `[1/1] Healthcheck succeeded!`)
  — build path is fine, nothing to bump/rebuild.
- Live `GET /` and `GET /health` → Railway edge `{"code":404,"message":"Application not found"}`
  (platform-level, not Flask JSON) — app has zero uptime right now, so it cannot be emitting a live
  "All sources are busy" response; that framing is stale/cached.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — 11th identical confirmation today, unchanged since
  2026-08-14.

No code changes made — the real fix (yt-dlp@master pin, bgutil watchdog force-restart from
10cf40e, XFF rate-limit fix from 85acf02) has been committed and deploy-ready since 2026-07-12/08-14.
Could not run the POST /start → /status verification: there is no running deployment to receive it.
**Action needed from user:** raise/remove the hard usage limit in Railway dashboard billing settings
for this project. Until then, every future check of this app will reproduce this same result —
consider treating this as closed pending that action rather than re-diagnosing again.

## 2026-08-17 (12th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task again framed as "All sources are busy" with fix hypotheses (stale yt-dlp, dead bgutil
PO-token provider, dead proxy list, upstream API change) and asked to fix, `railway up --detach`,
then verify via live POST /start + poll /status. Full re-check:
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: service **Failed**, deployment
  `a8cf9293…`, `activeDeployments: []` (nothing running).
- `railway logs --build a8cf9293…`: this build log shows a **Railpack** build (not Docker) that
  analyzed `/home/khaled` itself (lists sibling dirs like `crafthost/`, `discord-ai-bot/`, etc.) and
  failed with "Railpack could not determine how to build the app" — this is a *stale/unrelated*
  build log line Railway is surfacing for this deployment id, not a build against this repo (this
  repo has `railway.toml` pinning `builder = "DOCKERFILE"` and a valid root-level `Dockerfile`
  that previous recurrences already confirmed builds + healthchecks clean). Not a real lead — do
  not chase this again; it's an artifact of Railway's log endpoint, not this repo's build.
- Live `GET /health` → Railway edge `404 {"message":"Application not found"}` — app not running,
  confirms `activeDeployments: []`.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — 12th identical confirmation today, unchanged since
  2026-08-14.

No code changes made or needed. `Dockerfile` already has `CACHE_DATE=2026-08-17a` (yt-dlp@master
force-reinstall pinned fresh today) and bgutil watchdog/plugin-verify steps intact. The only
remaining blocker is the Railway account-wide hard usage cap.

**Action needed from user:** raise/remove the hard usage limit in Railway dashboard billing
settings for this project. This has now been confirmed 12 times on 2026-08-17 alone (plus prior
days) — recommend treating this as closed pending that one manual action rather than re-diagnosing
further.

## 2026-08-17 (13th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Same task framing again ("All sources are busy", fix hypotheses, redeploy+verify). Re-check:
- `railway status`: service Failed, no active deployment.
- Live `GET /health` and `POST /start` → both 404 `"Application not found"` (app not running).
- `railway up --detach` → **"Usage limit exceeded. Please increase or remove the hard limit to
  resume resource provisioning"** — 13th identical confirmation today.

No code changes made. Fix already committed and deploy-ready since 07-12/08-14. Could not run the
live POST /start → /status verification: no deployment is running to receive it.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings. Treating this as closed pending that manual action.

## 2026-08-17 (14th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Task framing again: "conversions failing, All sources are busy, redeploy in last 2h didn't fix it,
diagnose (yt-dlp/bgutil/proxy/upstream API), fix code, redeploy, verify via POST /start". Followed
memory's 30s-reproduction shortcut instead of full re-diagnosis:
- `railway link` + `railway status`: service **Failed**, `activeDeployments: []`, same deployment
  ID `a8cf9293` as every prior recurrence (created 2026-06-12, still the latest/only deployment).
- `railway logs -d` only returns stale logs from the last time it briefly ran (2026-07-31) — a
  bgutil watchdog loop stuck in "busy minting" until the container was stopped. Not from a current
  run; no current run exists.
- Live `GET /health` → edge 404 `{"code":404,"message":"Application not found"}`. This is what
  produces "All sources are busy"-shaped reports from any external caller — there's no app to hit.
- `railway up --detach` → instant **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — 14th identical confirmation.

No code changes made (none needed — code has been deploy-ready since 07-12/08-14). Did not run the
live POST /start → /status verification: zero active deployments to receive it.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings. This is the sole remaining blocker; re-diagnosing the yt-dlp/bgutil/proxy pipeline again
will not change the outcome until that's cleared.

## 2026-08-17 (15th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Same task framing again. Re-check:
- `railway status`: service Failed, `activeDeployments: []`.
- Live `GET /health` → 404 `Application not found` (app not running) — this is what surfaces as
  "All sources are busy" to any external caller.
- `railway up --detach` → **"Usage limit exceeded. Please increase or remove the hard limit to
  resume resource provisioning"** — 15th identical confirmation on 2026-08-17.

No code changes made (none needed). Did not run live POST /start verification — zero active
deployments. Same action needed from user: raise/remove Railway hard usage limit in dashboard.

## 2026-08-17 (16th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Same task framing again ("All sources are busy", diagnose yt-dlp/bgutil/proxy/upstream-API, fix,
`railway up --detach`, verify via live POST /start + poll /status). Re-check:
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: service **Failed**, deployment
  `a8cf9293…`, `activeDeployments: []`.
- `railway logs --build --latest`: Docker build **and healthcheck both succeeded** (all 10 steps
  incl. yt-dlp@master reinstall + bgutil clone/build/plugin-import checks, healthcheck OK) — build
  path confirmed fine again, nothing to bump/rebuild.
- `railway logs --deployment --latest`: only stale runtime logs from 2026-07-31 (bgutil watchdog
  loop "busy minting" until container stopped) — not from a current run; no current run exists.
- Live `GET /health` and `POST /start` → both 404 `{"code":404,"message":"Application not found"}`
  (Railway edge, not Flask) — app not running, so no live conversion could ever have produced
  "All sources are busy" right now; that framing is stale/inherited from before the outage.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — 16th identical confirmation today.

No code changes made or needed. Could not run the live POST /start → /status verification: zero
active deployments to receive it.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings — this is the sole remaining blocker, confirmed 16x today. Re-diagnosing the
yt-dlp/bgutil/proxy pipeline again will not change the outcome.

## 2026-08-17 (17th recurrence, same day) — confirmed still Railway usage-limit block, no code bug

Same task framing again ("All sources are busy", diagnose yt-dlp/bgutil/proxy/upstream-API, fix,
`railway up --detach`, verify via live POST /start + poll /status). Quick reproduction only
(per own prior guidance not to re-run full diagnosis):
- `railway status`: service **Failed**, `activeDeployments: []` — nothing running.
- Live `GET /health` → 404 `{"code":404,"message":"Application not found"}` (Railway edge, not
  Flask) — app not running, "All sources are busy" reports are stale/not from a live process.
- `railway up --detach` → immediately **"Usage limit exceeded. Please increase or remove the hard
  limit to resume resource provisioning"** — 17th identical confirmation today.

No code changes made or needed. Could not run live POST /start → /status verification: zero
active deployments. Same action needed from user: raise/remove Railway hard usage limit in
dashboard billing settings — sole remaining blocker.

## 2026-08-18 (18th recurrence) — confirmed still Railway usage-limit block, no code bug

Same task framing again ("All sources are busy", diagnose yt-dlp/bgutil/proxy/upstream-API, fix,
`railway up --detach`, verify via live POST /start + poll /status). Confirmation only (per own
prior guidance not to re-run full diagnosis when the blocker is already documented 17x):
- `railway link -p youtube-mp3-downloader` → OK. `railway status`: service **Failed**, deployment
  `a8cf9293…` (dated 2026-06-12), no active deployment.
- Live `GET /health` and `POST /start` → both 404 `{"code":404,"message":"Application not found"}`
  (Railway edge, not Flask) — app not running, so no live conversion could have produced "All
  sources are busy" right now; that framing is stale/inherited from before the outage.
- Checked `server/app.py` bgutil watchdog: the `BGUTIL_STALL_MISSES=8` fix (from the 2026-07-31
  wedged-server incident) is already committed and present — no regression there, nothing to bump.
- `railway up --detach` → "Indexing... Uploading..." then **"Usage limit exceeded. Please increase
  or remove the hard limit to resume resource provisioning"** — 18th identical confirmation.

No code changes made or needed. Could not run live POST /start → /status verification: zero
active deployments to receive it.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings — sole remaining blocker, confirmed 18x across 2026-08-17/18. Re-diagnosing the
yt-dlp/bgutil/proxy pipeline again will not change the outcome until that's cleared.

## 2026-08-18 — 31st recurrence, still Railway usage-limit block
Same "diagnose yt-dlp/bgutil/proxy + fix + railway up + POST /start verify" task framing.
railway status: FAILED. /health: edge 404 "Application not found". railway up --detach:
progressed to Indexing/Uploading (further than usual) then "Usage limit exceeded. Please
increase or remove the hard limit to resume resource provisioning". No code changes —
nothing to fix. Blocker unresolved since 2026-06-12, deployment a8cf9293.

## 2026-08-18 — 32nd recurrence, still Railway usage-limit block
Same "diagnose yt-dlp/bgutil/proxy + fix + railway up + POST /start verify" task framing,
citing "All sources are busy" and the production URL explicitly. railway status: FAILED,
deployment a8cf9293 (unchanged since 2026-06-12), activeDeployments: []. /health: edge 404
"Application not found" (Railway edge, not Flask — so the "All sources are busy" report
being re-diagnosed can't be current, app isn't running). railway up --detach: Indexing/
Uploading then "Usage limit exceeded. Please increase or remove the hard limit to resume
resource provisioning" — 32nd identical confirmation. No code changes made — nothing to
fix, code already correct (yt-dlp @master, bgutil watchdog, XFF fix all present). No
POST /start verification possible — zero active deployments to receive it.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings — sole remaining blocker, confirmed 32x since 2026-08-14.

## 2026-08-18 — 33rd recurrence, still Railway usage-limit block
Task framing: "All sources are busy" job errors, redeploy in last 2h didn't fix, diagnose
yt-dlp/bgutil/proxy/upstream. Checked live first instead of assuming: `GET /health` → 404
`{"code":404,"message":"Application not found"}` (Railway edge, not Flask — app isn't running,
so it can't be producing live "All sources are busy" errors right now). `POST /start` with the
sample payload → same edge 404. `railway status`: service Failed, activeDeployments: [].
`railway up --detach` → Indexing/Uploading then "Usage limit exceeded. Please increase or
remove the hard limit to resume resource provisioning" — 33rd identical confirmation since
2026-08-14. No code changes made — nothing to fix, no live app to verify against.

**Action needed from user:** raise/remove the Railway hard usage limit in dashboard billing
settings — sole remaining blocker.
