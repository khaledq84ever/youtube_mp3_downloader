from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil, socket
import urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Global socket fallback so any bare urlopen (pytubefix internals, etc.)
# cannot hang forever on a dead proxy. Explicit timeouts in our own
# urlopen() calls still override this.
socket.setdefaulttimeout(20)

try:
    from flask_compress import Compress
    _COMPRESS_OK = True
except ImportError:
    _COMPRESS_OK = False

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

try:
    from pytubefix import YouTube as PyTube
    _PYTUBE_OK = True
except ImportError:
    _PYTUBE_OK = False

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
CORS(app)
if _COMPRESS_OK:
    app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/javascript',
                                        'application/javascript', 'application/json',
                                        'image/svg+xml']
    app.config['COMPRESS_LEVEL']     = 6
    app.config['COMPRESS_MIN_SIZE']  = 500
    Compress(app)

# ── bgutil PO token HTTP server ───────────────────────────────────────────────
BGUTIL_PORT      = 4416
BGUTIL_BASE_URL  = f'http://127.0.0.1:{BGUTIL_PORT}'
_bgutil_proc     = None
_bgutil_ready    = False

def _start_bgutil_server():
    global _bgutil_proc, _bgutil_ready
    # Look for the bgutil server in common locations
    search_dirs = [
        os.path.expanduser('~/bgutil-ytdlp-pot-provider/server'),
        '/app/bgutil-ytdlp-pot-provider/server',
        os.path.join(os.path.dirname(_HERE), 'bgutil-ytdlp-pot-provider', 'server'),
    ]
    server_dir = None
    for d in search_dirs:
        if os.path.isfile(os.path.join(d, 'build', 'main.js')):
            server_dir = d
            break
    if not server_dir:
        print('[bgutil] Server not found — PO token generation via HTTP unavailable')
        return

    node = shutil.which('node') or 'node'
    main_js = os.path.join(server_dir, 'build', 'main.js')
    try:
        _bgutil_proc = subprocess.Popen(
            [node, main_js, '-p', str(BGUTIL_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Wait up to 10 s for the server to come up
        for _ in range(20):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f'{BGUTIL_BASE_URL}/ping', timeout=1) as r:
                    if r.getcode() == 200:
                        _bgutil_ready = True
                        print(f'[bgutil] PO Token server ready on port {BGUTIL_PORT}')
                        return
            except Exception:
                pass
        print('[bgutil] Server started but /ping not responding within 10 s')
    except Exception as ex:
        print(f'[bgutil] Failed to start server: {ex}')

threading.Thread(target=_start_bgutil_server, daemon=True).start()

DOWNLOAD_DIR  = '/tmp/ytdl_cache'
YTDLP           = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL        = 1800          # 30 min
JOB_TIMEOUT     = 45            # 45 s per yt-dlp attempt (was 90 — never hits limit when blocked)
MAX_YTDLP_TRIES = 4             # 4 proxy attempts (was 8 — most blocks repeat across IPs)
GLOBAL_JOB_TTL  = 180           # give up entire job after 3 min (was 10 — UX killer)
RATE_LIMIT      = 30            # per minute per IP

# ── Proxy pool (rotates every job, auto-heals on failure) ─────────────────────

_PROXY_LIST = [
    'http://pxosioyq-gb-1:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ca-2:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-de-3:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-fr-4:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-au-5:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-nl-6:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-it-7:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-es-8:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-be-9:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-at-10:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ch-11:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-se-12:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-no-13:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-dk-14:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-fi-15:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ie-16:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-pt-17:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-nz-18:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-pl-19:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-kr-20:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-jp-21:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-br-22:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-mx-23:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-in-24:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-sg-25:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-hk-26:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-za-27:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ar-28:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-cl-29:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-us-30:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-gb-31:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ca-32:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-de-33:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-fr-34:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-au-35:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-nl-36:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-it-37:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-es-38:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-be-39:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-at-40:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ch-41:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-se-42:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-no-43:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-dk-44:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-fi-45:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-ie-46:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-pt-47:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-nz-48:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-pl-49:n08bo6f1b3c5@p.webshare.io:80',
    'http://pxosioyq-kr-50:n08bo6f1b3c5@p.webshare.io:80',
]

class ProxyRotator:
    def __init__(self, proxies):
        self._all   = list(proxies)
        self._pool  = list(proxies)
        self._idx   = 0
        self._lock  = threading.Lock()

    def get(self):
        with self._lock:
            if not self._pool:
                self._pool = list(self._all)
                self._idx  = 0
            proxy = self._pool[self._idx % len(self._pool)]
            self._idx = (self._idx + 1) % len(self._pool)
            return proxy

    def rotate(self):
        with self._lock:
            n = max(len(self._pool), 1)
            self._idx = (self._idx + 1) % n

    def mark_failed(self, proxy):
        with self._lock:
            if proxy in self._pool:
                self._pool.remove(proxy)
            if not self._pool:
                self._pool = list(self._all)
            self._idx = self._idx % max(len(self._pool), 1)

    def args(self, proxy=None):
        p = proxy or self.get()
        return ['--proxy', p] if p else []

    def opener(self, proxy=None):
        p = proxy or self.get()
        if not p:
            return urllib.request.build_opener()
        handler = urllib.request.ProxyHandler({'http': p, 'https': p})
        return urllib.request.build_opener(handler)

_proxy_rotator   = ProxyRotator(_PROXY_LIST)
_proxy_log       = []          # live event log
_proxy_log_lock  = threading.Lock()
_proxy_rotations = 0           # total rotation counter

_COUNTRY_MAP = {
    'gb':'🇬🇧 UK','ca':'🇨🇦 Canada','de':'🇩🇪 Germany','fr':'🇫🇷 France',
    'au':'🇦🇺 Australia','nl':'🇳🇱 Netherlands','it':'🇮🇹 Italy',
    'es':'🇪🇸 Spain','be':'🇧🇪 Belgium','at':'🇦🇹 Austria',
    'ch':'🇨🇭 Switzerland','se':'🇸🇪 Sweden','no':'🇳🇴 Norway',
    'dk':'🇩🇰 Denmark','fi':'🇫🇮 Finland','ie':'🇮🇪 Ireland',
    'pt':'🇵🇹 Portugal','nz':'🇳🇿 New Zealand','pl':'🇵🇱 Poland',
    'kr':'🇰🇷 Korea','jp':'🇯🇵 Japan','br':'🇧🇷 Brazil',
    'mx':'🇲🇽 Mexico','in':'🇮🇳 India','sg':'🇸🇬 Singapore',
    'hk':'🇭🇰 Hong Kong','za':'🇿🇦 S.Africa','ar':'🇦🇷 Argentina',
    'cl':'🇨🇱 Chile','us':'🇺🇸 USA',
}

def _proxy_label(proxy):
    try:
        user = proxy.split('@')[0].split('://')[-1].split(':')[0]
        parts = user.split('-')
        cc = parts[1] if len(parts) > 1 else '??'
        num = parts[2] if len(parts) > 2 else '?'
        country = _COUNTRY_MAP.get(cc, f'🌐 {cc.upper()}')
        return user, country, num
    except Exception:
        return proxy, '🌐 Unknown', '?'

def _log_proxy_event(proxy, result, detail=''):
    global _proxy_rotations
    user, country, num = _proxy_label(proxy)
    with _proxy_log_lock:
        if result in ('rotated', 'blocked'):
            _proxy_rotations += 1
        _proxy_log.insert(0, {
            'time':    time.strftime('%H:%M:%S'),
            'user':    user,
            'country': country,
            'num':     num,
            'result':  result,
            'detail':  detail[:100],
        })
        if len(_proxy_log) > 200:
            _proxy_log.pop()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def _proxy_args(proxy=None):
    return _proxy_rotator.args(proxy)

jobs          = {}
jobs_lock     = threading.Lock()
url_jobs      = {}
url_jobs_lock = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()


# ── yt-dlp: update at startup, then every 24 h ───────────────────────────────

def _update_ytdlp_loop():
    while True:
        try:
            subprocess.run([YTDLP, '--update-to', 'stable'],
                           capture_output=True, timeout=120)
        except Exception:
            pass
        time.sleep(86400)   # 24 h

threading.Thread(target=_update_ytdlp_loop, daemon=True).start()


# ── Job persistence ───────────────────────────────────────────────────────────

def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

def _load_jobs():
    for p in glob.glob(os.path.join(DOWNLOAD_DIR, 'job_*.json')):
        try:
            with open(p) as f:
                job = json.load(f)
            job_id = os.path.basename(p)[4:-5]
            if job.get('status') in ('pending', 'processing'):
                job['status'] = 'error'
                job['error']  = 'Server restarted. Please convert again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p)
                continue
            jobs[job_id] = job
        except Exception:
            pass

_load_jobs()

# Clean up files older than 1 hour from a previous container run
def _startup_cleanup():
    cutoff = time.time() - 3600
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, '*')):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except Exception:
            pass

