"""
MediaGrab Server — Production-Hardened
---------------------------------------
Flask backend with rate limiting, file size caps, auto-cleanup,
download timeouts, URL validation, and structured logging.
"""

import os
import re
import uuid
import json
import time
import shutil
import logging
import threading
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mediagrab")

# ─── App Setup ────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)

# ─── Configuration ────────────────────────────────────────────────────
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE_MB = 500           # Max download size in MB
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 300           # Kill downloads after 5 minutes
CLEANUP_INTERVAL = 600           # Check for old files every 10 min
FILE_TTL = 600                   # Delete files after 10 minutes

FFMPEG_PATH = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFMPEG_DIR = str(Path(FFMPEG_PATH).parent) if FFMPEG_PATH else None
COOKIES_FILE = Path(__file__).parent / "cookies.txt"

ALLOWED_DOMAINS = None  # None = allow all. Set to list to restrict.

download_tasks = {}
url_cache = {}  # Maps URL+type → task_id for caching
DEFAULT_COOKIE_BROWSER = "chrome"

# ─── Restore cookies from environment variable (free persistence on Render) ──
def _restore_cookies_from_env():
    """If COOKIES_B64 env var exists, decode and write to cookies.txt.
    This survives Render restarts without needing paid persistent disk."""
    import base64
    cookies_b64 = os.environ.get("COOKIES_B64", "")
    if cookies_b64 and (not COOKIES_FILE.exists() or COOKIES_FILE.stat().st_size < 100):
        try:
            decoded = base64.b64decode(cookies_b64).decode("utf-8")
            COOKIES_FILE.write_text(decoded)
            log.info(f"🍪 Restored cookies from COOKIES_B64 env var ({len(decoded)} bytes)")
        except Exception as e:
            log.warning(f"Failed to restore cookies from env: {e}")

_restore_cookies_from_env()


# ─── Helpers ──────────────────────────────────────────────────────────

def validate_url(url):
    """Validate URL format and optionally check against allowed domains."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "Only HTTP/HTTPS URLs are allowed"
        if not parsed.netloc:
            return False, "Invalid URL format"
        if ALLOWED_DOMAINS:
            domain = parsed.netloc.lower().replace("www.", "")
            if not any(d in domain for d in ALLOWED_DOMAINS):
                return False, f"Domain not supported"
        return True, ""
    except Exception:
        return False, "Invalid URL"


def get_cookie_opts(browser=None):
    """Return yt-dlp cookie options. Uses cookies.txt uploaded by user."""
    opts = {
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"]
    }
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def sanitize_filename(name):
    """Remove unsafe characters from filename."""
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:200]


def get_base_ydl_opts(cookie_browser=None):
    """Return base yt-dlp options shared across all download types."""
    opts = {
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "geo_bypass": True,
        "nocheckcertificate": True,
        **get_cookie_opts(cookie_browser),
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


def crop_to_ratio(filepath, ratio, task_id):
    """Use FFmpeg to crop/scale video to target aspect ratio.
    Uses -c:a copy to preserve audio without re-encoding.
    """
    if not ratio or ratio == "original" or not FFMPEG_PATH:
        return filepath

    import subprocess
    download_tasks[task_id]["phase"] = "cropping"

    ratio_filters = {
        "16:9": r"crop=min(iw\,ih*16/9):min(ih\,iw*9/16),scale=1920:1080",
        "9:16": r"crop=min(iw\,ih*9/16):min(ih\,iw*16/9),scale=1080:1920",
        "1:1":  r"crop=min(iw\,ih):min(iw\,ih)",
    }

    vf = ratio_filters.get(ratio)
    if not vf:
        return filepath

    p = Path(filepath)
    out = p.parent / f"{p.stem}_{ratio.replace(':', 'x')}{p.suffix}"

    cmd = [
        FFMPEG_PATH, "-y", "-i", str(filepath),
        "-vf", vf, "-c:a", "copy", str(out)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        # Remove original, keep cropped
        p.unlink(missing_ok=True)
        log.info(f"✂️  Cropped task {task_id} to {ratio}")
        return str(out)
    except Exception as e:
        log.warning(f"Crop failed for {task_id}: {e} — keeping original")
        return filepath


# ─── Auto-Cleanup ─────────────────────────────────────────────────────

def cleanup_old_files():
    """Delete download files older than FILE_TTL seconds."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        cleaned = 0
        try:
            for task_dir in DOWNLOAD_DIR.iterdir():
                if task_dir.is_dir():
                    age = now - task_dir.stat().st_mtime
                    if age > FILE_TTL:
                        shutil.rmtree(task_dir, ignore_errors=True)
                        # Also remove from tasks dict
                        task_id = task_dir.name
                        if task_id in download_tasks:
                            download_tasks[task_id]["status"] = "expired"
                            download_tasks[task_id]["filepath"] = ""
                        cleaned += 1
            if cleaned:
                log.info(f"🧹 Cleaned up {cleaned} expired download(s)")
        except Exception as e:
            log.error(f"Cleanup error: {e}")

