from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess
import os
import uuid
import json
import re
import glob
import threading
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ytdl_cache'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# yt-dlp: prefer local install, fall back to PATH
YTDLP = os.environ.get('YTDLP_PATH', 'yt-dlp')

jobs = {}
jobs_lock = threading.Lock()


def clean_title(title):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title)
    return safe.strip()[:120] or 'download'


def is_valid_url(url):
    return bool(re.match(
        r'^(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]',
        url
    ))


def do_convert(job_id, url):
    with jobs_lock:
        jobs[job_id]['status'] = 'processing'

    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')

    try:
        result = subprocess.run(
            [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', '0',
             '--no-playlist', '-o', output_template, url],
            capture_output=True, text=True, timeout=300
        )

        if result.returncode != 0:
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = 'Download failed. Video may be unavailable or age-restricted.'
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = 'Output file not found after conversion.'
            return

        mp3_path = files[0]

        title_result = subprocess.run(
            [YTDLP, '--get-title', '--no-playlist', url],
            capture_output=True, text=True, timeout=15
        )
        title = title_result.stdout.strip() or 'download'
        filename = clean_title(title) + '.mp3'

        with jobs_lock:
            jobs[job_id]['status'] = 'done'
            jobs[job_id]['file'] = mp3_path
            jobs[job_id]['filename'] = filename

    except subprocess.TimeoutExpired:
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = 'Download timed out. Try a shorter video.'
    except Exception as e:
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = str(e)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json() or {}
    url = data.get('url', '').strip()

    if not is_valid_url(url):
        return jsonify({'error': 'Please enter a valid YouTube URL'}), 400

    try:
        result = subprocess.run(
            [YTDLP, '--dump-json', '--no-playlist', url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({'error': 'Could not fetch video. Check the URL and try again.'}), 400

        info = json.loads(result.stdout)
        duration = info.get('duration', 0)
        minutes, seconds = divmod(int(duration), 60)

        return jsonify({
            'title': info.get('title', 'Unknown Title'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': f'{minutes}:{seconds:02d}',
            'uploader': info.get('uploader', ''),
        })
    except Exception:
        return jsonify({'error': 'Failed to fetch video info.'}), 500


@app.route('/start', methods=['POST'])
def start_convert():
    data = request.get_json() or {}
    url = data.get('url', '').strip()

    if not is_valid_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None, 'error': None}

    threading.Thread(target=do_convert, args=(job_id, url), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({'status': job['status'], 'error': job.get('error'), 'filename': job.get('filename')})


@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready'}), 404

    mp3_path = job['file']
    filename = job['filename']

    if not os.path.exists(mp3_path):
        return jsonify({'error': 'File expired. Please convert again.'}), 410

    response = send_file(mp3_path, as_attachment=True, download_name=filename, mimetype='audio/mpeg')

    def cleanup():
        time.sleep(60)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)

    threading.Thread(target=cleanup, daemon=True).start()
    return response


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
