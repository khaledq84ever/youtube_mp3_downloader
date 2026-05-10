from flask import Flask, request, jsonify, send_file, render_template, Response
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil, zipfile
import urllib.parse, logging
from collections import defaultdict

app = Flask(__name__)
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR   = '/tmp/ytdl_cache'
YTDLP          = os.environ.get('YTDLP_PATH', 'yt-dlp')
PROXY          = os.environ.get('YTDLP_PROXY', '')
FILE_TTL       = 1800
INFO_TTL       = 900
RATE_LIMIT     = 10
MAX_CONCURRENT = 4
DISK_MIN_MB    = 300
PLAYER_CLIENTS = ['android', 'web', 'ios', 'mweb']

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.ERROR, format='%(asctime)s %(levelname)s %(message)s')

# ── State ─────────────────────────────────────────────────────────────────────
jobs          = {}
jobs_lock     = threading.Lock()
url_jobs      = {}
url_jobs_lock = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()
processing    = set()
queue_list    = []
queue_cond    = threading.Condition()
start_time    = time.time()


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
            if job.get('status') in ('pending', 'processing', 'queued'):
                job['status'] = 'error'
                job['error']  = 'Server restarted. Please convert again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                try: os.remove(p)
                except: pass
                continue
            jobs[job_id] = job
        except Exception:
            pass

_load_jobs()


# ── URL helpers ───────────────────────────────────────────────────────────────
_YT_URL_RE  = re.compile(r'^https?://(www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/', re.IGNORECASE)
_VID_ID_RE  = re.compile(r'(?:v=|/(?:shorts|live|embed|v)/|youtu\.be/)([a-zA-Z0-9_-]{11})')

def is_valid_url(url):
    return bool(_YT_URL_RE.match(url.strip()))

def extract_video_id(url):
    m = _VID_ID_RE.search(url)
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


# ── Error classification ──────────────────────────────────────────────────────
def parse_ytdlp_error(stderr):
    err = (stderr or '').lower()
    if 'age' in err and ('restrict' in err or 'gate' in err):
        return 'This video is age-restricted and cannot be downloaded.'
    if 'private video' in err or ('private' in err and 'video' in err):
        return 'This video is private or no longer available.'
    if 'has been removed' in err or 'no longer available' in err:
        return 'This video has been removed or is no longer available.'
    if ('not available' in err or 'unavailable' in err) and ('country' in err or 'region' in err):
        return "This video is not available in the server's region."
    if 'live event' in err or ('live' in err and ('stream' in err or 'broadcast' in err)):
        return 'Live streams cannot be downloaded. Try after the stream ends.'
    if 'copyright' in err:
        return 'This video is unavailable due to copyright restrictions.'
    if 'http error 403' in err or 'forbidden' in err:
        return 'Access denied by YouTube. Please try again in a moment.'
    if 'http error 429' in err or 'too many requests' in err:
        return 'Too many requests to YouTube. Please wait a moment and try again.'
    if 'sign in' in err or 'confirm your age' in err:
        return 'This video requires sign-in. Please try another video.'
    if 'http error 5' in err or 'connection' in err or 'network' in err:
        return 'Network error during download. Please try again.'
    return 'Download failed. Please try again.'


# ── Filename helpers ──────────────────────────────────────────────────────────
_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:Official\s+(?:Video|Music\s+Video|Audio|Lyric[s]?\s+Video|Lyrics?)|'
    r'(?:4K|HD|Full\s+HD)(?:\s+Remaster(?:ed)?)?|Remaster(?:ed)?|'
    r'Lyrics?|Audio|Visualizer|Full\s+(?:Video|Song)|Music\s+Video|'
    r'Official|Video\s+Clip|Clip)\s*[\)\]]\s*', re.IGNORECASE)

def make_filename(title, uploader='', ext='mp3'):
    clean = _NOISE_RE.sub(' ', title).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    if uploader and uploader.lower() not in clean.lower():
        name = f'{uploader} - {clean}'
    else:
        name = clean
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    return (name[:80] or 'download') + '.' + ext