# Remove stale tasks from memory (older than 1 hour)
def cleanup_stale_tasks():
    while True:
        time.sleep(3600)
        now = time.time()
        stale = [tid for tid, t in download_tasks.items()
                 if now - t.get("created_at", 0) > 3600]
        for tid in stale:
            download_tasks.pop(tid, None)
        if stale:
            log.info(f"🗑  Purged {len(stale)} stale task(s) from memory")


# Start cleanup threads
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()
stale_thread = threading.Thread(target=cleanup_stale_tasks, daemon=True)
stale_thread.start()


# ─── Download Tasks ───────────────────────────────────────────────────

def _make_progress_hook(task_id):
    """Create a progress hook that also enforces file size limits."""
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)

            # Enforce size limit
            if total > MAX_FILE_SIZE:
                download_tasks[task_id]["status"] = "error"
                download_tasks[task_id]["error"] = (
                    f"File too large ({total // (1024*1024)}MB). "
                    f"Max allowed: {MAX_FILE_SIZE_MB}MB"
                )
                raise Exception(download_tasks[task_id]["error"])

            if total > 0:
                download_tasks[task_id]["progress"] = round(
                    (downloaded / total) * 100, 1
                )
            download_tasks[task_id]["speed"] = d.get("speed", 0)
            download_tasks[task_id]["eta"] = d.get("eta", 0)
            download_tasks[task_id]["phase"] = "downloading"
        elif d["status"] == "finished":
            download_tasks[task_id]["progress"] = 95
            download_tasks[task_id]["phase"] = "processing"
            download_tasks[task_id]["filename"] = d.get("filename", "")
    return hook


def _finalize_task(task_id, output_dir, info=None):
    """Find downloaded file and update task with final info."""
    files = list(output_dir.glob("*"))
    if files:
        actual_file = max(files, key=lambda f: f.stat().st_size)
        size = actual_file.stat().st_size

        if size > MAX_FILE_SIZE:
            actual_file.unlink(missing_ok=True)
            raise Exception(
                f"Downloaded file too large ({size // (1024*1024)}MB). "
                f"Max: {MAX_FILE_SIZE_MB}MB"
            )

        download_tasks[task_id]["filename"] = actual_file.name
        download_tasks[task_id]["filepath"] = str(actual_file)
        download_tasks[task_id]["filesize"] = size

    download_tasks[task_id]["status"] = "complete"
    download_tasks[task_id]["progress"] = 100
    download_tasks[task_id]["phase"] = "complete"
    download_tasks[task_id]["completed_at"] = time.time()

    if info:
        download_tasks[task_id]["title"] = info.get("title", "")
        download_tasks[task_id]["resolution"] = (
            f"{info.get('width', 0)}x{info.get('height', 0)}"
        )
        download_tasks[task_id]["fps"] = info.get("fps", 0)

    log.info(f"✅ Task {task_id} complete: {download_tasks[task_id].get('title', 'untitled')}")


