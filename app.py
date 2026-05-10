from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil, zipfile
import urllib.parse
from collections import defaultdict


app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ytdl_cache'
YTDLP        = os.environ.get('YTDLP_PATH', 'yt-dlp')
PROXY        = os.environ.get('YTDLP_PROXY', '')   # e.g. socks5://user:pass@host:port
FILE_TTL     = 1800   # 30 min
INFO_TTL     = 900    # 15 min for cached info JSON
JOB_TIMEOUT  = 480    # 8 min
RATE_LIMIT   = 10     # requests per minute per IP

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs          = {}
jobs_lock     = threading.Lock()
url_jobs      = {}
url_jobs_lock = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()




# ── Job persistence ────────────────────────────────────────────────────────

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


# ── URL helpers ────────────────────────────────────────────────────────────

# Anchored so notayoutube.com / evil.com?ref=youtube.com cannot bypass
_YT_URL_RE = re.compile(
    r'^https?://(www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/',
    re.IGNORECASE)

_VIDEO_ID_RE = re.compile(
    r'(?:v=|/(?:shorts|live|embed|v)/|youtu\.be/)([a-zA-Z0-9_-]{11})')

def is_valid_url(url):
    return bool(_YT_URL_RE.match(url.strip()))

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


# ── Error parsing ──────────────────────────────────────────────────────────

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


# ── Filename / ffmpeg helpers ──────────────────────────────────────────────

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

_FFMPEG_DIR  = _find_ffmpeg_dir()
_ARIA2C_PATH = shutil.which('aria2c')

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
        try:
            os.remove(_job_path(job_id))
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def schedule_delete(path, ttl):
    def _del():
        time.sleep(ttl)
        try:
            if os.path.isfile(path): os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_del, daemon=True).start()

def build_cmd(url_or_info, output_template, quality='320K', fmt='mp3',
              use_info_json=False):
    if fmt == 'mp4':
        if quality == '720':
            fmt_str = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
        elif quality == '1080':
            fmt_str = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
        else:
            fmt_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4',
               '--no-playlist', '--newline', '--no-warnings',
               '--concurrent-fragments', '16',
               '--throttled-rate', '500K']
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3',
               '--audio-quality', quality or '320K',
               '--no-playlist', '--newline', '--no-warnings',
               '--concurrent-fragments', '16',
               '--throttled-rate', '500K']

    if PROXY:
        cmd += ['--proxy', PROXY]

    if _ARIA2C_PATH and not PROXY:
        # aria2c doesn't inherit yt-dlp proxy settings, skip when proxy is set
        cmd += ['--external-downloader', 'aria2c',
                '--external-downloader-args', 'aria2c:-x 16 -s 16 -k 1M --min-split-size=1M']

    if _FFMPEG_DIR:
        cmd += ['--ffmpeg-location', _FFMPEG_DIR]

    cmd += ['--geo-bypass']
    if not use_info_json:
        cmd += ['--extractor-args', 'youtube:player_client=tv_embedded,ios,android,web']

    cmd += ['-o', output_template]

    if use_info_json:
        cmd += ['--load-info-json', url_or_info]
    else:
        cmd += [url_or_info]

    return cmd


# ── Rate limiter ───────────────────────────────────────────────────────────

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


# ── Worker ─────────────────────────────────────────────────────────────────

_DL_PROGRESS_RE  = re.compile(r'\[download\]\s+([\d.]+)%')
_ARIA2_PROGRESS_RE = re.compile(r'\((\d+)%\)')
_FF_TIME_RE      = re.compile(r'time=(\d+):(\d+):([\d.]+)')