def make_disposition(filename):
    ascii_name = re.sub(r'[^\x20-\x7e]', '_', filename)
    utf8_name  = urllib.parse.quote(filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p: return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/run/current-system/sw/bin',
              '/usr/bin', '/usr/local/bin']:
        if os.path.isfile(os.path.join(d, 'ffmpeg')): return d
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return os.path.dirname(nix[0]) if nix else None

_FFMPEG_DIR  = _find_ffmpeg_dir()
_ARIA2C_PATH = shutil.which('aria2c')


# ── Disk helpers ──────────────────────────────────────────────────────────────
def free_mb():
    return shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)

def evict_old_files():
    files = sorted(glob.glob(os.path.join(DOWNLOAD_DIR, '*')), key=os.path.getmtime)
    for f in files[:max(1, len(files) // 2)]:
        try:
            if os.path.isfile(f):  os.remove(f)
            elif os.path.isdir(f): shutil.rmtree(f, ignore_errors=True)
        except Exception: pass


# ── Queue / slot management ───────────────────────────────────────────────────
def acquire_slot(job_id):
    with queue_cond:
        if job_id not in queue_list:
            queue_list.append(job_id)
        while len(processing) >= MAX_CONCURRENT:
            pos = (queue_list.index(job_id) + 1) if job_id in queue_list else 1
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id].update(status='queued', queue_pos=pos)
            queue_cond.wait(timeout=3)
        processing.add(job_id)
        if job_id in queue_list:
            queue_list.remove(job_id)

def release_slot(job_id):
    with queue_cond:
        processing.discard(job_id)
        queue_cond.notify_all()


# ── Lifecycle helpers ─────────────────────────────────────────────────────────
def _set_job(job_id, updates):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(updates)
            _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception: pass
        try: os.remove(_job_path(job_id))
        except Exception: pass
        with jobs_lock: jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def schedule_delete(path, ttl):
    def _del():
        time.sleep(ttl)
        try:
            if os.path.isfile(path): os.remove(path)
        except Exception: pass
    threading.Thread(target=_del, daemon=True).start()


# ── Background workers ────────────────────────────────────────────────────────
def _background_cleanup():
    while True:
        time.sleep(600)
        cutoff = time.time() - 7200
        with jobs_lock:
            stale = [jid for jid, j in list(jobs.items()) if j.get('created', 0) < cutoff]
            for jid in stale: jobs.pop(jid, None)
        now = time.time()
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, '*')):
            try:
                if os.path.getmtime(f) < now - FILE_TTL * 2:
                    if os.path.isfile(f):  os.remove(f)
                    elif os.path.isdir(f): shutil.rmtree(f, ignore_errors=True)
            except Exception: pass

def _rate_cleanup():
    while True:
        time.sleep(120)
        now = time.time()
        with _rate_lock:
            for ip in list(_rate_store):
                _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
                if not _rate_store[ip]: del _rate_store[ip]

threading.Thread(target=_background_cleanup, daemon=True).start()
threading.Thread(target=_rate_cleanup, daemon=True).start()


# ── Rate limiter ──────────────────────────────────────────────────────────────
def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT: return False
        _rate_store[ip].append(now)
        return True

def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or 'unknown')