def _run_with_timeout(task_id, func):
    """Wrapper to enforce download timeout."""
    download_tasks[task_id]["phase"] = "starting"
    timer = threading.Timer(
        DOWNLOAD_TIMEOUT,
        lambda: _timeout_task(task_id)
    )
    timer.daemon = True
    timer.start()
    try:
        func()
    finally:
        timer.cancel()


def _timeout_task(task_id):
    """Called when a download exceeds the timeout."""
    task = download_tasks.get(task_id)
    if task and task["status"] not in ("complete", "error", "expired"):
        download_tasks[task_id]["status"] = "error"
        download_tasks[task_id]["error"] = (
            f"Download timed out after {DOWNLOAD_TIMEOUT}s"
        )
        log.warning(f"⏰ Task {task_id} timed out")


def download_video_task(task_id, url, quality, cookie_browser=None, ratio="original"):
    """Background task to download video using yt-dlp."""
    def _do():
        try:
            import yt_dlp
            download_tasks[task_id]["status"] = "downloading"
            download_tasks[task_id]["phase"] = "fetching"

            output_dir = DOWNLOAD_DIR / task_id
            output_dir.mkdir(exist_ok=True)

            format_map = {
                "4k60": "bestvideo[height>=2160][fps>=60]+bestaudio/bestvideo[height>=2160]+bestaudio/bestvideo+bestaudio/best",
                "4k": "bestvideo[height>=2160]+bestaudio/bestvideo[height>=1440]+bestaudio/bestvideo+bestaudio/best",
                "1440p": "bestvideo[height>=1440]+bestaudio/bestvideo[height>=1080]+bestaudio/bestvideo+bestaudio/best",
                "1080p": "bestvideo[height>=1080]+bestaudio/bestvideo+bestaudio/best",
                "720p": "bestvideo[height>=720]+bestaudio/bestvideo+bestaudio/best",
                "best": "bestvideo+bestaudio/best",
            }
            fmt = format_map.get(quality, format_map["best"])

            ydl_opts = {
                **get_base_ydl_opts(cookie_browser),
                "format": fmt,
                "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
                "progress_hooks": [_make_progress_hook(task_id)],
                "postprocessors": [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
                ],
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e:
                if "cookiefile" in ydl_opts:
                    log.warning(f"⚠️ Video task {task_id} failed with cookies. Retrying WITHOUT cookies... Error: {e}")
                    ydl_opts.pop("cookiefile", None)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                else:
                    raise e

            # Apply aspect ratio crop if needed
            if ratio and ratio != "original":
                files = list(output_dir.glob("*"))
                if files:
                    cropped = crop_to_ratio(str(files[0]), ratio, task_id)
                    # Update file reference

            _finalize_task(task_id, output_dir, info)

        except Exception as e:
            if download_tasks[task_id]["status"] != "error":
                download_tasks[task_id]["status"] = "error"
                download_tasks[task_id]["error"] = str(e)
            log.error(f"❌ Video task {task_id} failed: {e}")

    _run_with_timeout(task_id, _do)


def download_audio_task(task_id, url, audio_format, cookie_browser=None):
    """Background task to download audio using yt-dlp."""
    def _do():
        try:
            import yt_dlp
            download_tasks[task_id]["status"] = "downloading"
            download_tasks[task_id]["phase"] = "fetching"

            output_dir = DOWNLOAD_DIR / task_id
            output_dir.mkdir(exist_ok=True)

            preferred_codec = audio_format if audio_format in (
                "mp3", "flac", "wav", "aac", "opus", "m4a"
            ) else "mp3"

            ydl_opts = {
                **get_base_ydl_opts(cookie_browser),
                "format": "bestaudio/best",
                "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
                "progress_hooks": [_make_progress_hook(task_id)],
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": preferred_codec,
                    "preferredquality": "320",
                }],
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e:
                if "cookiefile" in ydl_opts:
                    log.warning(f"⚠️ Audio task {task_id} failed with cookies. Retrying WITHOUT cookies... Error: {e}")
                    ydl_opts.pop("cookiefile", None)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                else:
                    raise e
            _finalize_task(task_id, output_dir, info)

        except Exception as e:
            if download_tasks[task_id]["status"] != "error":
                download_tasks[task_id]["status"] = "error"
                download_tasks[task_id]["error"] = str(e)
            log.error(f"❌ Audio task {task_id} failed: {e}")

    _run_with_timeout(task_id, _do)