threading.Thread(target=_startup_cleanup, daemon=True).start()


# ── URL helpers ───────────────────────────────────────────────────────────────

_YT_DOMAIN_RE = re.compile(
    r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)',
    re.IGNORECASE)

_VIDEO_ID_RE = re.compile(
    r'(?:v=|/(?:shorts|live|embed|v)/|youtu\.be/)([a-zA-Z0-9_-]{11})')

def is_valid_url(url):
    return bool(_YT_DOMAIN_RE.search(url))

def extract_video_id(url):
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None

def normalize_url(url):
    try:
        vid = extract_video_id(url)
        if vid:
            return f'https://www.youtube.com/watch?v={vid}'
        p    = urllib.parse.urlparse(url)
        keep = {k: v for k, v in urllib.parse.parse_qs(p.query, keep_blank_values=True).items()
                if k in ('v', 'list', 'index')}
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(keep, doseq=True)))
    except Exception:
        return url

def is_playlist_only(url):
    if 'v=' in url or '/shorts/' in url or '/live/' in url:
        return False
    return 'playlist?' in url or '/playlist' in url

def _extract_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else None


# ── Error parsing ─────────────────────────────────────────────────────────────

def parse_ytdlp_error(stderr):
    err = (stderr or '').lower()
    if 'sign in' in err or 'confirm you' in err or 'bot' in err:
        return '__BOT__'
    if 'po token' in err:
        return '__BOT__'
    if '402' in err or 'payment required' in err:
        return '__PROXY_EXPIRED__'   # proxy subscription expired — skip, don't loop
    if 'age' in err and ('restrict' in err or 'gate' in err or '-restricted' in err):
        return 'This video is age-restricted and cannot be downloaded.'
    if 'private video' in err or ('private' in err and 'video' in err):
        return 'This video is private or no longer available.'
    if 'has been removed' in err or 'no longer available' in err:
        return 'This video has been removed or is no longer available.'
    if ('not available' in err or 'unavailable' in err) and ('country' in err or 'region' in err):
        return '__BOT__'
    if 'live event' in err or ('live' in err and ('stream' in err or 'broadcast' in err)):
        return 'Live streams cannot be downloaded. Try after the stream ends.'
    if 'copyright' in err:
        return 'This video is unavailable due to copyright restrictions.'
    return '__BOT__'


# ── Source pool — Piped + Invidious (self-healing every 30 min) ───────────────

_ALL_PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://pipedapi.r4fo.com',
    'https://pipedapi.qdi.fi',
    'https://pipedapi.smnz.de',
    'https://pipedapi.ducks.party',
    'https://pipedapi.darkness.services',
    'https://api.piped.privacydev.net',
    'https://pipedapi.drgns.space',
    'https://pipedapi.tokhmi.xyz',
    'https://api.piped.yt',
    'https://piped-api.privacy.com.de',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.ngn.tf',
    'https://pipedapi.moomoo.me',
    'https://watchapi.whatever.social',
]

_ALL_INVIDIOUS = [
    'https://invidious.f5.si',
    'https://invidious.materialio.us',
    'https://invidious.einfach.tech',
    'https://invidious.adminforge.de',
    'https://invidious.flokinet.to',
    'https://iv.duti.dev',
    'https://invidious.nerdvpn.de',
    'https://yewtu.be',
    'https://inv.nadeko.net',
    'https://invidious.privacydev.net',
    'https://iv.datura.network',
    'https://invidious.perennialte.ch',
    'https://invidious.lunar.icu',
    'https://invidious.projectsegfau.lt',
    'https://invidious.privacyredirect.com',
    'https://invidious.drgns.space',
]

_working_piped     = []
_working_invidious = []
_sources_lock      = threading.Lock()
_last_probe        = 0.0
_PROBE_VIDEO       = 'dQw4w9WgXcQ'
PROBE_INTERVAL     = 1800   # 30 min

def _probe_sources():
    global _working_piped, _working_invidious, _last_probe
    piped_ok, inv_ok = [], []

    for inst in _ALL_PIPED:
        try:
            req = urllib.request.Request(
                f'{inst}/streams/{_PROBE_VIDEO}',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=7) as r:
                data = json.loads(r.read())
            if not data.get('error') and data.get('audioStreams'):
                piped_ok.append((time.time() - t0, inst))
        except Exception:
            pass

    for inst in _ALL_INVIDIOUS:
        try:
            req = urllib.request.Request(
                f'{inst}/api/v1/videos/{_PROBE_VIDEO}?fields=adaptiveFormats',
                headers={'User-Agent': 'Mozilla/5.0'})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=7) as r:
                data = json.loads(r.read())
            if data.get('adaptiveFormats'):
                inv_ok.append((time.time() - t0, inst))
        except Exception:
            pass

    with _sources_lock:
        _working_piped     = [i for _, i in sorted(piped_ok)]
        _working_invidious = [i for _, i in sorted(inv_ok)]
        _last_probe        = time.time()

def _ensure_sources_fresh():
    if time.time() - _last_probe > PROBE_INTERVAL:
        threading.Thread(target=_probe_sources, daemon=True).start()

threading.Thread(target=_probe_sources, daemon=True).start()


# ── Proxy startup probe — auto-remove dead proxies ────────────────────────────

def _probe_proxies():
    test_url = 'https://www.youtube.com/robots.txt'

    def _check(proxy):
        try:
            opener = _proxy_rotator.opener(proxy)
            with opener.open(test_url, timeout=8) as r:
                return proxy, r.getcode() in (200, 301, 302)
        except Exception:
            return proxy, False

    with ThreadPoolExecutor(max_workers=20) as ex:
        for proxy, ok in ex.map(_check, _PROXY_LIST):
            if not ok:
                _proxy_rotator.mark_failed(proxy)

    with _proxy_rotator._lock:
        active = len(_proxy_rotator._pool)
    print(f'[proxy] Startup probe done: {active}/{len(_PROXY_LIST)} proxies alive')

threading.Thread(target=_probe_proxies, daemon=True).start()


# ── Piped helpers ─────────────────────────────────────────────────────────────