# ── Build yt-dlp command ──────────────────────────────────────────────────────
def build_cmd(url_or_info, output_template, quality='320K', fmt='mp3',
              use_info_json=False, player_client='android'):
    frags = '1' if PROXY else '16'

    if fmt == 'mp4':
        if quality == '720':
            fmt_str = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
        elif quality == '1080':
            fmt_str = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
        else:
            fmt_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4', '--remux-video', 'mp4',
               '--no-playlist', '--newline', '--retries', '5', '--fragment-retries', '5',
               '--concurrent-fragments', frags, '--http-chunk-size', '0']
    else:
        cmd = [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', quality or '320K',
               '--no-playlist', '--newline', '--retries', '5', '--fragment-retries', '5',
               '--concurrent-fragments', frags, '--http-chunk-size', '0']
        if not PROXY:
            cmd += ['--throttled-rate', '500K']

    if PROXY:
        cmd += ['--proxy', PROXY]
    if _ARIA2C_PATH and not PROXY:
        cmd += ['--external-downloader', 'aria2c',
                '--external-downloader-args',
                'aria2c:-x 16 -s 16 -k 1M --console-log-level=notice']
    if _FFMPEG_DIR:
        cmd += ['--ffmpeg-location', _FFMPEG_DIR]

    cmd += ['--geo-bypass']
    if not use_info_json:
        cmd += ['--extractor-args', f'youtube:player_client={player_client}']
    cmd += ['-o', output_template]
    if use_info_json:
        cmd += ['--load-info-json', url_or_info]
    else:
        cmd += [url_or_info]
    return cmd


# ── Progress regexes ──────────────────────────────────────────────────────────
_DL_RE    = re.compile(r'\[download\]\s+([\d.]+)%')
_ARIA2_RE = re.compile(r'\((\d+)%\)')
_ETA_RE   = re.compile(r'ETA:(\d+)([smh])')
_FF_RE    = re.compile(r'time=(\d+):(\d+):([\d.]+)')
_RETRY_ERRORS = ('http error 403', 'forbidden', 'http error 5', 'connection', 'network', 'timed out')


# ── FFmpeg progress simulation ────────────────────────────────────────────────
def _ffmpeg_progress(job_id, duration_sec):
    start    = time.time()
    estimate = max(8.0, duration_sec * 0.07)
    while True:
        time.sleep(0.8)
        with jobs_lock:
            job = jobs.get(job_id)
            if not job or job.get('status') != 'processing': break
            if job.get('progress', 0) >= 97: break
        pct     = min((time.time() - start) / estimate, 0.97)
        new_val = int(60 + pct * 37)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['progress'] = max(jobs[job_id].get('progress', 60), new_val)


# ── Conversion worker ─────────────────────────────────────────────────────────
def do_convert(job_id, url, title=None, uploader=None,
               quality='320K', fmt='mp3', info_path=None, duration_sec=0):
    acquire_slot(job_id)
    try:
        _run_convert(job_id, url, title, uploader, quality, fmt, info_path, duration_sec)
    except Exception as e:
        logging.error(f'do_convert outer job={job_id}: {e}')
        _set_job(job_id, {'status': 'error', 'error': 'Unexpected error. Please try again.'})
    finally:
        release_slot(job_id)
        with url_jobs_lock:
            url_jobs.pop(url, None)