def download_image_task(task_id, url):
    """Background task to download an image from URL."""
    def _do():
        try:
            download_tasks[task_id]["status"] = "downloading"
            download_tasks[task_id]["phase"] = "fetching"

            output_dir = DOWNLOAD_DIR / task_id
            output_dir.mkdir(exist_ok=True)

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            }

            response = requests.get(url, headers=headers, stream=True, timeout=30)
            response.raise_for_status()

            # Check size before downloading
            total_size = int(response.headers.get("content-length", 0))
            if total_size > MAX_FILE_SIZE:
                raise Exception(
                    f"Image too large ({total_size // (1024*1024)}MB). "
                    f"Max: {MAX_FILE_SIZE_MB}MB"
                )

            content_type = response.headers.get("Content-Type", "")
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
            parsed = urlparse(url)
            basename = Path(parsed.path).stem or "image"
            filename = sanitize_filename(basename) + ext
            filepath = output_dir / filename

            downloaded = 0
            download_tasks[task_id]["phase"] = "downloading"
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE:
                        f.close()
                        filepath.unlink(missing_ok=True)
                        raise Exception(f"File exceeded {MAX_FILE_SIZE_MB}MB limit")
                    if total_size > 0:
                        download_tasks[task_id]["progress"] = round(
                            (downloaded / total_size) * 100, 1
                        )

            download_tasks[task_id]["title"] = basename
            _finalize_task(task_id, output_dir)

        except Exception as e:
            if download_tasks[task_id]["status"] != "error":
                download_tasks[task_id]["status"] = "error"
                download_tasks[task_id]["error"] = str(e)
            log.error(f"❌ Image task {task_id} failed: {e}")

    _run_with_timeout(task_id, _do)


# ─── Routes ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)


@app.route("/health")
def health():
    """Health check endpoint."""
    ffmpeg_ok = FFMPEG_PATH and os.path.exists(FFMPEG_PATH)
    cookies_ok = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100
    
    cookie_size = COOKIES_FILE.stat().st_size if COOKIES_FILE.exists() else 0
    youtube_cookie_count = 0
    if COOKIES_FILE.exists():
        try:
            content = COOKIES_FILE.read_text(errors="ignore")
            youtube_cookie_count = sum(1 for line in content.splitlines() if "youtube.com" in line)
        except Exception:
            pass

    active = sum(1 for t in download_tasks.values()
                 if t["status"] in ("queued", "downloading"))
    return jsonify({
        "status": "healthy",
        "uptime": time.time(),
        "ffmpeg": ffmpeg_ok,
        "cookies": cookies_ok,
        "cookie_size": cookie_size,
        "youtube_cookies": youtube_cookie_count,
        "active_downloads": active,
        "total_tasks": len(download_tasks),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
    })