def piped_get_streams(video_id):
    _ensure_sources_fresh()
    with _sources_lock:
        instances = (list(_working_piped) or _ALL_PIPED[:5])[:5]
    for inst in instances:
        try:
            req = urllib.request.Request(
                f'{inst}/streams/{video_id}',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            if not data.get('error'):
                return data
        except Exception:
            continue
    return None

def _piped_best_audio(d):
    best = None
    for f in d.get('audioStreams', []):
        if not best or f.get('bitrate', 0) > best.get('bitrate', 0):
            best = f
    return best

def _piped_best_video(d, max_h=None):
    best = None
    for f in d.get('videoStreams', []):
        try: h = int(f.get('quality', '0').replace('p', ''))
        except: h = 0
        if max_h and h > max_h:
            continue
        if not best or h > int(best.get('quality', '0').replace('p', '') or 0):
            best = f
    return best


# ── Invidious helpers ─────────────────────────────────────────────────────────

def invidious_get_streams(video_id):
    _ensure_sources_fresh()
    with _sources_lock:
        instances = (list(_working_invidious) or _ALL_INVIDIOUS[:5])[:5]
    for inst in instances:
        try:
            req = urllib.request.Request(
                f'{inst}/api/v1/videos/{video_id}?fields=title,author,lengthSeconds,adaptiveFormats,videoThumbnails',
                headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            audio = [f for f in data.get('adaptiveFormats', []) if 'audio' in f.get('type', '')]
            if audio:
                return data
        except Exception:
            continue
    return None


# ── oEmbed info (works from any IP — no bot detection) ───────────────────────

def _oembed_info(video_id):
    try:
        yt_url = f'https://www.youtube.com/watch?v={video_id}'
        req = urllib.request.Request(
            f'https://www.youtube.com/oembed?url={urllib.parse.quote(yt_url)}&format=json',
            headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        if d.get('title'):
            return d
    except Exception:
        pass
    return None

def _yt_duration_from_page(video_id):
    try:
        req = urllib.request.Request(
            f'https://www.youtube.com/watch?v={video_id}',
            headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read(80000).decode('utf-8', errors='ignore')
        m = re.search(r'"lengthSeconds"\s*:\s*"?(\d+)"?', html)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


# ── Filename / ffmpeg helpers ─────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:Official\s+(?:Video|Music\s+Video|Audio|Lyric[s]?\s+Video|Lyrics?)|'
    r'(?:4K|HD|Full\s+HD)(?:\s+Remaster(?:ed)?)?|Remaster(?:ed)?|'
    r'Lyrics?|Audio|Visualizer|Full\s+(?:Video|Song)|Music\s+Video|'
    r'Official|Video\s+Clip|Clip)\s*[\)\]]\s*',
    re.IGNORECASE
)

def make_filename(title, uploader='', ext='mp3'):
    clean = _NOISE_RE.sub(' ', title).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    name  = f'{uploader} - {clean}' if uploader and uploader.lower() not in clean.lower() else clean
    name  = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    return (name[:80] or 'download') + '.' + ext

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p:
        return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/run/current-system/sw/bin',
              '/usr/bin', '/usr/local/bin']:
        if os.path.isfile(os.path.join(d, 'ffmpeg')):
            return d
    hits = glob.glob('/nix/store/*/bin/ffmpeg')
    return os.path.dirname(hits[0]) if hits else None

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        try: os.remove(_job_path(job_id))
        except Exception: pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


# ── Shared stream downloader + ffmpeg converter ───────────────────────────────

_FFMPEG_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+)')
_FFMPEG_TIME_RE     = re.compile(r'time=(\d+):(\d+):(\d+)')

def _get_ffmpeg():
    ffmpeg = shutil.which('ffmpeg') or 'ffmpeg'
    d = _find_ffmpeg_dir()
    return os.path.join(d, 'ffmpeg') if d else ffmpeg

def _download_stream(job_id, stream_url, out_path, progress_start=10, progress_end=85):
    """Stream a URL to disk with progress updates. Socket timeout = 30 s applies
    to BOTH connect and each read, so a stalled CDN can never hang us forever."""
    req = urllib.request.Request(stream_url,
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.youtube.com/'})
    total, done = 0, 0
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get('Content-Length', 0))
        with open(out_path, 'wb') as f:
            while True:
                chunk = r.read(524288)   # 512 KB chunks
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(progress_start + int(done / total * (progress_end - progress_start)),
                              progress_end)
                else:
                    # No Content-Length: bump progress every chunk so UI doesn't look frozen
                    pct = min(progress_start + (done // (1024 * 1024)),
                              progress_end)
                with jobs_lock:
                    if jobs.get(job_id, {}).get('status') == 'processing':
                        jobs[job_id]['progress'] = pct
                        jobs[job_id]['last_progress_at'] = time.time()

def _ffmpeg_stream_convert(job_id, stream_url, dst, quality,
                           referer='https://www.youtube.com/'):
    """Single-pass: ffmpeg fetches the URL and converts to mp3 simultaneously.
    Fastest path — no separate download step.

    Stall watchdog: if no progress for STALL_LIMIT seconds, kill ffmpeg.
    Hard cap: total runtime cannot exceed HARD_LIMIT seconds.
    """
    STALL_LIMIT = 60
    HARD_LIMIT  = 300
    kbps = (quality or '320K').rstrip('Kk')
    _set_job(job_id, {'progress': 5, 'last_progress_at': time.time()})
    cmd = [
        _get_ffmpeg(), '-y',
        '-rw_timeout', '30000000',  # 30s I/O timeout (microseconds) — kills hung HTTP reads
        '-headers', f'User-Agent: Mozilla/5.0\r\nReferer: {referer}\r\n',
        '-i', stream_url,
        '-vn', '-ar', '44100', '-ac', '2', '-b:a', f'{kbps}k', dst
    ]
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    started = time.time()
    last_progress = started
    killed = False

    def _watchdog():
        nonlocal killed
        while proc.poll() is None:
            time.sleep(2)
            now = time.time()
            if now - last_progress > STALL_LIMIT or now - started > HARD_LIMIT:
                killed = True
                try: proc.kill()
                except Exception: pass
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    total_secs = 0
    for line in proc.stderr:
        dm = _FFMPEG_DURATION_RE.search(line)
        if dm and not total_secs:
            total_secs = int(dm.group(1))*3600 + int(dm.group(2))*60 + int(dm.group(3))
            last_progress = time.time()
        tm = _FFMPEG_TIME_RE.search(line)
        if tm and total_secs:
            done = int(tm.group(1))*3600 + int(tm.group(2))*60 + int(tm.group(3))
            pct = min(int(done / total_secs * 85) + 10, 90)
            last_progress = time.time()
            with jobs_lock:
                if jobs.get(job_id, {}).get('status') == 'processing':
                    jobs[job_id]['progress'] = pct
                    jobs[job_id]['last_progress_at'] = last_progress
    try:
        proc.wait(timeout=HARD_LIMIT)
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except Exception: pass
        return False
    if killed:
        return False
    return proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024

def _ffmpeg_to_mp3(src, dst, quality):
    kbps = (quality or '320K').rstrip('Kk')
    res = subprocess.run(
        [_get_ffmpeg(), '-y', '-i', src, '-vn', '-ar', '44100', '-ac', '2',
         '-b:a', f'{kbps}k', dst],
        capture_output=True, timeout=300)
    return res.returncode == 0 and os.path.exists(dst)

def _ffmpeg_merge(v_src, a_src, dst):
    res = subprocess.run(
        [_get_ffmpeg(), '-y', '-i', v_src, '-i', a_src,
         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
         '-movflags', '+faststart', dst],
        capture_output=True, timeout=300)
    return res.returncode == 0 and os.path.exists(dst)


# ── y2mate.nu scraper backend ─────────────────────────────────────────────────
# Reverse-engineered from https://v3.y2mate.nu/js/.../y2mate.js
# Their server (etacloud.org) does the YouTube extraction for us, so this
# path works even when our own IP is bot-blocked and proxies are down.

import base64
import http.cookiejar

_Y2MATE_PAGE = 'https://v3.y2mate.nu/'
# Rotate through real browser UAs so y2mate doesn't fingerprint us as the
# same headless client across many requests from the Railway IP.
_Y2MATE_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
]
_y2mate_ua_idx = 0
_y2mate_ua_lock = threading.Lock()
def _y2mate_pick_ua():
    global _y2mate_ua_idx
    with _y2mate_ua_lock:
        ua = _Y2MATE_UAS[_y2mate_ua_idx % len(_Y2MATE_UAS)]
        _y2mate_ua_idx += 1
    return ua

def _y2mate_session():
    """One opener with a CookieJar so y2mate sees us as a coherent browser
    session (homepage → init → convert → progress → download)."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar

def _y2mate_get(opener, ua, url, timeout=20):
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept': 'text/html,application/json,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': _Y2MATE_PAGE,
        'Origin':  'https://v3.y2mate.nu',
        'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-site',
    })
    return opener.open(req, timeout=timeout).read()

def _y2mate_auth(cfg):
    arr0, arr2, rev = cfg[0], cfg[2], cfg[1]
    s = ''.join(chr(arr0[i] - arr2[len(arr2) - (i + 1)]) for i in range(len(arr0)))
    if rev: s = s[::-1]
    return s[:32]

def _y2mate_bump(job_id, pct):
    if not job_id:
        return
    with jobs_lock:
        j = jobs.get(job_id)
        if j and j.get('status') == 'processing' and j.get('progress', 0) < pct:
            j['progress'] = pct
            j['last_progress_at'] = time.time()

def _y2mate_resolve(youtube_url, fmt='mp3', job_id=None):
    """Return (signed_download_url, title, opener, ua) or raise on failure."""
    opener, _jar = _y2mate_session()
    ua = _y2mate_pick_ua()

    html = _y2mate_get(opener, ua, _Y2MATE_PAGE).decode('utf-8', 'replace')
    m = re.search(r"json\s*=\s*JSON\.parse\('([^']+)'\)", html)
    if not m:
        raise RuntimeError('no config on page')
    cfg = json.loads(m.group(1))
    auth_param = chr(cfg[6])
    auth = _y2mate_auth(cfg)
    backend = base64.b64decode('ZXRhY2xvdWQub3Jn').decode()  # etacloud.org

    vm = re.search(r'(?:v=|youtu\.be/|/shorts/|/live/)([a-zA-Z0-9_-]{11})', youtube_url)
    if not vm:
        raise RuntimeError('bad youtube url')
    vid = vm.group(1)

    ts = int(time.time())
    init_url = f'https://eta.{backend}/api/v1/init?{auth_param}={urllib.parse.quote(auth)}&t={ts}'
    d = json.loads(_y2mate_get(opener, ua, init_url))
    if int(d.get('error', 0) or 0) > 0:
        raise RuntimeError(f'init error={d.get("error")}')
    convert_url = d['convertURL']
    _y2mate_bump(job_id, 6)

    for _ in range(3):
        ts = int(time.time())
        c_url = f'{convert_url}&v={vid}&f={fmt}&t={ts}'
        d = json.loads(_y2mate_get(opener, ua, c_url))
        if int(d.get('error', 0) or 0) > 0:
            raise RuntimeError(f'convert error={d.get("error")}')
        if d.get('redirect') == 1:
            convert_url = d['redirectURL']
            continue
        break

    progress_url = d['progressURL']
    download_url = d['downloadURL']
    title        = d.get('title') or ''
    _y2mate_bump(job_id, 7)

    # Poll until y2mate's server finishes conversion (their progress == 3).
    # Bump our local progress every iteration so the UI doesn't look frozen at 5%.
    deadline = time.time() + 90
    polls = 0
    while time.time() < deadline:
        ts = int(time.time())
        pd = json.loads(_y2mate_get(opener, ua, f'{progress_url}&t={ts}'))
        if int(pd.get('error', 0) or 0) > 0:
            raise RuntimeError(f'progress error={pd.get("error")}')
        yp = int(pd.get('progress', 0) or 0)
        if yp >= 3:
            _y2mate_bump(job_id, 10)
            return download_url, pd.get('title') or title, opener, ua
        # Map y2mate's coarse 0/1/2 to our 7..9, plus a poll-count tick so the
        # bar visibly moves even while y2mate sits at the same internal step.
        polls += 1
        _y2mate_bump(job_id, min(7 + yp + (polls // 3), 9))
        time.sleep(1.5)
    raise RuntimeError('conversion timeout')

def y2mate_download(job_id, url, title, uploader, quality, fmt):
    """Backend: y2mate.nu — most reliable when our IP is bot-blocked.
    Quality is fixed by y2mate (mp3=192 kbps, mp4=360p); 'quality' arg ignored.
    Retries 3x with a fresh session each time before giving up.
    """
    last_err = ''
    signed_url = fetched_title = sess_opener = sess_ua = None
    _set_job(job_id, {'progress': 5, 'last_progress_at': time.time()})
    for attempt in range(3):
        try:
            signed_url, fetched_title, sess_opener, sess_ua = _y2mate_resolve(
                url, 'mp3' if fmt != 'mp4' else 'mp4', job_id=job_id)
            break
        except Exception as ex:
            last_err = f'{type(ex).__name__}: {ex}'[:140]
            time.sleep(1.5)
    if not signed_url:
        _log_proxy_event('y2mate', 'error', f'y2mate resolve: {last_err}')
        return False

    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'
    out     = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')
    try:
        # Reuse the same session (cookies + UA) for the actual file fetch
        req = urllib.request.Request(signed_url, headers={
            'User-Agent': sess_ua, 'Accept': '*/*',
            'Referer': _Y2MATE_PAGE, 'Origin': 'https://v3.y2mate.nu',
        })
        with sess_opener.open(req, timeout=30) as r:
            total = int(r.headers.get('Content-Length', 0))
            done  = 0
            with open(out, 'wb') as f:
                while True:
                    chunk = r.read(524288)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = min(10 + int(done / total * 80), 90)
                    else:
                        pct = min(10 + done // (1024 * 1024), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress']         = pct
                            jobs[job_id]['last_progress_at'] = time.time()
    except Exception:
        return False

    if not os.path.exists(out) or os.path.getsize(out) < 1024:
        return False

    fname = make_filename(title or fetched_title or 'video',
                          uploader or '', ext)
    _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
    schedule_cleanup(job_id, out)
    return True


# ── Download backends ─────────────────────────────────────────────────────────

def piped_download(job_id, video_id, url, title, uploader, quality, fmt):
    data = piped_get_streams(video_id)
    if not data:
        return False
    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'

    try:
        _set_job(job_id, {'progress': 5})
        if fmt == 'mp4':
            q_map   = {'720': 720, '1080': 1080, '4k': 2160, 'best': None}
            vstream = _piped_best_video(data, q_map.get(quality))
            astream = _piped_best_audio(data)
            if not vstream or not vstream.get('url'):
                return False
            v_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_v.mp4')
            a_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_a.m4a')
            out   = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp4')
            _download_stream(job_id, vstream['url'], v_tmp, 10, 55)
            if astream and astream.get('url') and astream['url'] != vstream.get('url'):
                _download_stream(job_id, astream['url'], a_tmp, 55, 80)
                if not _ffmpeg_merge(v_tmp, a_tmp, out):
                    return False
                for f in [v_tmp, a_tmp]:
                    try: os.remove(f)
                    except: pass
            else:
                shutil.move(v_tmp, out)
        else:
            astream = _piped_best_audio(data)
            if not astream or not astream.get('url'):
                return False
            out = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            # Single-pass: ffmpeg downloads + converts simultaneously
            if not _ffmpeg_stream_convert(job_id, astream['url'], out, quality):
                return False

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        fname = make_filename(title or data.get('title', 'video'),
                              uploader or data.get('uploader', ''), ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True
    except Exception:
        return False


def invidious_download(job_id, video_id, url, title, uploader, quality, fmt):
    data = invidious_get_streams(video_id)
    if not data:
        return False
    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'
    out     = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')

    try:
        formats = data.get('adaptiveFormats', [])
        if fmt == 'mp4':
            max_h   = {'720': 720, '1080': 1080, '4k': 2160}.get(quality, 99999)
            vid_fmt = sorted(
                [f for f in formats if 'video' in f.get('type', '')
                 and f.get('qualityLabel', '').rstrip('p').isdigit()
                 and int(f['qualityLabel'].rstrip('p')) <= max_h],
                key=lambda f: int(f.get('qualityLabel', '0p').rstrip('p')), reverse=True)
            if not vid_fmt or not vid_fmt[0].get('url'):
                return False
            stream_url = vid_fmt[0]['url']
        else:
            aud_fmt = sorted(
                [f for f in formats if 'audio' in f.get('type', '') and f.get('url')],
                key=lambda f: f.get('bitrate', 0), reverse=True)
            if not aud_fmt:
                return False
            stream_url = aud_fmt[0]['url']

        _set_job(job_id, {'progress': 10})
        if fmt == 'mp3':
            # Single-pass: ffmpeg downloads + converts simultaneously
            if not _ffmpeg_stream_convert(job_id, stream_url, out, quality):
                return False
        else:
            _download_stream(job_id, stream_url, out, 10, 85)

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        fname = make_filename(title or data.get('title', 'video'),
                              uploader or data.get('author', ''), ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True
    except Exception:
        return False


def cobalt_download(job_id, url, title, uploader, quality, fmt):
    try:
        _set_job(job_id, {'progress': 3})
        body = json.dumps({
            'url': url,
            'audioFormat': 'mp3' if fmt != 'mp4' else 'mp4',
            'filenameStyle': 'basic',
            'quality': '1080' if fmt == 'mp4' else '320',
        }).encode()
        req = urllib.request.Request(
            'https://api.cobalt.tools/',
            data=body,
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        status = data.get('status')
        stream_url = None
        if status in ('stream', 'tunnel', 'redirect'):
            stream_url = data.get('url')
        elif status == 'picker':
            items = data.get('picker', [])
            if items:
                stream_url = items[0].get('url')

        if not stream_url:
            return False

        file_id = str(uuid.uuid4())
        ext = 'mp4' if fmt == 'mp4' else 'mp3'
        out = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')

        if fmt == 'mp3':
            raw = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.m4a')
            _download_stream(job_id, stream_url, raw, 10, 80)
            if not os.path.exists(raw) or os.path.getsize(raw) < 1024:
                return False
            if not _ffmpeg_to_mp3(raw, out, quality):
                return False
            try: os.remove(raw)
            except: pass
        else:
            _download_stream(job_id, stream_url, out, 10, 85)

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        fname = make_filename(title or 'video', uploader or '', ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True
    except Exception:
        return False


def pytube_download(job_id, url, title, uploader, quality, fmt):
    if not _PYTUBE_OK:
        return False
    yt = None
    # Try multiple clients × (direct + 2 proxies). YouTube bot-blocks the
    # Railway datacenter IP, so direct extraction usually fails — rotating
    # through residential proxies is what makes pytubefix usable here.
    clients = ['WEB', 'ANDROID_VR', 'MWEB', 'TV_EMBED', 'IOS']
    proxy_attempts = [None, _proxy_rotator.get(), _proxy_rotator.get()]
    for proxy in proxy_attempts:
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        for client in clients:
            try:
                _yt = PyTube(url, client=client, proxies=proxies)
                _ = _yt.streams   # trigger extraction
                yt = _yt
                break
            except Exception:
                continue
        if yt is not None:
            break
    if yt is None:
        return False

    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'
    try:
        _set_job(job_id, {'progress': 5, 'last_progress_at': time.time()})
        if fmt == 'mp4':
            max_h = {'720': 720, '1080': 1080, '4k': 2160}.get(quality, 99999)
            out = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp4')

            # Modern YouTube: progressive streams cap at 720p and often missing.
            # Try progressive first (single file, fastest), else fall back to
            # adaptive video-only + audio-only and merge with ffmpeg.
            prog = [s for s in yt.streams.filter(progressive=True, file_extension='mp4')
                    if (s.resolution and int(s.resolution.rstrip('p')) <= max_h)]
            prog.sort(key=lambda s: int(s.resolution.rstrip('p')), reverse=True)
            if prog:
                # Use _download_stream so we get progress + a real read timeout
                # (pytube's stream.download() has neither, which caused the 10% freeze).
                _download_stream(job_id, prog[0].url, out, 10, 90)
            else:
                v_streams = [s for s in yt.streams.filter(adaptive=True, file_extension='mp4', only_video=True)
                             if (s.resolution and int(s.resolution.rstrip('p')) <= max_h)]
                v_streams.sort(key=lambda s: int(s.resolution.rstrip('p')), reverse=True)
                a_stream  = yt.streams.filter(only_audio=True, file_extension='mp4').order_by('abr').last() \
                            or yt.streams.filter(only_audio=True).order_by('abr').last()
                if not v_streams or not a_stream:
                    return False
                v_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_v.mp4')
                a_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_a.{a_stream.subtype or "m4a"}')
                _download_stream(job_id, v_streams[0].url, v_tmp, 15, 55)
                _download_stream(job_id, a_stream.url, a_tmp, 55, 80)
                _set_job(job_id, {'progress': 85})
                if not _ffmpeg_merge(v_tmp, a_tmp, out):
                    return False
                for f in (v_tmp, a_tmp):
                    try: os.remove(f)
                    except: pass
        else:
            stream = yt.streams.filter(only_audio=True).order_by('abr').last()
            if not stream:
                return False
            raw_ext  = stream.mime_type.split('/')[-1]
            raw_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.{raw_ext}')
            out      = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            # _download_stream gives progress updates + 30s read timeout
            # (pytube's stream.download() does neither — this was the 10% freeze).
            _download_stream(job_id, stream.url, raw_path, 10, 70)
            if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 1024:
                return False
            _set_job(job_id, {'progress': 75})
            if not _ffmpeg_to_mp3(raw_path, out, quality):
                return False
            try: os.remove(raw_path)
            except: pass

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        fname = make_filename(title or yt.title or 'download',
                              uploader or yt.author or '', ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True
    except Exception:
        return False


# ── yt-dlp backend ────────────────────────────────────────────────────────────

def build_cmd(url, output_template, quality='320K', fmt='mp3', proxy=None, attempt=0):
    bgutil_up = _bgutil_ready

    if bgutil_up:
        # With bgutil: web client + PO token fetching
        client_sets = ['web', 'web,mweb', 'mweb,web', 'web_creator', 'web,web_creator']
    else:
        # No bgutil: try various clients
        client_sets = ['mweb', 'mweb,android', 'android,mweb', 'mweb', 'android']

    clients = client_sets[min(attempt, len(client_sets) - 1)]

    ea_parts = [f'player_client={clients}']
    if bgutil_up:
        ea_parts.append('fetch_pot=always')

    base_flags = [
        '--no-playlist', '--newline', '--geo-bypass', '--no-part',
        '--extractor-args', f'youtube:{";".join(ea_parts)}',
        '--socket-timeout', '15',
        '--retries', '2',
    ]
    if bgutil_up:
        base_flags += ['--extractor-args',
                       f'youtubepot-bgutilhttp:base_url={BGUTIL_BASE_URL}']
    if proxy:
        base_flags += _proxy_args(proxy)

    if fmt == 'mp4':
        q = quality or 'best'
        if q == '720':
            fmt_str = 'best[height<=720][ext=mp4]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
        elif q == '1080':
            fmt_str = 'best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
        elif q == '4k':
            fmt_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'
        else:
            fmt_str = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4'] + base_flags
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3',
               '--audio-quality', quality or '320K'] + base_flags

    d = _find_ffmpeg_dir()
    if d:
        cmd += ['--ffmpeg-location', d]
    cmd += ['-o', output_template, url]
    return cmd

_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

def _run_ytdlp(cmd, job_id):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stderr_lines = []

    def _upd(line):
        m = _PROGRESS_RE.search(line)
        if m:
            pct = min(int(float(m.group(1))), 90)
            with jobs_lock:
                if jobs.get(job_id, {}).get('status') == 'processing':
                    jobs[job_id]['progress'] = pct

    def _rd_err():
        for line in proc.stderr:
            stderr_lines.append(line); _upd(line)
    def _rd_out():
        for line in proc.stdout:
            _upd(line)

    te = threading.Thread(target=_rd_err, daemon=True)
    to = threading.Thread(target=_rd_out, daemon=True)
    te.start(); to.start()
    try:
        proc.wait(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1, 'timeout'
    te.join(timeout=5); to.join(timeout=5)
    return proc.returncode, ''.join(stderr_lines)


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT:
            return False
        _rate_store[ip].append(now)
        return True

def _client_ip():
    return (request.headers.get('X-Forwarded-For', '')
            .split(',')[0].strip() or request.remote_addr or 'unknown')


# ── Main worker: y2mate.nu only ──────────────────────────────────────────────

def do_convert(job_id, url, prefetched_title=None, prefetched_uploader=None,
               quality='320K', fmt='mp3'):
    _set_job(job_id, {'status': 'processing', 'progress': 2})

    try:
        _log_proxy_event('y2mate', 'trying', f'New job → {fmt.upper()} {quality}')
        if y2mate_download(job_id, url, prefetched_title, prefetched_uploader, quality, fmt):
            _log_proxy_event('y2mate', 'success', 'y2mate.nu — done ✓')
            return
        _log_proxy_event('y2mate', 'error', 'y2mate failed (see preceding error log)')
        _set_job(job_id, {'status': 'error',
                          'error': 'Conversion failed. y2mate is blocking us — try again in a moment.'})
    except Exception as ex:
        _log_proxy_event('y2mate', 'error', f'Exception: {ex}')
        _set_job(job_id, {'status': 'error', 'error': 'Conversion failed. Please try again.'})
    finally:
        with url_jobs_lock:
            url_jobs.pop(f'{url}|{fmt}|{quality}', None)


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def _sec(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def _404(e):
    return jsonify({'error': 'Not found'}), 404

@app.route('/ping')
def ping():
    return 'pong', 200

@app.route('/proxy-status')
def proxy_status():
    with _proxy_rotator._lock:
        active = len(_proxy_rotator._pool)
        total  = len(_proxy_rotator._all)
        idx    = _proxy_rotator._idx
        pool   = list(_proxy_rotator._pool)
    next_proxy = pool[idx % len(pool)] if pool else None
    next_user, next_country, _ = _proxy_label(next_proxy) if next_proxy else ('—','—','—')
    with _proxy_log_lock:
        log  = list(_proxy_log[:100])
        rots = _proxy_rotations
    return jsonify({
        'active': active, 'total': total,
        'rotations': rots,
        'next': next_user, 'next_country': next_country,
        'log': log,
    })

_CONSOLE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Proxy Console — YT MP3</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07070f;color:#eeeeff;font-family:'Courier New',monospace;font-size:13px;min-height:100vh}
header{background:#0e0e1c;border-bottom:1px solid #252548;padding:14px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:.95rem;font-weight:900;letter-spacing:.05em;color:#8b5cf6}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin-left:auto}
.stat{display:flex;flex-direction:column;align-items:center;gap:2px}
.stat-val{font-size:1.2rem;font-weight:900;color:#10b981}
.stat-val.red{color:#ef4444}.stat-val.blue{color:#3b82f6}.stat-val.gold{color:#f59e0b}
.stat-lbl{font-size:.6rem;color:#60608a;letter-spacing:.08em;text-transform:uppercase}
.toolbar{background:#0e0e1c;border-bottom:1px solid #14142a;padding:8px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.next-lbl{font-size:.68rem;color:#60608a;text-transform:uppercase;letter-spacing:.1em}
.next-country{color:#7db8fb;font-size:.82rem;font-weight:700}
.next-val{color:#b39dfd;font-weight:700;font-size:.88rem}
.btn{padding:6px 14px;border-radius:6px;border:none;font-family:inherit;font-size:.75rem;font-weight:700;cursor:pointer;letter-spacing:.04em}
.btn-rotate{background:rgba(59,130,246,.18);color:#3b82f6;border:1px solid rgba(59,130,246,.35)}
.btn-rotate:hover{background:rgba(59,130,246,.3)}
.btn-clear{background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.25)}
.btn-clear:hover{background:rgba(239,68,68,.22)}
.btn-test{background:rgba(16,185,129,.14);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.btn-test:hover{background:rgba(16,185,129,.26)}
.auto-badge{font-size:.68rem;color:#10b981;margin-left:auto;display:flex;align-items:center;gap:5px}
.log-header{padding:8px 20px;font-size:.6rem;color:#60608a;text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid #14142a;display:grid;grid-template-columns:65px 150px 140px 75px 1fr;gap:8px}
.log-body{overflow-y:auto;max-height:calc(100vh - 185px)}
.row{display:grid;grid-template-columns:65px 150px 140px 75px 1fr;gap:8px;padding:6px 20px;border-bottom:1px solid #0a0a18;align-items:center;animation:fadeIn .4s ease}
@keyframes fadeIn{from{opacity:0;background:#1c1c38}to{opacity:1;background:transparent}}
.row:hover{background:#0e0e1c}
.t{color:#404060;font-size:.78rem}
.c{color:#eeeeff;font-size:.82rem}
.u{color:#b39dfd;font-size:.78rem}
.d{color:#505070;font-size:.76rem;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.62rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.badge.success{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.badge.trying{background:rgba(139,92,246,.12);color:#a78bfa;border:1px solid rgba(139,92,246,.25)}
.badge.blocked{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.25)}
.badge.rotated{background:rgba(59,130,246,.12);color:#60a5fa;border:1px solid rgba(59,130,246,.25)}
.badge.error{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.25)}
.badge.timeout{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
.badge.job_start{background:rgba(16,185,129,.08);color:#6ee7b7;border:1px solid rgba(16,185,129,.18)}
.empty{padding:48px;text-align:center;color:#404060;font-size:.85rem}
.dot{width:7px;height:7px;border-radius:50%;background:#10b981;display:inline-block;animation:blink 2s ease-in-out infinite;margin-right:6px;vertical-align:middle}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>▶ YT MP3 — Live Proxy Console</h1>
  <div class="stats">
    <div class="stat"><span class="stat-val" id="sActive">—</span><span class="stat-lbl">Active IPs</span></div>
    <div class="stat"><span class="stat-val blue" id="sTotal">—</span><span class="stat-lbl">Pool Size</span></div>
    <div class="stat"><span class="stat-val red" id="sFailed">—</span><span class="stat-lbl">Failed</span></div>
    <div class="stat"><span class="stat-val gold" id="sRot">—</span><span class="stat-lbl">Rotations</span></div>
  </div>
</header>
<div class="toolbar">
  <span class="next-lbl">Next IP →</span>
  <span class="next-country" id="nCountry">—</span>
  <span class="next-val" id="nUser">—</span>
  <button class="btn btn-rotate" onclick="forceRotate()">⟳ Force Rotate</button>
  <button class="btn btn-clear" onclick="clearLog()">✕ Clear Log</button>
  <span class="auto-badge"><span class="dot" style="width:5px;height:5px;margin:0"></span> Auto-refresh 2s</span>
</div>
<div class="log-header">
  <span>Time</span><span>Country</span><span>Username</span><span>Status</span><span>Detail</span>
</div>
<div class="log-body" id="logBody">
  <div class="empty">⏳ Waiting for download events — try converting a video on the main page</div>
</div>
<script>
let _lastLog=[];
async function refresh(){
  try{
    const r=await fetch('/proxy-status');
    const d=await r.json();
    document.getElementById('sActive').textContent=d.active;
    document.getElementById('sTotal').textContent=d.total;
    document.getElementById('sFailed').textContent=d.total-d.active;
    document.getElementById('sRot').textContent=d.rotations;
    document.getElementById('nCountry').textContent=d.next_country||'—';
    document.getElementById('nUser').textContent=d.next||'—';
    if(!d.log||!d.log.length)return;
    if(JSON.stringify(d.log[0])===JSON.stringify(_lastLog[0]))return;
    _lastLog=d.log;
    document.getElementById('logBody').innerHTML=d.log.map(e=>`
      <div class="row">
        <span class="t">${e.time}</span>
        <span class="c">${e.country}</span>
        <span class="u">${e.user}</span>
        <span><span class="badge ${e.result}">${e.result}</span></span>
        <span class="d" title="${e.detail}">${e.detail}</span>
      </div>`).join('');
  }catch(err){}
}
async function forceRotate(){
  await fetch('/proxy-rotate',{method:'POST'});
  await refresh();
}
function clearLog(){
  fetch('/proxy-clear',{method:'POST'}).then(refresh);
}
refresh();
setInterval(refresh,2000);
</script>
</body>
</html>'''

@app.route('/console')
def console():
    return _CONSOLE_HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/proxy-rotate', methods=['POST'])
def proxy_rotate():
    p = _proxy_rotator.get()
    _log_proxy_event(p, 'rotated', 'Manual rotate via console')
    return jsonify({'ok': True})

@app.route('/proxy-clear', methods=['POST'])
def proxy_clear():
    with _proxy_log_lock:
        _proxy_log.clear()
    return jsonify({'ok': True})

_BUILD_ID = None
def _build_id():
    """Short identifier visible in the footer so users + dev can tell which
    deploy a browser is actually rendering (cache-sanity check)."""
    global _BUILD_ID
    if _BUILD_ID:
        return _BUILD_ID
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                             capture_output=True, text=True, timeout=2,
                             cwd=os.path.dirname(os.path.dirname(_HERE)) or _HERE)
        _BUILD_ID = (out.stdout.strip() or str(int(time.time())))[:8]
    except Exception:
        _BUILD_ID = str(int(time.time()))[-7:]
    return _BUILD_ID

@app.route('/')
def index():
    try:
        resp = app.make_response(render_template('index.html', build=_build_id()))
        # Force every browser to fetch a fresh page — old Chrome desktop
        # caches were serving stale JS that broke polling after our deploys.
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma']        = 'no-cache'
        resp.headers['Expires']       = '0'
        return resp
    except Exception as exc:
        return f'template error: {exc}', 500

_HEALTH_CACHE      = {'ts': 0, 'data': None}
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_TTL        = 30

@app.route('/health')
def health():
    now = time.time()
    with _HEALTH_CACHE_LOCK:
        if _HEALTH_CACHE['data'] and now - _HEALTH_CACHE['ts'] < _HEALTH_TTL:
            return jsonify(_HEALTH_CACHE['data'])
    with _sources_lock:
        piped = list(_working_piped)
        inv   = list(_working_invidious)
    with _proxy_rotator._lock:
        active_proxies = len(_proxy_rotator._pool)
    ytdlp_ver = ''
    try:
        r = subprocess.run([YTDLP, '--version'], capture_output=True, timeout=5)
        ytdlp_ver = (r.stdout.strip() if isinstance(r.stdout, str) else r.stdout.decode().strip())
    except Exception:
        pass
    # Verify bgutil yt-dlp plugin is importable + server is reachable
    try:
        from yt_dlp_plugins.extractor import getpot_bgutil_http  # noqa
        bgutil_plugin_loaded = True
    except Exception:
        bgutil_plugin_loaded = False
    bgutil_server_alive = False
    try:
        with urllib.request.urlopen(f'{BGUTIL_BASE_URL}/ping', timeout=2) as r:
            bgutil_server_alive = r.getcode() == 200
    except Exception:
        pass
    payload = {
        'status':              'ok',
        'yt_dlp_version':      ytdlp_ver,
        'pytubefix':           _PYTUBE_OK,
        'working_piped':       piped,
        'working_invidious':   inv,
        'active_proxies':      active_proxies,
        'total_proxies':       len(_PROXY_LIST),
        'last_probe_ago':      int(time.time() - _last_probe) if _last_probe else None,
        'bgutil_server':       bgutil_server_alive,
        'bgutil_plugin':       bgutil_plugin_loaded,
    }
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE['ts']   = now
        _HEALTH_CACHE['data'] = payload
    return jsonify(payload)

_TEST_VIDEO = 'dQw4w9WgXcQ'

@app.route('/test')
def test_backends():
    """Race all 5 backends in parallel — returns in ~time of slowest, not sum."""
    def _piped():
        try:
            pd = piped_get_streams(_TEST_VIDEO)
            return 'ok' if pd and not pd.get('error') and pd.get('audioStreams') else 'fail'
        except Exception as e:
            return f'error: {e}'

    def _invidious():
        try:
            iv = invidious_get_streams(_TEST_VIDEO)
            return 'ok' if iv and iv.get('adaptiveFormats') else 'fail'
        except Exception as e:
            return f'error: {e}'

    def _ytdlp():
        try:
            proxy = _proxy_rotator.get()
            r = subprocess.run(
                [YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
                 '--socket-timeout', '10', '--retries', '1',
                 '--extractor-args', 'youtube:player_client=mweb',
                 f'https://www.youtube.com/watch?v={_TEST_VIDEO}']
                + _proxy_args(proxy),
                capture_output=True, text=True, timeout=15)
            return 'ok' if r.returncode == 0 else f'fail: {parse_ytdlp_error(r.stderr)}'
        except Exception as e:
            return f'error: {e}'

    def _pytubefix():
        if not _PYTUBE_OK:
            return 'not installed'
        try:
            yt = PyTube(f'https://www.youtube.com/watch?v={_TEST_VIDEO}', client='WEB')
            _ = yt.streams
            return 'ok'
        except Exception as e:
            return f'fail: {e}'

    def _cobalt():
        try:
            body = json.dumps({'url': f'https://www.youtube.com/watch?v={_TEST_VIDEO}',
                               'audioFormat': 'mp3', 'filenameStyle': 'basic'}).encode()
            req = urllib.request.Request('https://api.cobalt.tools/', data=body,
                headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                         'User-Agent': 'Mozilla/5.0'}, method='POST')
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            return 'ok' if d.get('status') in ('stream','tunnel','redirect','picker') else f"fail: {d.get('status')}"
        except Exception as e:
            return f'error: {e}'

    fns = {'piped': _piped, 'invidious': _invidious, 'ytdlp': _ytdlp,
           'pytubefix': _pytubefix, 'cobalt': _cobalt}
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(fn): name for name, fn in fns.items()}
        for fut in as_completed(futs, timeout=20):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = f'error: {e}'
    for name in fns:
        results.setdefault(name, 'timeout')

    ok_count = sum(1 for v in results.values() if v == 'ok')
    return jsonify({'backends': results, 'ok': ok_count, 'total': len(results)})

@app.route('/manifest.json')
def manifest():
    return jsonify({
        'name': 'YT MP3 Converter', 'short_name': 'YT MP3',
        'description': 'Convert YouTube videos to MP3 or MP4',
        'start_url': '/', 'display': 'standalone',
        'background_color': '#0b0b0f', 'theme_color': '#7c5cfc', 'icons': []
    })

@app.route('/sw.js')
def sw():
    js = ("self.addEventListener('install',e=>{e.waitUntil(caches.keys()"
          ".then(ks=>Promise.all(ks.map(k=>caches.delete(k)))"
          ".then(()=>self.skipWaiting()))});\n"
          "self.addEventListener('activate',e=>{e.waitUntil(caches.keys()"
          ".then(ks=>Promise.all(ks.map(k=>caches.delete(k))))"
          ".then(()=>clients.claim())"
          ".then(()=>self.registration.unregister()))});\n")
    return js, 200, {'Content-Type': 'application/javascript', 'Cache-Control': 'no-store'}

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/ads.txt')
def ads_txt():
    return 'google.com, pub-3956390078338144, DIRECT, f08c47fec0942fa0\n', 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{host}/</loc><changefreq>monthly</changefreq>'
            f'<priority>1.0</priority></url></urlset>',
            200, {'Content-Type': 'application/xml'})

def _yt_info_cmd(url, proxy=None):
    clients = 'mweb,tv_embedded,web_creator,android,web'
    ea = [f'player_client={clients}']
    if _bgutil_ready:
        ea.append('fetch_pot=always')
    cmd = [YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
           '--socket-timeout', '15', '--retries', '2',
           '--extractor-args', f'youtube:{";".join(ea)}']
    if _bgutil_ready:
        cmd += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={BGUTIL_BASE_URL}']
    cmd += [url] + _proxy_args(proxy)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=45)

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL — please check the link.'}), 400
    if is_playlist_only(url):
        return jsonify({'error': "That's a playlist URL. Please paste a single video link."}), 400

    video_id = _extract_video_id(url)

    # Race 3 fast sources in parallel — first winner wins.
    # Piped, oEmbed, Invidious all return in <2s usually. No more sequential waiting.
    if video_id:
        def _piped_info():
            try:
                pd = piped_get_streams(video_id)
                if pd and not pd.get('error') and pd.get('title'):
                    dur = int(pd.get('duration', 0))
                    return {
                        'title': pd.get('title', 'Unknown Title'),
                        'thumbnail': pd.get('thumbnailUrl', '') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                        'duration_sec': dur,
                        'uploader': pd.get('uploader', ''),
                    }
            except Exception:
                pass
            return None

        def _oembed_combo():
            oe = _oembed_info(video_id)
            if not oe:
                return None
            dur = _yt_duration_from_page(video_id) or 0
            return {
                'title': oe.get('title', 'Unknown Title'),
                'thumbnail': oe.get('thumbnail_url') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                'duration_sec': dur,
                'uploader': oe.get('author_name', ''),
            }

        def _invidious_info():
            try:
                iv = invidious_get_streams(video_id)
                if iv and iv.get('title'):
                    dur = int(iv.get('lengthSeconds', 0))
                    thumb = next((t['url'] for t in iv.get('videoThumbnails', [])
                                  if t.get('quality') in ('maxresdefault', 'sddefault', 'high')), '')
                    return {
                        'title': iv.get('title', 'Unknown Title'),
                        'thumbnail': thumb or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                        'duration_sec': dur,
                        'uploader': iv.get('author', ''),
                    }
            except Exception:
                pass
            return None

        def _pytube_info():
            if not _PYTUBE_OK:
                return None
            for client in ('WEB', 'ANDROID_VR', 'MWEB'):
                try:
                    yt = PyTube(url, client=client)
                    title = yt.title
                    if not title:
                        continue
                    return {
                        'title': title,
                        'thumbnail': yt.thumbnail_url or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                        'duration_sec': int(yt.length or 0),
                        'uploader': yt.author or '',
                    }
                except Exception:
                    continue
            return None

        # Race all 4 sources. Prefer first result with both title AND duration > 0.
        # Fall back to first result with title only if nothing has duration in time.
        winner_with_dur = None
        winner_no_dur   = None
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(fn) for fn in (_piped_info, _oembed_combo, _invidious_info, _pytube_info)]
            try:
                for fut in as_completed(futures, timeout=12):
                    try:
                        res = fut.result()
                    except Exception:
                        continue
                    if not res or not res.get('title'):
                        continue
                    if res.get('duration_sec'):
                        winner_with_dur = res
                        break
                    if not winner_no_dur:
                        winner_no_dur = res
            except Exception:
                pass
        winner = winner_with_dur or winner_no_dur
        if winner:
            dur  = int(winner.get('duration_sec') or 0)
            m, s = divmod(dur, 60)
            return jsonify({
                'title':        winner['title'],
                'thumbnail':    winner['thumbnail'],
                'duration':     f'{m}:{s:02d}' if dur else '?:??',
                'duration_sec': dur,
                'uploader':     winner.get('uploader', ''),
                'url':          url,
            })

    # Last resort — yt-dlp with proxy rotation
    try:
        result, last_err = None, '__BOT__'
        _tried = set()
        for _ in range(min(len(_PROXY_LIST), 4)):
            proxy = _proxy_rotator.get()
            result   = _yt_info_cmd(url, proxy)
            if result.returncode == 0:
                break
            last_err = parse_ytdlp_error(result.stderr)
            if last_err != '__BOT__':
                break
            if proxy not in _tried:
                _tried.add(proxy)
                _proxy_rotator.mark_failed(proxy)
                _proxy_rotator.rotate()
        if result and result.returncode == 0:
            info     = json.loads(result.stdout)
            duration = info.get('duration', 0)
            m, s     = divmod(int(duration), 60)
            return jsonify({
                'title': info.get('title', 'Unknown Title'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': f'{m}:{s:02d}', 'duration_sec': int(duration),
                'uploader': info.get('uploader', '') or info.get('channel', ''),
                'url': url,
            })
        err = 'Video unavailable. Please try again.' if last_err == '__BOT__' else last_err
        return jsonify({'error': err}), 400
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out. Please try again.'}), 504
    except Exception:
        return jsonify({'error': 'Failed to fetch video info. Please try again.'}), 500

@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data     = request.get_json() or {}
    url      = normalize_url(data.get('url', '').strip())
    title    = data.get('title', '').strip()
    uploader = data.get('uploader', '').strip()
    quality  = data.get('quality', '320K') or '320K'
    fmt      = data.get('format', 'mp3')
    if fmt not in ('mp3', 'mp4'):
        fmt = 'mp3'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    if is_playlist_only(url):
        return jsonify({'error': 'Please paste a single video URL, not a playlist.'}), 400

    dedup_key = f'{url}|{fmt}|{quality}'
    with url_jobs_lock:
        existing = url_jobs.get(dedup_key)
        if existing:
            with jobs_lock:
                st = jobs.get(existing, {}).get('status')
            if st in ('pending', 'processing'):
                return jsonify({'job_id': existing})

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                        'error': None, 'progress': 0}
        _save_job(job_id, jobs[job_id])
    with url_jobs_lock:
        url_jobs[dedup_key] = job_id

    threading.Thread(
        target=do_convert,
        args=(job_id, url, title or None, uploader or None, quality, fmt),
        daemon=True
    ).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})

@app.route('/download/<job_id>')
@app.route('/download/<job_id>/<path:_fname>')
def download_file(job_id, _fname=None):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please convert again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'audio.mp3'
    mime = 'video/mp4' if safe.endswith('.mp4') else 'audio/mpeg'
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 13000))
    app.run(host='0.0.0.0', port=port)