def _run_convert(job_id, url, title, uploader, quality, fmt, info_path, duration_sec):
    _set_job(job_id, {'status': 'processing', 'progress': 0})

    if free_mb() < DISK_MIN_MB:
        evict_old_files()
        if free_mb() < DISK_MIN_MB:
            _set_job(job_id, {'status': 'error',
                               'error': 'Server storage full. Try again in a moment.'})
            return

    max_attempts = 3 if PROXY else 2

    for attempt in range(max_attempts):
        client   = PLAYER_CLIENTS[attempt % len(PLAYER_CLIENTS)]
        file_id  = str(uuid.uuid4())
        out_tmpl = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
        use_info = bool(attempt == 0 and not PROXY and info_path and os.path.exists(info_path))
        source   = info_path if use_info else url

        try:
            cmd  = build_cmd(source, out_tmpl, quality, fmt, use_info, client)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, errors='replace')
            in_ffmpeg  = False
            ffmpeg_th  = None
            output_buf = []

            for line in proc.stdout:
                line = line.rstrip()
                output_buf.append(line)

                m = _DL_RE.search(line)
                if m and not in_ffmpeg:
                    pct = min(int(float(m.group(1))), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = int(pct * 0.55)
                    continue

                m = _ARIA2_RE.search(line)
                if m and not in_ffmpeg:
                    pct = int(m.group(1))
                    eta = None
                    em  = _ETA_RE.search(line)
                    if em:
                        eta = int(em.group(1)) * {'s':1,'m':60,'h':3600}[em.group(2)]
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = int(pct * 0.55)
                            if eta: jobs[job_id]['eta'] = eta
                    continue

                if '[ExtractAudio]' in line or '[Merger]' in line or '[VideoRemuxer]' in line:
                    in_ffmpeg = True
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id].update(progress=60, eta=None)
                    if duration_sec > 0 and ffmpeg_th is None:
                        ffmpeg_th = threading.Thread(
                            target=_ffmpeg_progress, args=(job_id, duration_sec), daemon=True)
                        ffmpeg_th.start()

                if in_ffmpeg and duration_sec > 0:
                    m = _FF_RE.search(line)
                    if m:
                        elapsed = (int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3)))
                        ffpct   = min(elapsed / duration_sec, 1.0)
                        with jobs_lock:
                            if jobs.get(job_id, {}).get('status') == 'processing':
                                jobs[job_id]['progress'] = int(60 + ffpct * 38)

            proc.wait()

            if proc.returncode != 0:
                combined  = '\n'.join(output_buf[-30:])
                err_lower = combined.lower()
                if attempt < max_attempts - 1 and any(e in err_lower for e in _RETRY_ERRORS):
                    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*')):
                        try: os.remove(f)
                        except: pass
                    time.sleep(1); continue
                _set_job(job_id, {'status': 'error', 'error': parse_ytdlp_error(combined)})
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
                               'filename': filename, 'progress': 100, 'eta': None})
            schedule_cleanup(job_id, files[0])
            return

        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
            if attempt < max_attempts - 1: time.sleep(1); continue
            _set_job(job_id, {'status': 'error',
                               'error': 'Download timed out. The video may be too long.'})
            return
        except Exception as e:
            logging.error(f'job={job_id} attempt={attempt}: {e}')
            if attempt < max_attempts - 1: time.sleep(1); continue
            _set_job(job_id, {'status': 'error', 'error': 'Conversion failed. Please try again.'})
            return

    _set_job(job_id, {'status': 'error',
                       'error': 'Download failed after multiple attempts. Please try again.'})


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
    with jobs_lock: n = len(jobs)
    return jsonify({'status': 'ok', 'jobs': n, 'processing': len(processing),
                    'queued': len(queue_list), 'disk_free_mb': free_mb(),
                    'uptime_sec': int(time.time() - start_time)})