@app.route("/api/info", methods=["POST"])
@limiter.limit("10 per minute")
def get_info():
    """Get media info without downloading."""
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    valid, err = validate_url(url)
    if not valid:
        return jsonify({"error": err}), 400

    try:
        import yt_dlp
        browser = data.get("browser", DEFAULT_COOKIE_BROWSER)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "geo_bypass": True,
            **get_cookie_opts(browser),
        }
        if FFMPEG_DIR:
            ydl_opts["ffmpeg_location"] = FFMPEG_DIR
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            if "cookiefile" in ydl_opts:
                log.warning(f"⚠️ Info fetch failed with cookies for {url}. Retrying WITHOUT cookies... Error: {e}")
                ydl_opts.pop("cookiefile", None)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                raise e
        formats = info.get("formats", [])

        resolutions = set()
        for f in formats:
            h = f.get("height")
            fps = f.get("fps")
            if h:
                label = f"{h}p"
                if fps and fps >= 60:
                    label += "60"
                resolutions.add((h, label))

        resolutions = sorted(resolutions, key=lambda x: x[0], reverse=True)

        # Estimate file size from best format
        filesize_approx = info.get("filesize_approx") or info.get("filesize", 0)

        return jsonify({
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
            "view_count": info.get("view_count", 0),
            "resolutions": [{"height": r[0], "label": r[1]} for r in resolutions],
            "filesize_approx": filesize_approx,
            "url": url,
        })
    except Exception as e:
        log.error(f"Info fetch failed for {url}: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
@limiter.limit("5 per minute")
def start_download():
    """Start a download task."""
    data = request.json
    url = data.get("url", "").strip()
    media_type = data.get("type", "video")
    quality = data.get("quality", "best")
    audio_format = data.get("audioFormat", "mp3")
    ratio = data.get("ratio", "original")
    cookie_browser = data.get("browser", DEFAULT_COOKIE_BROWSER)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    valid, err = validate_url(url)
    if not valid:
        return jsonify({"error": err}), 400

    # Check cache: if same URL+type was already downloaded and file still exists
    cache_key = f"{url}|{media_type}"
    cached_id = url_cache.get(cache_key)
    if cached_id and cached_id in download_tasks:
        cached = download_tasks[cached_id]
        if (cached["status"] == "complete" and
                cached.get("filepath") and os.path.exists(cached.get("filepath", ""))):
            log.info(f"♻️  Cache hit for {url[:60]}... → task {cached_id}")
            return jsonify({"task_id": cached_id, "status": "complete", "cached": True})

    # Check concurrent download limit (max 3)
    active = sum(1 for t in download_tasks.values()
                 if t["status"] in ("queued", "downloading"))
    if active >= 3:
        return jsonify({"error": "Too many active downloads. Please wait."}), 429

    task_id = str(uuid.uuid4())[:8]
    download_tasks[task_id] = {
        "id": task_id,
        "url": url,
        "type": media_type,
        "status": "queued",
        "phase": "queued",
        "progress": 0,
        "speed": 0,
        "eta": 0,
        "filename": "",
        "filepath": "",
        "filesize": 0,
        "title": "",
        "error": "",
        "created_at": time.time(),
        "completed_at": None,
        "expires_in": FILE_TTL,
    }

    log.info(f"📥 New {media_type} download: {url[:80]}... (task={task_id})")
    url_cache[cache_key] = task_id  # Register in cache

    if media_type == "video":
        thread = threading.Thread(
            target=download_video_task,
            args=(task_id, url, quality, cookie_browser, ratio),
        )
    elif media_type == "audio":
        thread = threading.Thread(
            target=download_audio_task,
            args=(task_id, url, audio_format, cookie_browser),
        )
    elif media_type == "image":
        thread = threading.Thread(
            target=download_image_task, args=(task_id, url),
        )
    else:
        return jsonify({"error": f"Unknown media type: {media_type}"}), 400

    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "queued"})


@app.route("/api/status/<task_id>")
def get_status(task_id):
    """Get the status of a download task."""
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    # Add time-to-expiry for completed tasks
    resp = dict(task)
    if task.get("completed_at"):
        elapsed = time.time() - task["completed_at"]
        resp["expires_in"] = max(0, FILE_TTL - int(elapsed))

    return jsonify(resp)


@app.route("/api/download/<task_id>/file")
def download_file(task_id):
    """Download the completed file."""
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task["status"] == "expired":
        return jsonify({"error": "File expired and was deleted"}), 410
    if task["status"] != "complete":
        return jsonify({"error": "Download not complete"}), 400

    filepath = task.get("filepath", "")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found (may have been cleaned up)"}), 404

    log.info(f"📤 Serving file for task {task_id}")
    return send_file(
        filepath,
        as_attachment=True,
        download_name=task.get("filename", "download"),
    )


