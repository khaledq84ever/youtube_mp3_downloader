import glob
import os
import re
import subprocess
import uuid
import zipfile
from urllib.parse import quote, unquote

from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify
import yt_dlp

app = Flask(__name__)
LOCAL_DOWNLOAD_FOLDER = 'temp'

# ── Auto-update yt-dlp on startup ────────────────────────────────────────────

def update_ytdlp():
    try:
        subprocess.run(['pip', 'install', '--upgrade', 'yt-dlp', '-q'], timeout=60, check=False)
        print('[startup] yt-dlp updated.')
    except Exception as e:
        print(f'[startup] yt-dlp update skipped: {e}')

# ── yt-dlp helpers ──────────────────────────────────────────────────────────

def make_ydl_opts(output_template, quality='192'):
    return {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(quality),
        }],
        'quiet': False,
        'no_warnings': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web_creator'],
            }
        },
    }

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html',
                           error_message=request.args.get('error'),
                           success_message=request.args.get('success'))

# Legacy form-based download (kept for compatibility)
@app.route('/download', methods=['POST'])
def download():
    try:
        os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
        mp3_file, title = download_audio(request.form['url'])
        return send_file(os.path.abspath(mp3_file), as_attachment=True,
                         download_name=unquote(title))
    except Exception as e:
        print(f'Download error: {e}')
        return redirect(url_for('index', error=friendly_error(str(e))))

# JSON endpoint for AJAX single convert
@app.route('/convert', methods=['POST'])
def convert():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    quality = str(data.get('quality') or '192')
    if quality not in ('128', '192', '320'):
        quality = '192'

    if not url:
        return jsonify(error='No URL provided.'), 400

    try:
        os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
        mp3_file, raw_title = download_audio(url, quality)
        filename = os.path.basename(mp3_file)
        return jsonify(
            title=raw_title,
            filename=filename,
            download_url=f'/file/{filename}'
        )
    except Exception as e:
        print(f'Convert error: {e}')
        return jsonify(error=friendly_error(str(e))), 500

# Serve individual converted files
@app.route('/file/<filename>')
def serve_file(filename):
    if '..' in filename or '/' in filename:
        return 'Forbidden', 403
    path = os.path.abspath(os.path.join(LOCAL_DOWNLOAD_FOLDER, filename))
    if not os.path.exists(path):
        return 'File not found', 404
    return send_file(path, as_attachment=True, download_name=filename)

# Batch ZIP download
@app.route('/batch', methods=['POST'])
def batch():
    data = request.get_json(silent=True) or {}
    urls = [u.strip() for u in (data.get('urls') or []) if u.strip()][:20]
    quality = str(data.get('quality') or '192')
    if quality not in ('128', '192', '320'):
        quality = '192'

    if not urls:
        return jsonify(error='No URLs provided.'), 400

    os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
    mp3_files = []
    errors = []

    for url in urls:
        try:
            mp3_file, _ = download_audio(url, quality)
            mp3_files.append(mp3_file)
        except Exception as e:
            errors.append(f'{url}: {friendly_error(str(e))}')

    if not mp3_files:
        return jsonify(error='All downloads failed. ' + '; '.join(errors[:3])), 500

    zip_path = os.path.join(LOCAL_DOWNLOAD_FOLDER, f'batch_{uuid.uuid4().hex[:8]}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in mp3_files:
            zf.write(f, os.path.basename(f))

    return send_file(os.path.abspath(zip_path), as_attachment=True, download_name='mp3_batch.zip')

# ── Core download logic ─────────────────────────────────────────────────────

def download_audio(url, quality='192'):
    if not is_valid_youtube_url(url):
        raise ValueError(f'Invalid YouTube URL: {url}')
    cleanup_temp_folder_if_needed()
    uid = str(uuid.uuid4())[:8]
    tmpl = os.path.join(LOCAL_DOWNLOAD_FOLDER, f'{uid}_%(title)s.%(ext)s')
    with yt_dlp.YoutubeDL(make_ydl_opts(tmpl, quality)) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'audio')
    files = glob.glob(os.path.join(LOCAL_DOWNLOAD_FOLDER, f'{uid}_*.mp3'))
    if not files:
        raise FileNotFoundError('MP3 not found after conversion')
    return files[0], title

def is_valid_youtube_url(url):
    return re.match(r'^(https?\:\/\/)?(www\.youtube\.com|youtu\.?be)\/.+$', url)

def friendly_error(msg):
    msg_low = msg.lower()
    if 'unavailable' in msg_low or 'private' in msg_low:
        return 'Video is unavailable or private.'
    if 'Invalid YouTube URL' in msg:
        return 'Invalid YouTube URL — please check the link.'
    if 'age' in msg_low or 'restricted' in msg_low:
        return 'Age-restricted video — cannot be downloaded.'
    if 'Sign in' in msg or 'bot' in msg_low:
        return 'YouTube blocked this download. Please try again later.'
    return f'Download failed: {msg[:200]}'

def cleanup_temp_folder_if_needed():
    if not os.path.exists(LOCAL_DOWNLOAD_FOLDER):
        return
    total = sum(os.path.getsize(f) for f in glob.glob(f'{LOCAL_DOWNLOAD_FOLDER}/*') if os.path.isfile(f))
    if total / (1024 ** 3) > 1:
        for f in glob.glob(f'{LOCAL_DOWNLOAD_FOLDER}/*'):
            try:
                os.remove(f)
            except Exception:
                pass

if __name__ == '__main__':
    update_ytdlp()
    os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
    port = int(os.environ.get('PORT', 13000))
    app.run(host='0.0.0.0', port=port)
