import base64
import glob
import os
import re
import tempfile
import time
import uuid
import zipfile
from urllib.parse import quote, unquote

from flask import Flask, render_template, request, send_file, redirect, url_for
import yt_dlp

app = Flask(__name__)
LOCAL_DOWNLOAD_FOLDER = 'temp'
ALLOWED_EXTENSIONS = {'txt'}

COOKIES_FILE = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')
_env_cookies_file = None

# ── Cookie helpers ──────────────────────────────────────────────────────────

def cookie_string_to_netscape(cookie_str):
    """Convert 'name=value; name2=value2' HTTP header string to Netscape cookies.txt"""
    lines = ['# Netscape HTTP Cookie File']
    expiry = str(int(time.time()) + 365 * 24 * 3600)
    for pair in cookie_str.split(';'):
        pair = pair.strip()
        if '=' not in pair:
            continue
        name, _, value = pair.partition('=')
        name = name.strip()
        value = value.strip()
        if name:
            lines.append(f'.youtube.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}')
    return '\n'.join(lines) + '\n'

def get_cookies_file():
    """Return path to a cookies.txt, or None. Uploaded file > env var."""
    if os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    global _env_cookies_file
    b64 = os.environ.get('YOUTUBE_COOKIES_B64', '').strip()
    if not b64:
        return None
    if _env_cookies_file and os.path.exists(_env_cookies_file):
        return _env_cookies_file
    try:
        content = base64.b64decode(b64).decode('utf-8')
        fd, path = tempfile.mkstemp(suffix='_env.txt')
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        _env_cookies_file = path
        print("Cookies loaded from YOUTUBE_COOKIES_B64 env var")
        return _env_cookies_file
    except Exception as e:
        print(f"Failed to load cookies from env: {e}")
        return None

def cookies_active():
    return bool(get_cookies_file()) or bool(os.environ.get('YOUTUBE_COOKIES_B64', '').strip())

# ── yt-dlp helpers ──────────────────────────────────────────────────────────

def make_ydl_opts(output_template):
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': False,
        'no_warnings': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web_creator'],
            }
        },
        'js_runtimes': ['nodejs'],
    }
    cf = get_cookies_file()
    if cf:
        opts['cookiefile'] = cf
    return opts

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html',
                           error_message=request.args.get('error'),
                           success_message=request.args.get('success'),
                           has_cookies=cookies_active())

@app.route('/set-cookies', methods=['POST'])
def set_cookies():
    """Accept either a cookies.txt file upload or a pasted cookie string."""
    # Option 1: file upload
    if 'cookiefile' in request.files and request.files['cookiefile'].filename:
        request.files['cookiefile'].save(COOKIES_FILE)
        print(f"Cookies file uploaded → {COOKIES_FILE}")
        return redirect(url_for('index', success="Cookies saved! All videos should now download."))

    # Option 2: paste from DevTools
    cookie_str = request.form.get('cookiestring', '').strip()
    if cookie_str:
        netscape = cookie_string_to_netscape(cookie_str)
        with open(COOKIES_FILE, 'w') as f:
            f.write(netscape)
        print(f"Cookies saved from paste → {COOKIES_FILE}")
        return redirect(url_for('index', success="Cookies saved! All videos should now download."))

    return redirect(url_for('index', error="No cookies provided."))

@app.route('/download', methods=['POST'])
def download():
    try:
        os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
        mp3_file, title = download_audio(request.form['url'])
        return send_file(os.path.abspath(mp3_file), as_attachment=True,
                         download_name=unquote(title))
    except Exception as e:
        print(f"Download error: {e}")
        msg = str(e)
        if 'unavailable' in msg.lower() or 'private' in msg.lower():
            user_msg = "Video is unavailable or private."
        elif 'Invalid' in msg or 'invalid' in msg:
            user_msg = "Invalid YouTube URL — please check the link."
        elif 'age' in msg.lower() or 'restricted' in msg.lower():
            user_msg = "Age-restricted video. Add your YouTube cookies below to download it."
        elif 'Sign in' in msg or 'bot' in msg.lower():
            user_msg = "YouTube blocked this download. Add your YouTube cookies below to fix it."
        else:
            user_msg = f"Download failed: {msg[:300]}"
        return redirect(url_for('index', error=user_msg))

@app.route('/batch_download', methods=['POST'])
def batch_download():
    try:
        os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
        urls = []
        if 'file' in request.files:
            f = request.files['file']
            if f and allowed_file(f.filename):
                urls = [l.decode('utf-8').strip() for l in f.readlines() if l.strip()]
        if not urls:
            return redirect(url_for('index', error="No URLs provided."))
        cleanup_temp_folder_if_needed()
        zip_name = str(uuid.uuid4())[:8] + '_music.zip'
        zip_path = os.path.join(LOCAL_DOWNLOAD_FOLDER, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for url in urls:
                try:
                    mp3, title = download_audio(url)
                    zf.write(mp3, os.path.basename(unquote(mp3)))
                except Exception as e:
                    print(f"Skipped {url}: {e}")
        return send_file(os.path.abspath(zip_path), as_attachment=True, download_name=zip_name)
    except Exception as e:
        return redirect(url_for('index', error=f"Batch failed: {str(e)[:200]}"))

# ── Core download logic ─────────────────────────────────────────────────────

def download_audio(url):
    if not is_valid_youtube_url(url):
        raise ValueError(f"Invalid YouTube URL: {url}")
    cleanup_temp_folder_if_needed()
    uid = str(uuid.uuid4())[:8]
    tmpl = os.path.join(LOCAL_DOWNLOAD_FOLDER, f'{uid}_%(title)s.%(ext)s')
    with yt_dlp.YoutubeDL(make_ydl_opts(tmpl)) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'audio')
    files = glob.glob(os.path.join(LOCAL_DOWNLOAD_FOLDER, f'{uid}_*.mp3'))
    if not files:
        raise FileNotFoundError("MP3 not found after conversion")
    return files[0], quote(title + '.mp3', safe='')

def is_valid_youtube_url(url):
    return re.match(r'^(https?\:\/\/)?(www\.youtube\.com|youtu\.?be)\/.+$', url)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_temp_folder_if_needed():
    if not os.path.exists(LOCAL_DOWNLOAD_FOLDER):
        return
    total = sum(os.path.getsize(f) for f in glob.glob(f"{LOCAL_DOWNLOAD_FOLDER}/*") if os.path.isfile(f))
    if total / (1024 ** 3) > 1:
        for f in glob.glob(f"{LOCAL_DOWNLOAD_FOLDER}/*"):
            try:
                os.remove(f)
            except Exception:
                pass

if __name__ == '__main__':
    os.makedirs(LOCAL_DOWNLOAD_FOLDER, exist_ok=True)
    port = int(os.environ.get('PORT', 13000))
    app.run(host='0.0.0.0', port=port)
