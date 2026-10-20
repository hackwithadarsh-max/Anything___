#!/usr/bin/env python3
"""AnyThing Downloader backend: Flask + yt-dlp.

Run:  python backed.py   ->  http://127.0.0.1:5000  (also serves index.html)
Deps: pip install flask flask-cors yt-dlp   (optional: waitress, ffmpeg)
"""
import hashlib, ipaddress, logging, mimetypes, os, re, shutil, socket, tempfile, threading, time
import urllib.error, urllib.request
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, Response, after_this_request, jsonify, request, send_file, send_from_directory, stream_with_context
from flask_cors import CORS

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "40"))      # API calls / minute / IP
CACHE_TTL, CACHE_MAX = 600, 128
HAS_FFMPEG = shutil.which("ffmpeg") is not None
BASE_DIR = Path(__file__).resolve().parent
IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anything")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # API only needs a small JSON URL payload
CORS(app, expose_headers=["Content-Disposition", "Content-Length"])

YDL_BASE = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 3, "nocheckcertificate": False}
cache, cache_lock = OrderedDict(), threading.Lock()
hits = defaultdict(deque)
server_slots = threading.Semaphore(2)               # max parallel server-side downloads


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def on_api_error(e):
    return jsonify(success=False, error=e.message), e.status


@app.errorhandler(Exception)
def on_unexpected(e):
    code = getattr(e, "code", 500) if isinstance(getattr(e, "code", None), int) else 500
    if code == 500:
        log.exception("Unhandled error")
    return jsonify(success=False, error="Server error" if code == 500 else str(e)), code


@app.before_request
def rate_limit():
    if not request.path.startswith("/api/") or request.path == "/api/health" or request.headers.get("Range"):
        return
    now, q = time.time(), hits[request.remote_addr]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise ApiError("Too many requests. Please wait a minute.", 429)
    q.append(now)


# ---------- helpers ----------
def is_public_host(host):
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (socket.gaierror, ValueError):
        return False


def validate_url(url):
    url = (url or "").strip()
    p = urlparse(url)
    if len(url) > 2048 or p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError("Please enter a valid http/https link.")
    if not is_public_host(p.hostname):
        raise ApiError("This address is not allowed.")
    return url


def clean_err(msg):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "Could not extract this media."


def safe_filename(title, ext):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "").strip(" .")[:100] or "media"
    return f"{name}.{ext}"