@app.route("/api/tasks")
def list_tasks():
    """List all download tasks."""
    tasks = sorted(
        download_tasks.values(),
        key=lambda t: t.get("created_at", 0),
        reverse=True,
    )
    return jsonify(tasks[:50])  # Cap at 50 tasks


@app.route("/api/test-clients")
def test_clients():
    """Test different player clients on YouTube to find a working bypass."""
    url = "https://youtu.be/qn5neFBpU40?si=vy9jsvp3saVJkZ0n"
    import yt_dlp
    
    results = {}
    clients_to_test = [
        ["tv"],
        ["android"],
        ["web_embedded"],
        ["default"],
        ["default", "-android_sdkless"],
        ["web_embedded", "tv"],
        ["android", "tv"],
    ]
    
    for client in clients_to_test:
        client_name = ",".join(client)
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "geo_bypass": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": client
                    }
                },
                **get_cookie_opts()
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results[client_name] = {"success": True, "title": info.get("title")}
        except Exception as e:
            results[client_name] = {"success": False, "error": str(e)[:150]}
            
    return jsonify(results)


@app.route("/api/system-status")
def system_status():
    """Check ffmpeg and cookie status."""
    ffmpeg_ok = FFMPEG_PATH and os.path.exists(FFMPEG_PATH)
    cookies_ok = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100
    return jsonify({
        "ffmpeg": ffmpeg_ok,
        "ffmpeg_path": FFMPEG_PATH if ffmpeg_ok else None,
        "cookies_file": cookies_ok,
        "cookies_path": str(COOKIES_FILE) if cookies_ok else None,
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "download_timeout": DOWNLOAD_TIMEOUT,
        "file_ttl": FILE_TTL,
    })


@app.route("/api/upload-cookies", methods=["POST"])
@limiter.limit("3 per minute")
def upload_cookies():
    """Upload a cookies.txt file for authentication."""
    if "file" in request.files:
        f = request.files["file"]
        f.save(str(COOKIES_FILE))
        log.info(f"🍪 Cookies file uploaded ({COOKIES_FILE.stat().st_size} bytes)")
        return jsonify({"success": True, "size": COOKIES_FILE.stat().st_size})

    data = request.get_data(as_text=True)
    if data and len(data) > 50:
        COOKIES_FILE.write_text(data)
        return jsonify({"success": True, "size": len(data)})

    return jsonify({"error": "No cookie data provided"}), 400


# ─── Error Handlers ───────────────────────────────────────────────────

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Rate limit exceeded. Please slow down.",
        "retry_after": e.description,
    }), 429


@app.errorhandler(500)
def internal_error(e):
    log.error(f"Internal error: {e}")
    return jsonify({"error": "Internal server error"}), 500


# ─── Startup ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    ffmpeg_ok = FFMPEG_PATH and os.path.exists(FFMPEG_PATH)
    cookies_ok = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100
    port = int(os.environ.get("PORT", 5000))

    print(f"\n  ⬇  MediaGrab running at http://localhost:{port}")
    print(f"  {'✓' if ffmpeg_ok else '✗'}  ffmpeg: {'found' if ffmpeg_ok else 'NOT FOUND'}")
    print(f"  {'✓' if cookies_ok else '✗'}  cookies.txt: {'loaded' if cookies_ok else 'not found'}")
    print(f"  ⚙  Max file size: {MAX_FILE_SIZE_MB}MB | Timeout: {DOWNLOAD_TIMEOUT}s | TTL: {FILE_TTL}s")
    print(f"  ⚙  Rate limits: 5 downloads/min, 10 info/min")
    print(f"  ⚙  Auto-cleanup: every {CLEANUP_INTERVAL}s\n")

    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "1") == "1")
