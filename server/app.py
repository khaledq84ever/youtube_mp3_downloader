from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil
import urllib.parse, urllib.request
from collections import defaultdict

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

DOWNLOAD_DIR  = '/tmp/ytdl_cache'
YTDLP         = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL      = 1800          # 30 min
JOB_TIMEOUT   = 120           # 2 min per yt-dlp attempt
RATE_LIMIT    = 10            # per minute per IP
COOKIES_FILE  = '/tmp/yt_cookies.txt'
PROXY_URL     = os.environ.get('PROXY_URL', '')

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

_yt_cookies_env = os.environ.get('YOUTUBE_COOKIES', '')
if _yt_cookies_env:
    with open(COOKIES_FILE, 'w') as _f:
        _f.write(_yt_cookies_env)

def _cookies_args():
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
        return ['--cookies', COOKIES_FILE]
    return []

def _proxy_args():
    return ['--proxy', PROXY_URL] if PROXY_URL else []

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
    if '402' in err or 'payment required' in err or 'po token' in err:
        return '__BOT__'
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
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.darkness.services',
    'https://piped-api.privacy.com.de',
    'https://api.piped.privacydev.net',
    'https://pipedapi.ngn.tf',
    'https://pipedapi.tokhmi.xyz',
    'https://pipedapi.moomoo.me',
    'https://watchapi.whatever.social',
    'https://api.piped.projectsegfau.lt',
]

_ALL_INVIDIOUS = [
    'https://invidious.io.lol',
    'https://yewtu.be',
    'https://inv.nadeko.net',
    'https://invidious.fdn.fr',
    'https://invidious.nerdvpn.de',
    'https://invidious.privacydev.net',
    'https://iv.datura.network',
    'https://invidious.perennialte.ch',
    'https://invidious.lunar.icu',
    'https://iv.melmac.space',
    'https://invidious.projectsegfau.lt',
    'https://inv.tux.pizza',
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


# ── Piped helpers ─────────────────────────────────────────────────────────────

def piped_get_streams(video_id):
    _ensure_sources_fresh()
    with _sources_lock:
        instances = list(_working_piped) or _ALL_PIPED[:5]
    for inst in instances:
        try:
            req = urllib.request.Request(
                f'{inst}/streams/{video_id}',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=12) as r:
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
        instances = list(_working_invidious) or _ALL_INVIDIOUS[:5]
    for inst in instances:
        try:
            req = urllib.request.Request(
                f'{inst}/api/v1/videos/{video_id}?fields=title,author,lengthSeconds,adaptiveFormats,videoThumbnails',
                headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=12) as r:
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

def _download_stream(job_id, stream_url, out_path, progress_start=10, progress_end=85):
    req = urllib.request.Request(stream_url,
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.youtube.com/'})
    total, done = 0, 0
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get('Content-Length', 0))
        with open(out_path, 'wb') as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(progress_start + int(done / total * (progress_end - progress_start)),
                              progress_end)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

def _ffmpeg_to_mp3(src, dst, quality):
    ffmpeg = shutil.which('ffmpeg') or 'ffmpeg'
    d = _find_ffmpeg_dir()
    if d:
        ffmpeg = os.path.join(d, 'ffmpeg')
    kbps = (quality or '320K').rstrip('Kk')
    res = subprocess.run(
        [ffmpeg, '-i', src, '-vn', '-ar', '44100', '-ac', '2',
         '-b:a', f'{kbps}k', dst, '-y'],
        capture_output=True, timeout=300)
    return res.returncode == 0 and os.path.exists(dst)

def _ffmpeg_merge(v_src, a_src, dst):
    ffmpeg = shutil.which('ffmpeg') or 'ffmpeg'
    d = _find_ffmpeg_dir()
    if d:
        ffmpeg = os.path.join(d, 'ffmpeg')
    res = subprocess.run(
        [ffmpeg, '-i', v_src, '-i', a_src,
         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
         '-movflags', '+faststart', dst, '-y'],
        capture_output=True, timeout=300)
    return res.returncode == 0 and os.path.exists(dst)


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
            raw = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.m4a')
            out = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            _download_stream(job_id, astream['url'], raw, 10, 80)
            if not _ffmpeg_to_mp3(raw, out, quality):
                return False
            try: os.remove(raw)
            except: pass

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

        raw = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.m4a') if fmt == 'mp3' else out
        _set_job(job_id, {'progress': 10})
        _download_stream(job_id, stream_url, raw, 10, 80)

        if fmt == 'mp3':
            if not _ffmpeg_to_mp3(raw, out, quality):
                return False
            try: os.remove(raw)
            except: pass

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        fname = make_filename(title or data.get('title', 'video'),
                              uploader or data.get('author', ''), ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': fname, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True
    except Exception:
        return False


def pytube_download(job_id, url, title, uploader, quality, fmt):
    if not _PYTUBE_OK:
        return False
    yt = None
    for client in ['WEB', 'ANDROID_VR', 'MWEB', 'TV_EMBED', 'IOS']:
        try:
            _yt = PyTube(url, client=client)
            _ = _yt.streams   # trigger extraction
            yt = _yt
            break
        except Exception:
            continue
    if yt is None:
        return False

    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'
    try:
        _set_job(job_id, {'progress': 5})
        if fmt == 'mp4':
            stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').last()
            if not stream:
                stream = yt.streams.filter(file_extension='mp4').order_by('resolution').last()
            if not stream:
                return False
            out = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp4')
            _set_job(job_id, {'progress': 10})
            stream.download(output_path=DOWNLOAD_DIR, filename=f'{file_id}.mp4')
        else:
            stream = yt.streams.filter(only_audio=True).order_by('abr').last()
            if not stream:
                return False
            raw_ext  = stream.mime_type.split('/')[-1]
            raw_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.{raw_ext}')
            out      = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            _set_job(job_id, {'progress': 10})
            stream.download(output_path=DOWNLOAD_DIR, filename=f'{file_id}_raw.{raw_ext}')
            _set_job(job_id, {'progress': 75})
            if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 1024:
                return False
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

def build_cmd(url, output_template, quality='320K', fmt='mp3'):
    clients = 'tv_embedded,android_vr,android,ios,web'
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
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4',
               '--no-playlist', '--newline', '--geo-bypass', '--no-part',
               '--extractor-args', f'youtube:player_client={clients}',
               '--js-runtimes', 'node'] + _proxy_args() + _cookies_args()
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3',
               '--audio-quality', quality or '320K',
               '--no-playlist', '--newline', '--geo-bypass', '--no-part',
               '--extractor-args', f'youtube:player_client={clients}',
               '--js-runtimes', 'node'] + _proxy_args() + _cookies_args()
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


