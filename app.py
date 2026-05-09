from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, zipfile, shutil
import urllib.parse

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ytdl_cache'
YTDLP = os.environ.get('YTDLP_PATH', 'yt-dlp')
MAX_BATCH = 20
FILE_TTL = 600

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
cookie_store = {}
jobs_lock = threading.Lock()


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
                job['error'] = 'Server restarted. Please convert again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p)
                continue
            jobs[job_id] = job
        except Exception:
            pass

_load_jobs()


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_url(url):
    """Strip tracking params (?si=, ?pp=, etc.) keeping only video/playlist IDs."""
    try:
        p = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(p.query, keep_blank_values=True)
        keep = {k: v for k, v in params.items() if k in ('v', 'list', 'index')}
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(keep, doseq=True)))
    except Exception:
        return url

# Common noise to strip from YouTube titles
_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:Official\s+(?:Video|Music\s+Video|Audio|Lyric[s]?\s+Video|Lyrics?)|'
    r'(?:4K|HD|Full\s+HD)(?:\s+Remaster(?:ed)?)?|Remaster(?:ed)?|'
    r'Lyrics?|Audio|Visualizer|Full\s+(?:Video|Song)|Music\s+Video|'
    r'Official|Video\s+Clip|Clip)\s*[\)\]]\s*',
    re.IGNORECASE
)

def make_filename(title, uploader='', ext='mp3'):
    """Return 'Uploader - Title.ext', stripped of noise, max 80 chars."""
    clean = _NOISE_RE.sub(' ', title).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    if uploader and uploader.lower() not in clean.lower():
        name = f'{uploader} - {clean}'
    else:
        name = clean
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    return (name[:80] or 'download') + '.' + ext

def is_valid_url(url):
    return bool(re.search(r'(youtube\.com|youtu\.be)/', url))

def is_playlist_only(url):
    return ('playlist?' in url or '/playlist' in url) and 'v=' not in url

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p:
        return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/run/current-system/sw/bin', '/usr/bin', '/usr/local/bin']:
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
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        try:
            os.remove(_job_path(job_id))
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def build_cmd(url, output_template, cookie_path=None, quality='320K', fmt='mp3'):
    if fmt == 'mp4':
        cmd = [YTDLP,
               '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
               '--merge-output-format', 'mp4',
               '--no-playlist', '--newline',
               '--extractor-args', 'youtube:player_client=android,web']
    else:
        q = quality if quality else '320K'
        cmd = [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', q,
               '--no-playlist', '--newline',
               '--extractor-args', 'youtube:player_client=android,web']
    ffmpeg_dir = _find_ffmpeg_dir()
    if ffmpeg_dir:
        cmd += ['--ffmpeg-location', ffmpeg_dir]
    if cookie_path and os.path.exists(cookie_path):
        cmd += ['--cookies', cookie_path]
    cmd += ['-o', output_template, url]
    return cmd


# ── Workers ───────────────────────────────────────────────────────────────────

_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

