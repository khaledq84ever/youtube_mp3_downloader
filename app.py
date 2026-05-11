from flask import Flask, request, jsonify, send_file, render_template, Response, stream_with_context
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil
import urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR   = '/tmp/ytdl_cache'
YTDLP          = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL       = 1800
JOB_TIMEOUT    = 120
RATE_LIMIT     = 10
COOKIES_FILE   = '/tmp/yt_cookies.txt'
PROXY_URL      = os.environ.get('PROXY_URL', '')

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
    if PROXY_URL:
        return ['--proxy', PROXY_URL]
    return []

jobs          = {}
jobs_lock     = threading.Lock()
url_jobs      = {}
url_jobs_lock = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()


# ── Startup: keep yt-dlp fresh ────────────────────────────────────────────────

def _update_ytdlp():
    try:
        subprocess.run([YTDLP, '--update-to', 'stable'],
                       capture_output=True, timeout=90)
    except Exception:
        pass

threading.Thread(target=_update_ytdlp, daemon=True).start()


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


# ── Error parsing ─────────────────────────────────────────────────────────────

def parse_ytdlp_error(stderr):
    err = (stderr or '').lower()
    if 'sign in' in err or 'confirm you' in err or 'bot' in err:
        return '__BOT_DETECTED__'
    if '402' in err or 'payment required' in err or 'po token' in err:
        return '__BOT_DETECTED__'
    if 'age' in err and ('restrict' in err or 'gate' in err or '-restricted' in err):
        return 'This video is age-restricted and cannot be downloaded.'
    if 'private video' in err or ('private' in err and 'video' in err):
        return 'This video is private or no longer available.'
    if 'has been removed' in err or 'no longer available' in err:
        return 'This video has been removed or is no longer available.'
    if ('not available' in err or 'unavailable' in err) and \
       ('country' in err or 'region' in err):
        return '__BOT_DETECTED__'
    if 'live event' in err or ('live' in err and ('stream' in err or 'broadcast' in err)):
        return 'Live streams cannot be downloaded. Try after the stream ends.'
    if 'copyright' in err:
        return 'This video is unavailable due to copyright restrictions.'
    return '__BOT_DETECTED__'


# ── Piped API (primary path — bypasses YouTube bot detection) ─────────────────

_PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://watchapi.whatever.social',
    'https://api.piped.projectsegfau.lt',
]

# Startup: rank instances by latency so fastest is tried first
_piped_ranked = list(_PIPED_INSTANCES)
_piped_lock   = threading.Lock()

def _rank_piped():
    latencies = []
    for inst in _PIPED_INSTANCES:
        try:
            t0 = time.time()
            req = urllib.request.Request(
                f'{inst}/streams/dQw4w9WgXcQ',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read(512)
            latencies.append((time.time() - t0, inst))
        except Exception:
            latencies.append((999, inst))
    latencies.sort()
    with _piped_lock:
        global _piped_ranked
        _piped_ranked = [inst for _, inst in latencies]

threading.Thread(target=_rank_piped, daemon=True).start()


def _extract_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else None

def piped_get_streams(video_id):
    with _piped_lock:
        instances = list(_piped_ranked)
    for instance in instances:
        try:
            req = urllib.request.Request(
                f'{instance}/streams/{video_id}',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            if data.get('error'):
                continue
            return data
        except Exception:
            continue
    return None

def piped_best_audio(streams_data):
    best = None
    for f in streams_data.get('audioStreams', []):
        if not best or f.get('bitrate', 0) > best.get('bitrate', 0):
            best = f
    return best

def piped_best_video(streams_data, max_quality=None):
    best = None
    for f in streams_data.get('videoStreams', []):
        h = f.get('quality', '').replace('p', '')
        try: h = int(h)
        except Exception: h = 0
        if max_quality and h > max_quality:
            continue
        if not best:
            best = f
        else:
            bh = int(best.get('quality', '0').replace('p', '') or 0)
            if h > bh:
                best = f
    return best

def _http_download(url, dest_path, job_id=None, progress_start=10, progress_end=85):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.youtube.com/',
    })
    total, done = 0, 0
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get('Content-Length', 0))
        with open(dest_path, 'wb') as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and job_id:
                    pct = min(progress_start + int(done / total * (progress_end - progress_start)), progress_end)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