# ── Main worker: Piped → PyTubefix → yt-dlp → Invidious ─────────────────────

def do_convert(job_id, url, prefetched_title=None, prefetched_uploader=None,
               quality='320K', fmt='mp3'):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    video_id = _extract_video_id(url)

    try:
        # 1. Piped (fastest, no bot detection)
        if video_id:
            if piped_download(job_id, video_id, url,
                              prefetched_title, prefetched_uploader, quality, fmt):
                return

        # 2. PyTubefix (different extraction path)
        _set_job(job_id, {'progress': 0})
        if pytube_download(job_id, url, prefetched_title, prefetched_uploader, quality, fmt):
            return

        # 3. yt-dlp (retries twice with different clients)
        file_id  = str(uuid.uuid4())
        tmpl     = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
        rc, serr = -1, ''
        for _ in range(2):
            cmd    = build_cmd(url, tmpl, quality, fmt)
            rc, serr = _run_ytdlp(cmd, job_id)
            if rc == 0:
                break
            if serr == 'timeout':
                _set_job(job_id, {'status': 'error',
                                  'error': 'Download timed out. The video may be too long.'})
                return
            if parse_ytdlp_error(serr) != '__BOT__':
                break
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*')):
                try: os.remove(f)
                except: pass

        if rc == 0:
            ext    = 'mp4' if fmt == 'mp4' else 'mp3'
            target = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')
            if not os.path.exists(target):
                audio_exts = {'.webm', '.m4a', '.ogg', '.opus', '.aac', '.mp4'}
                cands = [f for f in glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
                         if os.path.splitext(f)[1].lower() in audio_exts]
                if not cands:
                    _set_job(job_id, {'status': 'error',
                                      'error': 'Output file not found. Please try again.'})
                    return
                src = cands[0]
                if not _ffmpeg_to_mp3(src, target, quality):
                    _set_job(job_id, {'status': 'error',
                                      'error': 'Conversion failed. Please try again.'})
                    return
                try: os.remove(src)
                except: pass
            if os.path.getsize(target) < 1024:
                _set_job(job_id, {'status': 'error',
                                  'error': 'Output file is empty. Please try again.'})
                return
            fname = make_filename(prefetched_title or 'download',
                                  prefetched_uploader or '', ext)
            _set_job(job_id, {'status': 'done', 'file': target,
                              'filename': fname, 'progress': 100})
            schedule_cleanup(job_id, target)
            return

        # 4. Invidious last resort
        if video_id and invidious_download(job_id, video_id, url,
                                           prefetched_title, prefetched_uploader, quality, fmt):
            return

        err = parse_ytdlp_error(serr)
        _set_job(job_id, {'status': 'error',
                           'error': err if err != '__BOT__' else
                           'Video unavailable on this server. Try again — sources rotate automatically.'})
    except Exception:
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
    routes = sorted(str(r) for r in app.url_map.iter_rules())
    tdir = app.template_folder
    import os as _os
    tmpl_exists = _os.path.exists(_os.path.join(tdir, 'index.html')) if tdir else False
    return (f'404 debug | template_folder={tdir} | index.html={tmpl_exists} | '
            f'routes={routes}'), 404