def get_info(url):
    key = hashlib.sha1(url.encode()).hexdigest()
    with cache_lock:
        hit = cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            cache.move_to_end(key)
            return hit[1]
    try:
        with yt_dlp.YoutubeDL({**YDL_BASE, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ApiError(clean_err(str(e)), 422)
    if info and info.get("_type") == "playlist":
        info = next((x for x in (info.get("entries") or []) if x), None)
    if not info:
        raise ApiError("No media found at this link.", 404)
    with cache_lock:
        cache[key] = (time.time(), info)
        while len(cache) > CACHE_MAX:
            cache.popitem(last=False)
    return info


def fid_of(f):
    return str(f.get("format_id") or "direct")


def list_formats(info):
    fl = info.get("formats") or [info]
    usable = [f for f in fl if f.get("url") and f.get("protocol") in ("http", "https") and f.get("ext") != "mhtml"]
    videos, audio, images = {}, None, []
    for f in usable:
        has_v, has_a = f.get("vcodec") != "none", f.get("acodec") != "none"
        if has_v and has_a and (f.get("ext") or "") not in IMG_EXT:
            h = f.get("height") or 0
            if h not in videos or (f.get("tbr") or 0) > (videos[h].get("tbr") or 0):
                videos[h] = f
        elif has_a and not has_v:
            if not audio or (f.get("abr") or f.get("tbr") or 0) > (audio.get("abr") or audio.get("tbr") or 0):
                audio = f
        elif (f.get("ext") or "") in IMG_EXT:
            images.append(f)

    def item(f, typ, label):
        return {"id": fid_of(f), "label": label, "ext": f.get("ext") or "mp4", "kind": "direct",
                "type": typ, "size": f.get("filesize") or f.get("filesize_approx")}

    out = [item(videos[h], "video", f"{h}p" if h else "Video") for h in sorted(videos, reverse=True)[:5]]
    if audio:
        out.append(item(audio, "audio", f"Audio {(audio.get('ext') or '').upper()}".strip()))
    out += [item(f, "image", "Image") for f in images[:1]]
    best_h, got_h = max((f.get("height") or 0 for f in fl), default=0), max(videos, default=0)
    if not out or (HAS_FFMPEG and best_h > got_h):
        out.insert(0, {"id": "srv:best", "label": "Best quality" + (" (HD merged)" if HAS_FFMPEG else ""),
                       "ext": "mp4", "kind": "server", "type": "video", "size": None})
    if HAS_FFMPEG:
        out.append({"id": "srv:mp3", "label": "Audio MP3", "ext": "mp3", "kind": "server", "type": "audio", "size": None})
    return out


def proxy(f, title, inline):
    """Stream the media through this server so the browser downloads it directly."""
    headers = dict(f.get("http_headers") or {})
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        up = urllib.request.urlopen(urllib.request.Request(f["url"], headers=headers), timeout=25)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return Response(status=416)
        raise ApiError(f"Source server refused the download ({e.code}).", 502)
    except Exception as e:
        raise ApiError(f"Could not reach the media server: {e}", 502)

    ext = f.get("ext") or "mp4"
    filename = safe_filename(title, ext)
    ascii_name = filename.encode("ascii", "ignore").decode() or f"media.{ext}"
    out = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; "
                                  f"filename*=UTF-8''{quote(filename)}",
           "Cache-Control": "no-store", "Accept-Ranges": up.headers.get("Accept-Ranges", "bytes")}
    for k in ("Content-Length", "Content-Range"):
        if up.headers.get(k):
            out[k] = up.headers[k]
    ctype = up.headers.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def stream():
        try:
            while chunk := up.read(128 * 1024):
                yield chunk
        finally:
            up.close()

    log.info("proxy %s (%s)", filename, ctype)
    return Response(stream_with_context(stream()), status=up.status, headers=out, content_type=ctype)


def server_download(url, mode):
    """Fallback: yt-dlp downloads/merges/converts to a temp file, then we send it."""
    if not server_slots.acquire(blocking=False):
        raise ApiError("Server is busy, try again in a moment.", 503)
    tmp = tempfile.mkdtemp(prefix="anything_")
    opts = {**YDL_BASE, "outtmpl": os.path.join(tmp, "%(title).80B.%(ext)s")}
    if mode == "mp3" and HAS_FFMPEG:
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    elif HAS_FFMPEG:
        opts.update(format="bv*+ba/b", merge_output_format="mp4")
    else:
        opts.update(format="b")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in Path(tmp).iterdir() if p.is_file()]
        if not files:
            raise ApiError("Download produced no file.", 500)
        path = max(files, key=lambda p: p.stat().st_size)
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ApiError(clean_err(str(e)), 422)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        server_slots.release()

    @after_this_request
    def cleanup(resp):
        resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return resp

    return send_file(path, as_attachment=True, download_name=path.name)


# ---------- routes ----------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        service="AnyThing Downloader",
        ffmpeg=HAS_FFMPEG,
        yt_dlp=yt_dlp.version.__version__,
    )


@app.post("/api/extract")
def extract():
    data = request.get_json(silent=True) or {}
    url = validate_url(data.get("url"))
    info = get_info(url)
    log.info("extract %s -> %s", url, info.get("title"))
    return jsonify(success=True, title=info.get("title") or "Downloaded Media",
                   thumbnail=info.get("thumbnail") or "", duration=info.get("duration"),
                   uploader=info.get("uploader") or info.get("channel") or "",
                   platform=info.get("extractor_key") or "", formats=list_formats(info))


@app.get("/api/download")
def download():
    url = validate_url(request.args.get("url"))
    fid, inline = request.args.get("format", ""), request.args.get("inline") == "1"
    if fid.startswith("srv:"):
        if inline:
            raise ApiError("Preview not available for this format.")
        return server_download(url, fid[4:])
    info = get_info(url)
    f = next((x for x in (info.get("formats") or [info]) if x.get("url") and fid_of(x) == fid), None)
    if not f:
        raise ApiError("Format not found. Please preview the link again.", 404)
    return proxy(f, info.get("title"), inline)


if __name__ == "__main__":
    log.info("AnyThing backend on http://%s:%s (ffmpeg: %s)", HOST, PORT, HAS_FFMPEG)
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