def piped_download(job_id, video_id, url, title, uploader, quality, fmt):
    data = piped_get_streams(video_id)
    if not data:
        return False

    file_id = str(uuid.uuid4())
    ext     = 'mp4' if fmt == 'mp4' else 'mp3'
    out     = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')

    try:
        _set_job(job_id, {'progress': 5})

        if fmt == 'mp4':
            q_map   = {'720': 720, '1080': 1080, 'best': None}
            vstream = piped_best_video(data, q_map.get(quality))
            astream = piped_best_audio(data)

            if not vstream or not vstream.get('url'):
                return False

            # Download video stream
            v_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_v.mp4')
            a_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_a.m4a')

            _set_job(job_id, {'progress': 10})
            _http_download(vstream['url'], v_tmp, job_id, 10, 50)

            # Download audio stream (if separate)
            if astream and astream.get('url') and astream['url'] != vstream.get('url'):
                _set_job(job_id, {'progress': 50})
                _http_download(astream['url'], a_tmp, job_id, 50, 80)

                # Merge with ffmpeg
                ffmpeg_dir = _find_ffmpeg_dir()
                ffmpeg_bin = os.path.join(ffmpeg_dir, 'ffmpeg') if ffmpeg_dir else shutil.which('ffmpeg') or 'ffmpeg'
                res = subprocess.run(
                    [ffmpeg_bin, '-i', v_tmp, '-i', a_tmp,
                     '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                     '-movflags', '+faststart', out, '-y'],
                    capture_output=True, timeout=300)
                for f in [v_tmp, a_tmp]:
                    try: os.remove(f)
                    except: pass
                if res.returncode != 0 or not os.path.exists(out):
                    return False
            else:
                # Video already has audio (muxed stream)
                shutil.move(v_tmp, out)

        else:
            # MP3: download best audio, convert with ffmpeg
            astream = piped_best_audio(data)
            if not astream or not astream.get('url'):
                return False

            raw_tmp = os.path.join(DOWNLOAD_DIR, f'{file_id}_raw.m4a')
            _http_download(astream['url'], raw_tmp, job_id, 10, 80)

            ffmpeg_dir = _find_ffmpeg_dir()
            ffmpeg_bin = os.path.join(ffmpeg_dir, 'ffmpeg') if ffmpeg_dir else shutil.which('ffmpeg') or 'ffmpeg'
            kbps = (quality or '320K').rstrip('Kk')
            res = subprocess.run(
                [ffmpeg_bin, '-i', raw_tmp, '-vn', '-ar', '44100', '-ac', '2',
                 '-b:a', f'{kbps}k', out, '-y'],
                capture_output=True, timeout=300)
            try: os.remove(raw_tmp)
            except: pass
            if res.returncode != 0 or not os.path.exists(out):
                return False

        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            return False

        filename = make_filename(
            title or data.get('title', f'video_{video_id}'),
            uploader or data.get('uploader', ''), ext)
        _set_job(job_id, {'status': 'done', 'file': out, 'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, out)
        return True

    except Exception:
        if os.path.exists(out):
            try: os.remove(out)
            except: pass
        return False


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
    if uploader and uploader.lower() not in clean.lower():
        name = f'{uploader} - {clean}'
    else:
        name = clean
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    return (name[:80] or 'download') + '.' + ext

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p:
        return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/run/current-system/sw/bin',
              '/usr/bin', '/usr/local/bin']:
        if os.path.isfile(os.path.join(d, 'ffmpeg')):
            return d
    nix_matches = glob.glob('/nix/store/*/bin/ffmpeg')
    if nix_matches:
        return os.path.dirname(nix_matches[0])
    return None

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):   os.remove(path)
            elif os.path.isdir(path):  shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        try:
            os.remove(_job_path(job_id))
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def build_cmd(url, output_template, quality='320K', fmt='mp3'):
    # android_vr uses a different API path that sometimes bypasses datacenter IP blocks
    clients = 'android_vr,android,ios,tv_embedded,web_embedded'
    if fmt == 'mp4':
        if quality == '720':
            fmt_str = 'best[height<=720][ext=mp4]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
        elif quality == '1080':
            fmt_str = 'best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
        else:
            fmt_str = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4',
               '--no-playlist', '--newline', '--geo-bypass', '--no-part',
               '--extractor-args', f'youtube:player_client={clients}'] + _proxy_args() + _cookies_args()
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3',
               '--audio-quality', quality or '320K',
               '--no-playlist', '--newline', '--geo-bypass', '--no-part',
               '--extractor-args', f'youtube:player_client={clients}'] + _proxy_args() + _cookies_args()
    ffmpeg_dir = _find_ffmpeg_dir()
    if ffmpeg_dir:
        cmd += ['--ffmpeg-location', ffmpeg_dir]
    cmd += ['-o', output_template, url]
    return cmd


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


# ── Worker ────────────────────────────────────────────────────────────────────

_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