@app.route('/ping')
def ping():
    return 'pong', 200

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as exc:
        return f'template error: {exc}', 500

@app.route('/health')
def health():
    with _sources_lock:
        piped = list(_working_piped)
        inv   = list(_working_invidious)
    ytdlp_ver = ''
    try:
        r = subprocess.run([YTDLP, '--version'], capture_output=True, timeout=5)
        ytdlp_ver = (r.stdout.strip() if isinstance(r.stdout, str) else r.stdout.decode().strip())
    except Exception:
        pass
    return jsonify({
        'status':            'ok',
        'yt_dlp_version':    ytdlp_ver,
        'pytubefix':         _PYTUBE_OK,
        'working_piped':     piped,
        'working_invidious': inv,
        'last_probe_ago':    int(time.time() - _last_probe) if _last_probe else None,
    })

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

def _yt_info_cmd(url):
    clients = 'tv_embedded,android_vr,android,ios,web'
    cmd = [YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
           '--extractor-args', f'youtube:player_client={clients}',
           '--js-runtimes', 'node', url] + _proxy_args() + _cookies_args()
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

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

    # 1. Piped (has duration)
    if video_id:
        try:
            pd = piped_get_streams(video_id)
            if pd and not pd.get('error'):
                dur  = int(pd.get('duration', 0))
                m, s = divmod(dur, 60)
                return jsonify({
                    'title': pd.get('title', 'Unknown Title'),
                    'thumbnail': pd.get('thumbnailUrl', '') or '',
                    'duration': f'{m}:{s:02d}', 'duration_sec': dur,
                    'uploader': pd.get('uploader', ''), 'url': url,
                })
        except Exception:
            pass

    # 2. oEmbed (works from any IP) + duration from page
    if video_id:
        oe = _oembed_info(video_id)
        if oe:
            dur = 0
            if _PYTUBE_OK:
                try:
                    dur = PyTube(url).length or 0
                except Exception:
                    dur = _yt_duration_from_page(video_id)
            else:
                dur = _yt_duration_from_page(video_id)
            m, s = divmod(dur, 60)
            return jsonify({
                'title': oe.get('title', 'Unknown Title'),
                'thumbnail': oe.get('thumbnail_url') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                'duration': f'{m}:{s:02d}' if dur else '?:??',
                'duration_sec': dur,
                'uploader': oe.get('author_name', ''), 'url': url,
            })

    # 3. Invidious
    if video_id:
        try:
            iv = invidious_get_streams(video_id)
            if iv:
                dur  = int(iv.get('lengthSeconds', 0))
                m, s = divmod(dur, 60)
                thumb = next((t['url'] for t in iv.get('videoThumbnails', [])
                              if t.get('quality') in ('maxresdefault', 'sddefault', 'high')), '')
                return jsonify({
                    'title': iv.get('title', 'Unknown Title'),
                    'thumbnail': thumb,
                    'duration': f'{m}:{s:02d}', 'duration_sec': dur,
                    'uploader': iv.get('author', ''), 'url': url,
                })
        except Exception:
            pass

    # 4. yt-dlp
    try:
        result, last_err = None, '__BOT__'
        for _ in range(2):
            result   = _yt_info_cmd(url)
            if result.returncode == 0:
                break
            last_err = parse_ytdlp_error(result.stderr)
            if last_err != '__BOT__':
                break
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

@app.route('/upload-cookies', methods=['POST'])
def upload_cookies():
    txt = (request.get_json() or {}).get('cookies', '').strip()
    if not txt or len(txt) < 20:
        return jsonify({'error': 'No cookie data provided.'}), 400
    with open(COOKIES_FILE, 'w') as f:
        f.write(txt)
    return jsonify({'ok': True, 'message': 'Cookies saved. Downloads should now work.'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 13000))
    app.run(host='0.0.0.0', port=port)