@app.route('/manifest.json')
def manifest():
    return jsonify({'name': 'YT MP3 Converter', 'short_name': 'YT MP3',
                    'description': 'Convert YouTube videos to MP3 or MP4',
                    'start_url': '/', 'display': 'standalone',
                    'background_color': '#07070f', 'theme_color': '#8b5cf6', 'icons': []})

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap():
    host = request.host_url.rstrip('/')
    xml  = (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{host}/</loc><changefreq>monthly</changefreq>'
            f'<priority>1.0</priority></url></urlset>')
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

    for attempt, client in enumerate(PLAYER_CLIENTS[:3]):
        try:
            cmd = [YTDLP, '--dump-json', '--no-playlist', '--geo-bypass',
                   '--extractor-args', f'youtube:player_client={client}']
            if PROXY: cmd += ['--proxy', PROXY]
            cmd += [url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)

            if result.returncode == 0 and result.stdout.strip():
                info         = json.loads(result.stdout)
                duration_sec = int(info.get('duration', 0))
                m, s         = divmod(duration_sec, 60)

                thumbnail = info.get('thumbnail', '')
                thumbs    = info.get('thumbnails') or []
                if thumbs:
                    best = max(thumbs,
                               key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                               default=None)
                    if best: thumbnail = best.get('url', thumbnail)

                info_id   = str(uuid.uuid4())
                info_path = os.path.join(DOWNLOAD_DIR, f'info_{info_id}.json')
                with open(info_path, 'w') as f:
                    f.write(result.stdout)
                schedule_delete(info_path, INFO_TTL)

                return jsonify({
                    'title':        info.get('title', 'Unknown Title'),
                    'thumbnail':    thumbnail,
                    'duration':     f'{m}:{s:02d}',
                    'duration_sec': duration_sec,
                    'uploader':     info.get('uploader', '') or info.get('channel', ''),
                    'view_count':   info.get('view_count', 0),
                    'url':          url,
                    'info_id':      info_id,
                })

            err = parse_ytdlp_error(result.stderr)
            if attempt < 2 and ('403' in (result.stderr or '') or
                                 'network' in (result.stderr or '').lower()):
                continue
            return jsonify({'error': err}), 400

        except subprocess.TimeoutExpired:
            if attempt < 2: continue
            return jsonify({'error': 'Request timed out. Please try again.'}), 504
        except Exception as e:
            logging.error(f'/info attempt={attempt}: {e}')
            if attempt < 2: continue
            return jsonify({'error': 'Failed to fetch video info. Please try again.'}), 500

    return jsonify({'error': 'Could not fetch video info. Please try again.'}), 500


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

    if fmt not in ('mp3', 'mp4'): fmt = 'mp3'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    if is_playlist_only(url):
        return jsonify({'error': 'Please paste a single video URL, not a playlist.'}), 400

    with url_jobs_lock:
        existing = url_jobs.get(url)
        if existing:
            with jobs_lock:
                st = jobs.get(existing, {}).get('status')
            if st in ('pending', 'processing', 'queued'):
                return jsonify({'job_id': existing})

    info_path = None
    if info_id:
        candidate = os.path.join(DOWNLOAD_DIR, f'info_{info_id}.json')
        if os.path.exists(candidate): info_path = candidate

    job_id = str(uuid.uuid4())
    job    = {'status': 'pending', 'file': None, 'filename': None,
              'error': None, 'progress': 0, 'eta': None,
              'queue_pos': None, 'created': time.time()}
    with jobs_lock:
        jobs[job_id] = job
        _save_job(job_id, job)
    with url_jobs_lock:
        url_jobs[url] = job_id

    threading.Thread(target=do_convert,
                     args=(job_id, url, title or None, uploader or None,
                           quality, fmt, info_path, duration_sec),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k)
                    for k in ('status', 'error', 'filename', 'progress', 'queue_pos', 'eta')})


@app.route('/download/<job_id>')
@app.route('/download/<job_id>/<path:filename>')
def download_file(job_id, filename=None):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please convert again.'}), 404

    path        = job['file']
    stored_name = job['filename']

    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410

    ext         = stored_name.rsplit('.', 1)[-1].lower()
    mime        = {'mp3': 'audio/mpeg', 'mp4': 'video/mp4',
                   'zip': 'application/zip'}.get(ext, 'application/octet-stream')
    disposition = make_disposition(stored_name)
    file_size   = os.path.getsize(path)

    # Range request support for mobile seek/resume
    range_hdr = request.headers.get('Range', '')
    m = re.match(r'bytes=(\d+)-(\d*)', range_hdr)
    if m:
        start  = int(m.group(1))
        end    = int(m.group(2)) if m.group(2) else file_size - 1
        end    = min(end, file_size - 1)
        length = end - start + 1

        def _stream():
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk: break
                    remaining -= len(chunk)
                    yield chunk

        return Response(_stream(), 206, {
            'Content-Range':       f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges':       'bytes',
            'Content-Length':      str(length),
            'Content-Disposition': disposition,
            'Content-Type':        mime,
        })

    resp = send_file(path, as_attachment=True,
                     download_name=stored_name, mimetype=mime, conditional=True)
    resp.headers['Content-Disposition'] = disposition
    resp.headers['Accept-Ranges']       = 'bytes'
    return resp


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