def do_convert(job_id, url, title=None, uploader=None,
               quality='320K', fmt='mp3', info_path=None, duration_sec=0):
    _set_job(job_id, {'status': 'processing', 'progress': 0})

    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')

    use_info = bool(info_path and os.path.exists(info_path))
    source   = info_path if use_info else url

    cmd = build_cmd(source, output_template, quality, fmt, use_info)

    try:
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, errors='replace')
        in_ffmpeg = False

        for line in proc.stdout:
            line = line.rstrip()

            # Download progress (yt-dlp native)
            m = _DL_PROGRESS_RE.search(line)
            if m and not in_ffmpeg:
                pct = min(int(float(m.group(1))), 90)
                with jobs_lock:
                    if jobs.get(job_id, {}).get('status') == 'processing':
                        jobs[job_id]['progress'] = int(pct * 0.55)
                continue

            # Download progress (aria2c)
            m = _ARIA2_PROGRESS_RE.search(line)
            if m and not in_ffmpeg:
                pct = int(m.group(1))
                with jobs_lock:
                    if jobs.get(job_id, {}).get('status') == 'processing':
                        jobs[job_id]['progress'] = int(pct * 0.55)
                continue

            # FFmpeg phase begins
            if '[ExtractAudio]' in line or '[Merger]' in line or '[VideoRemuxer]' in line:
                in_ffmpeg = True
                with jobs_lock:
                    if jobs.get(job_id, {}).get('status') == 'processing':
                        jobs[job_id]['progress'] = 60

            # FFmpeg real-time progress (time=HH:MM:SS.xx)
            if in_ffmpeg and duration_sec > 0:
                m = _FF_TIME_RE.search(line)
                if m:
                    elapsed = (int(m.group(1)) * 3600 +
                               int(m.group(2)) * 60 +
                               float(m.group(3)))
                    ffpct = min(elapsed / duration_sec, 1.0)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = int(60 + ffpct * 38)

        proc.wait()

        if proc.returncode != 0:
            _set_job(job_id, {'status': 'error',
                               'error': 'Download failed. Video may be unavailable or age-restricted.'})
            return

        files = [f for f in glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
                 if not f.endswith(('.part', '.ytdl', '.json'))]
        if not files:
            _set_job(job_id, {'status': 'error',
                               'error': 'Output file not found. Please try again.'})
            return

        ext      = 'mp4' if fmt == 'mp4' else 'mp3'
        filename = make_filename(title or 'download', uploader or '', ext)
        _set_job(job_id, {'status': 'done', 'file': files[0],
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, files[0])

    except subprocess.TimeoutExpired:
        try: proc.kill()
        except Exception: pass
        _set_job(job_id, {'status': 'error',
                           'error': 'Download timed out. The video may be too long.'})
    except Exception:
        _set_job(job_id, {'status': 'error',
                           'error': 'Conversion failed. Please try again.'})
    finally:
        with url_jobs_lock:
            url_jobs.pop(url, None)


# ── Security headers ───────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "YT MP3 Converter",
        "short_name": "YT MP3",
        "description": "Convert YouTube videos to MP3 or MP4",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#07070f",
        "theme_color": "#8b5cf6",
        "icons": []
    })

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

    data = request.get_json(silent=True) or {}
    url  = normalize_url(data.get('url', '').strip())
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL — please check the link.'}), 400
    if is_playlist_only(url):
        return jsonify({'error': "That's a playlist URL. Please paste a single video link."}), 400

    try:
        info_cmd = [YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
                    '--extractor-args', 'youtube:player_client=tv_embedded,ios,android,web']
        if PROXY:
            info_cmd += ['--proxy', PROXY]
        info_cmd += [url]
        result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': parse_ytdlp_error(result.stderr)}), 400

        info         = json.loads(result.stdout)
        duration_sec = int(info.get('duration', 0))
        m, s         = divmod(duration_sec, 60)

        # Cache the full info JSON so /start can skip re-fetching (~11s saved)
        info_id   = str(uuid.uuid4())
        info_path = os.path.join(DOWNLOAD_DIR, f'info_{info_id}.json')
        with open(info_path, 'w') as f:
            f.write(result.stdout)
        schedule_delete(info_path, INFO_TTL)

        return jsonify({
            'title':        info.get('title', 'Unknown Title'),
            'thumbnail':    info.get('thumbnail', ''),
            'duration':     f'{m}:{s:02d}',
            'duration_sec': duration_sec,
            'uploader':     info.get('uploader', '') or info.get('channel', ''),
            'url':          url,
            'info_id':      info_id,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out. Please try again.'}), 504
    except Exception:
        return jsonify({'error': 'Failed to fetch video info. Please try again.'}), 500


@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429

    data         = request.get_json(silent=True) or {}
    url          = normalize_url(data.get('url', '').strip())
    title        = data.get('title', '').strip()
    uploader     = data.get('uploader', '').strip()
    quality      = data.get('quality', '320K') or '320K'
    fmt          = data.get('format', 'mp3')
    info_id      = data.get('info_id', '')
    duration_sec = int(data.get('duration_sec', 0) or 0)

    if fmt not in ('mp3', 'mp4'):
        fmt = 'mp3'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    if is_playlist_only(url):
        return jsonify({'error': 'Please paste a single video URL, not a playlist.'}), 400

    # Return existing job if the same URL is already being processed
    with url_jobs_lock:
        existing = url_jobs.get(url)
        if existing:
            with jobs_lock:
                st = jobs.get(existing, {}).get('status')
            if st in ('pending', 'processing'):
                return jsonify({'job_id': existing})

    # Use cached info JSON to skip yt-dlp re-fetching
    info_path = None
    if info_id:
        candidate = os.path.join(DOWNLOAD_DIR, f'info_{info_id}.json')
        if os.path.exists(candidate):
            info_path = candidate

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
        args=(job_id, url, title or None, uploader or None,
              quality, fmt, info_path, duration_sec),
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
@app.route('/download/<job_id>/<path:filename>')
def download_file(job_id, filename=None):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please convert again.'}), 404
    path, stored_name = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', stored_name).strip() or 'audio.mp3'
    return send_file(path, as_attachment=True, download_name=safe,
                     mimetype='application/octet-stream')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
