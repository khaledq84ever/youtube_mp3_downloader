import glob
import os
import re
import uuid
from urllib.parse import quote, unquote

from flask import Flask, render_template, request, send_file, redirect, url_for
import yt_dlp

app = Flask(__name__)
LOCAL_DOWNLOAD_FOLDER = 'temp'

# ── yt-dlp helpers ──────────────────────────────────────────────────────────

def make_ydl_opts(output_template):
    return {
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
    }

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html',
                           error_message=request.args.get('error'),
                           success_message=request.args.get('success'))

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
        elif 'Invalid YouTube URL' in msg:
            user_msg = "Invalid YouTube URL — please check the link."
        elif 'age' in msg.lower() or 'restricted' in msg.lower():
            user_msg = "Age-restricted video — this video cannot be downloaded."
        elif 'Sign in' in msg or 'bot' in msg.lower():
            user_msg = "YouTube blocked this download. Please try again later."
        else:
            user_msg = f"Download failed: {msg[:300]}"
        return redirect(url_for('index', error=user_msg))

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
