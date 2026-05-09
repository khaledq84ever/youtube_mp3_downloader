from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil
import urllib.parse
from collections import defaultdict

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ytdl_cache'
YTDLP       = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL    = 1800   # 30 min
JOB_TIMEOUT = 480    # 8 min
RATE_LIMIT  = 10     # requests per minute per IP

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
    if 'age' in err and ('restrict' in err or 'gate' in err or '-restricted' in err):
        return 'This video is age-restricted and cannot be downloaded.'
    if 'private video' in err or ('private' in err and 'video' in err):
        return 'This video is private or no longer available.'
    if 'has been removed' in err or 'no longer available' in err:
        return 'This video has been removed or is no longer available.'
    if ('not available' in err or 'unavailable' in err) and \
       ('country' in err or 'region' in err):
        return "This video is not available in the server's region."
    if 'live event' in err or ('live' in err and ('stream' in err or 'broadcast' in err)):
        return 'Live streams cannot be downloaded. Try after the stream ends.'
    if 'copyright' in err:
        return 'This video is unavailable due to copyright restrictions.'
    return 'Video unavailable or region-blocked. Please try another video.'


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
    if fmt == 'mp4':
        if quality == '720':
            fmt_str = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
        elif quality == '1080':
            fmt_str = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
        else:
            fmt_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4',
               '--no-playlist', '--newline',
               '--extractor-args', 'youtube:player_client=android,web']
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3',
               '--audio-quality', quality or '320K',
               '--no-playlist', '--newline',
               '--extractor-args', 'youtube:player_client=android,web']
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

def do_convert(job_id, url, prefetched_title=None, prefetched_uploader=None,
               quality='320K', fmt='mp3'):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
    cmd = build_cmd(url, output_template, quality, fmt)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        stderr_lines = []

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = _PROGRESS_RE.search(line)
                if m:
                    pct = min(int(float(m.group(1))), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        try:
            proc.wait(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            _set_job(job_id, {'status': 'error',
                               'error': 'Download timed out. The video may be too long.'})
            return
        t.join(timeout=5)

        if proc.returncode != 0:
            _set_job(job_id, {'status': 'error',
                               'error': parse_ytdlp_error(''.join(stderr_lines))})
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            _set_job(job_id, {'status': 'error',
                               'error': 'Output file not found. Please try again.'})
            return

        ext      = 'mp4' if fmt == 'mp4' else 'mp3'
        filename = make_filename(prefetched_title or 'download',
                                 prefetched_uploader or '', ext)
        _set_job(job_id, {'status': 'done', 'file': files[0],
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, files[0])

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

@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    xml  = (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{host}/</loc>'
            f'<changefreq>monthly</changefreq><priority>1.0</priority></url>'
            f'</urlset>')
    return xml, 200, {'Content-Type': 'application/xml'}

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
    try:
        result = subprocess.run(
            [YTDLP, '--dump-json', '--no-playlist',
             '--extractor-args', 'youtube:player_client=android,web', url],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': parse_ytdlp_error(result.stderr)}), 400
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

    # Deduplication — return existing job if URL already processing
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
