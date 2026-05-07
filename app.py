from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, zipfile, shutil

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


def clean_title(title):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title).strip()[:120] or 'download'


def is_valid_url(url):
    return bool(re.search(r'(youtube\.com|youtu\.be)/', url))


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
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


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

FFMPEG_DIR = _find_ffmpeg_dir()


def build_cmd(url, output_template, cookie_path=None):
    cmd = [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', '0', '--no-playlist',
           '--extractor-args', 'youtube:player_client=android,web']
    if FFMPEG_DIR:
        cmd += ['--ffmpeg-location', FFMPEG_DIR]
    if cookie_path and os.path.exists(cookie_path):
        cmd += ['--cookies', cookie_path]
    cmd += ['-o', output_template, url]
    return cmd


def do_convert(job_id, url, cookie_path=None):
    with jobs_lock:
        jobs[job_id]['status'] = 'processing'

    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')

    try:
        result = subprocess.run(build_cmd(url, output_template, cookie_path),
                                capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            with jobs_lock:
                jobs[job_id].update(status='error',
                                    error='Download failed. Video may be unavailable or age-restricted.')
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            with jobs_lock:
                jobs[job_id].update(status='error', error='Output file not found.')
            return

        title_result = subprocess.run([YTDLP, '--get-title', '--no-playlist', url],
                                      capture_output=True, text=True, timeout=15)
        title = title_result.stdout.strip() or 'download'

        with jobs_lock:
            jobs[job_id].update(status='done', file=files[0],
                                filename=clean_title(title) + '.mp3')
        schedule_cleanup(job_id, files[0])
    except subprocess.TimeoutExpired:
        with jobs_lock:
            jobs[job_id].update(status='error', error='Download timed out.')
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update(status='error', error=str(e))


def do_batch_convert(job_id, urls, cookie_path=None):
    with jobs_lock:
        jobs[job_id].update(status='processing', total=len(urls), done=0)

    batch_dir = os.path.join(DOWNLOAD_DIR, f'batch_{job_id}')
    os.makedirs(batch_dir, exist_ok=True)

    for i, url in enumerate(urls, 1):
        output_template = os.path.join(batch_dir, '%(title)s.%(ext)s')
        try:
            subprocess.run(build_cmd(url, output_template, cookie_path),
                           capture_output=True, text=True, timeout=300)
        except Exception:
            pass
        with jobs_lock:
            jobs[job_id]['done'] = i

    zip_path = os.path.join(DOWNLOAD_DIR, f'batch_{job_id}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in glob.glob(os.path.join(batch_dir, '*')):
            zf.write(f, os.path.basename(f))

    shutil.rmtree(batch_dir, ignore_errors=True)

    with jobs_lock:
        jobs[job_id].update(status='done', file=zip_path,
                            filename='ytmp3_batch.zip')
    schedule_cleanup(job_id, zip_path)


@app.route('/')
def index():
    return render_template('index.html')




@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL — please check the link.'}), 400
    try:
        result = subprocess.run([YTDLP, '--dump-json', '--no-playlist', url],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': 'Could not fetch video. Check the URL and try again.'}), 400
        info = json.loads(result.stdout)
        duration = info.get('duration', 0)
        m, s = divmod(int(duration), 60)
        return jsonify({
            'title': info.get('title', 'Unknown Title'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': f'{m}:{s:02d}',
            'uploader': info.get('uploader', ''),
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
    url = data.get('url', '').strip()
    cookie_id = data.get('cookie_id')
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    cookie_path = cookie_store.get(cookie_id) if cookie_id else None
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None, 'error': None}

    threading.Thread(target=do_convert, args=(job_id, url, cookie_path), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/batch', methods=['POST'])
def batch_start():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400

    txt_file = request.files['file']
    cookie_id = request.form.get('cookie_id')
    content = txt_file.read().decode('utf-8', errors='ignore')
    urls = [l.strip() for l in content.splitlines() if l.strip() and is_valid_url(l.strip())]

    if not urls:
        return jsonify({'error': 'No valid YouTube URLs found in the .txt file'}), 400
    if len(urls) > MAX_BATCH:
        return jsonify({'error': f'Max {MAX_BATCH} URLs per batch'}), 400

    cookie_path = cookie_store.get(cookie_id) if cookie_id else None
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                        'error': None, 'total': len(urls), 'done': 0, 'type': 'batch'}

    threading.Thread(target=do_batch_convert, args=(job_id, urls, cookie_path), daemon=True).start()
    return jsonify({'job_id': job_id, 'total': len(urls)})


@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'total', 'done', 'type')})


@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready'}), 404
    mp3_path, filename = job['file'], job['filename']
    if not os.path.exists(mp3_path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410

    mimetype = 'application/zip' if filename.endswith('.zip') else 'audio/mpeg'
    return send_file(mp3_path, as_attachment=True, download_name=filename, mimetype=mimetype)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