def do_convert(job_id, url, cookie_path=None, prefetched_title=None, prefetched_uploader=None, quality='320K', fmt='mp3'):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
    cmd = build_cmd(url, output_template, cookie_path, quality, fmt)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            _set_job(job_id, {'status': 'error', 'error': 'Download timed out.'})
            return
        t.join(timeout=5)

        if proc.returncode != 0:
            _set_job(job_id, {'status': 'error',
                               'error': 'Download failed. Video may be unavailable or age-restricted.'})
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            _set_job(job_id, {'status': 'error', 'error': 'Output file not found.'})
            return

        title = prefetched_title or 'download'
        uploader = prefetched_uploader or ''
        ext = 'mp4' if fmt == 'mp4' else 'mp3'
        filename = make_filename(title, uploader, ext)
        _set_job(job_id, {'status': 'done', 'file': files[0], 'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, files[0])
    except Exception as e:
        _set_job(job_id, {'status': 'error', 'error': str(e)})


def do_batch_convert(job_id, urls, cookie_path=None):
    _set_job(job_id, {'status': 'processing', 'total': len(urls), 'done': 0, 'results': []})
    batch_dir = os.path.join(DOWNLOAD_DIR, f'batch_{job_id}')
    os.makedirs(batch_dir, exist_ok=True)
    results = []
    for i, url in enumerate(urls, 1):
        output_template = os.path.join(batch_dir, '%(title)s.%(ext)s')
        ok = False
        label = url
        before = set(glob.glob(os.path.join(batch_dir, '*')))
        try:
            r = subprocess.run(build_cmd(url, output_template, cookie_path),
                               capture_output=True, text=True, timeout=300)
            ok = r.returncode == 0
            if ok:
                after = set(glob.glob(os.path.join(batch_dir, '*')))
                new_files = after - before
                if new_files:
                    label = os.path.splitext(os.path.basename(next(iter(new_files))))[0]
        except Exception:
            pass
        results.append({'url': url, 'title': label, 'ok': ok})
        _set_job(job_id, {'done': i, 'results': results})

    zip_path = os.path.join(DOWNLOAD_DIR, f'batch_{job_id}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in glob.glob(os.path.join(batch_dir, '*')):
            zf.write(f, os.path.basename(f))
    shutil.rmtree(batch_dir, ignore_errors=True)
    _set_job(job_id, {'status': 'done', 'file': zip_path, 'filename': 'ytmp3_batch.zip'})
    schedule_cleanup(job_id, zip_path)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json() or {}
    url = normalize_url(data.get('url', '').strip())
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL — please check the link.'}), 400
    if is_playlist_only(url):
        return jsonify({'error': "That's a playlist — paste a single video URL (youtube.com/watch?v=...)."}), 400
    try:
        result = subprocess.run(
            [YTDLP, '--dump-json', '--no-playlist',
             '--extractor-args', 'youtube:player_client=android,web', url],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            err = result.stderr or ''
            if 'Sign in' in err or 'bot' in err or 'cookies' in err.lower():
                return jsonify({'error': 'This video requires sign-in. Upload your YouTube cookies using the 🔒 button below.',
                                'needs_cookies': True}), 400
            return jsonify({'error': 'Could not fetch video. It may be private, deleted, or region-blocked.'}), 400
        info = json.loads(result.stdout)
        duration = info.get('duration', 0)
        m, s = divmod(int(duration), 60)
        return jsonify({
            'title':     info.get('title', 'Unknown Title'),
            'thumbnail': info.get('thumbnail', ''),
            'duration':  f'{m}:{s:02d}',
            'uploader':  info.get('uploader', '') or info.get('channel', ''),
            'url':       url,
        })
    except Exception:
        return jsonify({'error': 'Failed to fetch video info.'}), 500


@app.route('/upload-cookies', methods=['POST'])
def upload_cookies():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    cookie_id = str(uuid.uuid4())
    path = os.path.join(DOWNLOAD_DIR, f'cookies_{cookie_id}.txt')
    f.save(path)
    cookie_store[cookie_id] = path
    return jsonify({'cookie_id': cookie_id})


@app.route('/start', methods=['POST'])
def start_convert():
    data = request.get_json() or {}
    url = normalize_url(data.get('url', '').strip())
    cookie_id = data.get('cookie_id')
    title = data.get('title', '').strip()
    uploader = data.get('uploader', '').strip()
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    if is_playlist_only(url):
        return jsonify({'error': 'Please paste a single video URL, not a playlist.'}), 400
    quality = data.get('quality', '320K') or '320K'
    fmt = data.get('format', 'mp3')
    if fmt not in ('mp3', 'mp4'):
        fmt = 'mp3'
    cookie_path = cookie_store.get(cookie_id) if cookie_id else None
    job_id = str(uuid.uuid4())
    job = {'status': 'pending', 'file': None, 'filename': None, 'error': None, 'progress': 0}
    with jobs_lock:
        jobs[job_id] = job
        _save_job(job_id, job)
    threading.Thread(target=do_convert,
                     args=(job_id, url, cookie_path, title or None, uploader or None, quality, fmt),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/batch', methods=['POST'])
def batch_start():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400
    txt_file = request.files['file']
    cookie_id = request.form.get('cookie_id')
    content = txt_file.read().decode('utf-8', errors='ignore')
    urls = [normalize_url(l.strip()) for l in content.splitlines()
            if l.strip() and is_valid_url(l.strip()) and not is_playlist_only(l.strip())]
    if not urls:
        return jsonify({'error': 'No valid YouTube URLs found in the .txt file'}), 400
    if len(urls) > MAX_BATCH:
        return jsonify({'error': f'Max {MAX_BATCH} URLs per batch'}), 400
    cookie_path = cookie_store.get(cookie_id) if cookie_id else None
    job_id = str(uuid.uuid4())
    job = {'status': 'pending', 'file': None, 'filename': None,
           'error': None, 'total': len(urls), 'done': 0, 'type': 'batch', 'results': []}
    with jobs_lock:
        jobs[job_id] = job
        _save_job(job_id, job)
    threading.Thread(target=do_batch_convert, args=(job_id, urls, cookie_path), daemon=True).start()
    return jsonify({'job_id': job_id, 'total': len(urls)})


@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in
                    ('status', 'error', 'filename', 'total', 'done', 'type', 'progress', 'results')})


@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please convert again.'}), 404
    mp3_path, filename = job['file'], job['filename']
    if not os.path.exists(mp3_path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410
    safe_name = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'audio.mp3'
    mimetype = 'application/zip' if safe_name.endswith('.zip') else 'audio/mpeg'
    return send_file(mp3_path, as_attachment=True, download_name=safe_name, mimetype=mimetype)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