def _run_ytdlp(cmd, job_id):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stderr_lines = []

    def _update_progress(line):
        m = _PROGRESS_RE.search(line)
        if m:
            pct = min(int(float(m.group(1))), 90)
            with jobs_lock:
                if jobs.get(job_id, {}).get('status') == 'processing':
                    jobs[job_id]['progress'] = pct

    def _read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            _update_progress(line)

    def _read_stdout():
        for line in proc.stdout:
            _update_progress(line)

    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err.start(); t_out.start()
    try:
        proc.wait(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1, 'timeout'
    t_err.join(timeout=5); t_out.join(timeout=5)
    return proc.returncode, ''.join(stderr_lines)

def do_convert(job_id, url, prefetched_title=None, prefetched_uploader=None,
               quality='320K', fmt='mp3'):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')

    # ── Step 1: Try Piped first (fast, bypasses 402 on cloud IPs) ────────────
    video_id = _extract_video_id(url)
    if video_id:
        if piped_download(job_id, video_id, url, prefetched_title,
                          prefetched_uploader, quality, fmt):
            with url_jobs_lock:
                url_jobs.pop(url, None)
            return

    # ── Step 2: yt-dlp fallback ───────────────────────────────────────────────
    returncode, stderr_text = -1, ''
    for attempt in range(2):
        cmd = build_cmd(url, output_template, quality, fmt)
        returncode, stderr_text = _run_ytdlp(cmd, job_id)
        if returncode == 0:
            break
        if stderr_text == 'timeout':
            _set_job(job_id, {'status': 'error', 'error': 'Download timed out. The video may be too long.'})
            with url_jobs_lock:
                url_jobs.pop(url, None)
            return
        err_msg = parse_ytdlp_error(stderr_text)
        if err_msg != '__BOT_DETECTED__':
            break
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*')):
            try: os.remove(f)
            except: pass

    try:
        if returncode != 0:
            err_msg = parse_ytdlp_error(stderr_text)
            _set_job(job_id, {'status': 'error',
                               'error': 'Video unavailable. Please try again.' if err_msg == '__BOT_DETECTED__' else err_msg})
            return

        ext    = 'mp4' if fmt == 'mp4' else 'mp3'
        target = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')

        if not os.path.exists(target):
            all_files   = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
            audio_exts  = {'.webm', '.m4a', '.ogg', '.opus', '.aac', '.mp4'}
            candidates  = [f for f in all_files
                           if os.path.splitext(f)[1].lower() in audio_exts]
            if not candidates:
                _set_job(job_id, {'status': 'error',
                                   'error': 'Output file not found. Please try again.'})
                return
            source = candidates[0]
            ffmpeg_dir = _find_ffmpeg_dir()
            ffmpeg_bin = (os.path.join(ffmpeg_dir, 'ffmpeg') if ffmpeg_dir
                          else shutil.which('ffmpeg') or 'ffmpeg')
            kbps = (quality or '320K').rstrip('Kk')
            res = subprocess.run(
                [ffmpeg_bin, '-i', source, '-vn', '-ar', '44100', '-ac', '2',
                 '-b:a', f'{kbps}k', target, '-y'],
                capture_output=True, timeout=300)
            try: os.remove(source)
            except: pass
            if res.returncode != 0 or not os.path.exists(target):
                _set_job(job_id, {'status': 'error',
                                   'error': 'Conversion failed. Please try again.'})
                return

        if os.path.getsize(target) < 1024:
            _set_job(job_id, {'status': 'error',
                               'error': 'Output file is empty. Please try again.'})
            return

        filename = make_filename(prefetched_title or 'download',
                                 prefetched_uploader or '', ext)
        _set_job(job_id, {'status': 'done', 'file': target,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, target)

    except Exception:
        _set_job(job_id, {'status': 'error',
                           'error': 'Conversion failed. Please try again.'})
    finally:
        with url_jobs_lock:
            url_jobs.pop(url, None)


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    piped_ok  = False
    oembed_ok = False
    try:
        data = piped_get_streams('dQw4w9WgXcQ')
        piped_ok = bool(data and not data.get('error'))
    except Exception:
        pass
    try:
        oe = _oembed_info('dQw4w9WgXcQ')
        oembed_ok = bool(oe and oe.get('title'))
    except Exception:
        pass
    ffmpeg_ok = bool(_find_ffmpeg_dir())
    ytdlp_ok  = False
    ytdlp_ver = ''
    try:
        r = subprocess.run([YTDLP, '--version'], capture_output=True, timeout=5)
        ytdlp_ok  = r.returncode == 0
        ytdlp_ver = r.stdout.strip() if isinstance(r.stdout, str) else r.stdout.decode().strip()
    except Exception:
        pass
    return jsonify({
        'status':          'ok',
        'piped':           piped_ok,
        'oembed':          oembed_ok,
        'ffmpeg':          ffmpeg_ok,
        'yt_dlp':          ytdlp_ok,
        'yt_dlp_version':  ytdlp_ver,
        'piped_instances': _piped_ranked,
        'info_source':     'piped' if piped_ok else ('oembed' if oembed_ok else 'yt-dlp'),
    })

@app.route('/manifest.json')
def manifest():
    data = {
        "name": "YT MP3 Converter",
        "short_name": "YT MP3",
        "description": "Convert YouTube videos to MP3 or MP4",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0b0f",
        "theme_color": "#7c5cfc",
        "icons": []
    }
    return jsonify(data)

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/ads.txt')
def ads_txt():
    return 'google.com, pub-3956390078338144, DIRECT, f08c47fec0942fa0\n', 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    xml  = (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{host}/</loc>'
            f'<changefreq>monthly</changefreq><priority>1.0</priority></url>'
            f'</urlset>')
    return xml, 200, {'Content-Type': 'application/xml'}

def _oembed_info(video_id):
    """YouTube oEmbed — works on any IP, no bot detection."""
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
    """Scrape duration from YouTube watch page — fallback when Piped is down."""
    try:
        req = urllib.request.Request(
            f'https://www.youtube.com/watch?v={video_id}',
            headers={'User-Agent': 'Mozilla/5.0',
                     'Accept-Language': 'en-US,en;q=0.9'})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read(80000).decode('utf-8', errors='ignore')
        m = re.search(r'"lengthSeconds"\s*:\s*"?(\d+)"?', html)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0

def _yt_info(url, extra_args=None):
    clients = 'android_vr,android,ios,tv_embedded,web_embedded'
    cmd = ([YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
             '--extractor-args', f'youtube:player_client={clients}', url]
           + _proxy_args() + _cookies_args() + (extra_args or []))
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

    # ── Step 1: Piped (best — includes duration) ──────────────────────────────
    if video_id:
        try:
            pd = piped_get_streams(video_id)
            if pd and not pd.get('error'):
                dur = int(pd.get('duration', 0))
                m, s = divmod(dur, 60)
                return jsonify({
                    'title':        pd.get('title', 'Unknown Title'),
                    'thumbnail':    pd.get('thumbnailUrl', '') or '',
                    'duration':     f'{m}:{s:02d}',
                    'duration_sec': dur,
                    'uploader':     pd.get('uploader', ''),
                    'url':          url,
                })
        except Exception:
            pass

    # ── Step 2: YouTube oEmbed + page scrape for duration ────────────────────
    if video_id:
        oe = _oembed_info(video_id)
        if oe:
            dur = _yt_duration_from_page(video_id)
            m, s = divmod(dur, 60)
            thumb = f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
            return jsonify({
                'title':        oe.get('title', 'Unknown Title'),
                'thumbnail':    oe.get('thumbnail_url') or thumb,
                'duration':     f'{m}:{s:02d}' if dur else '?:??',
                'duration_sec': dur,
                'uploader':     oe.get('author_name', ''),
                'url':          url,
            })

    # ── Step 3: yt-dlp fallback ───────────────────────────────────────────────
    try:
        result   = None
        last_err = '__BOT_DETECTED__'
        for attempt in range(2):
            result   = _yt_info(url)
            if result.returncode == 0:
                break
            last_err = parse_ytdlp_error(result.stderr)
            if last_err != '__BOT_DETECTED__':
                break

        if result and result.returncode == 0:
            info     = json.loads(result.stdout)
            duration = info.get('duration', 0)
            m, s     = divmod(int(duration), 60)
            return jsonify({
                'title':        info.get('title', 'Unknown Title'),
                'thumbnail':    info.get('thumbnail', ''),
                'duration':     f'{m}:{s:02d}',
                'duration_sec': int(duration),
                'uploader':     info.get('uploader', '') or info.get('channel', ''),
                'url':          url,
            })

        err_text = 'Video unavailable. Please try again in a moment.' if last_err == '__BOT_DETECTED__' else last_err
        return jsonify({'error': err_text}), 400

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

    with url_jobs_lock:
        existing = url_jobs.get(url)
        if existing:
            with jobs_lock:
                st = jobs.get(existing, {}).get('status')
            if st in ('pending', 'processing'):
                return jsonify({'job_id': existing})

    job_id = str(uuid.uuid4())
    job    = {'status': 'pending', 'file': None, 'filename': None,
               'error': None, 'progress': 0}
    with jobs_lock:
        jobs[job_id] = job
        _save_job(job_id, job)
    with url_jobs_lock:
        url_jobs[url] = job_id

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
def download_file(job_id):
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
