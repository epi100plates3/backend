"""
YouTube Downloader API - Backend (FastAPI + yt-dlp)
NO FFMPEG - Progressive MP4 streams only.
"""

import os
import re
import time
import tempfile
import asyncio
from pathlib import Path
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import yt_dlp

# ─── Simple In-Memory Rate Limiter ──────────────────────────────────────────

class RateLimiter:
    """Simple per-IP rate limiter (no external deps)."""
    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str, max_requests: int, window_seconds: int = 60) -> bool:
        now = time.time()
        bucket = self._buckets[ip]
        # Remove old entries
        while bucket and bucket[0] < now - window_seconds:
            bucket.pop(0)
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        return True

rate_limiter = RateLimiter()

def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── App Init ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="YT Downloader API",
    description="YouTube progressive MP4 downloader (no ffmpeg)",
    version="1.0.0",
)

# CORS – allow frontend domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://epii.pl", "http://localhost", "http://127.0.0.1"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str  # e.g. "18" (360p), "22" (720p)


# ─── Constants ────────────────────────────────────────────────────────────────

# YouTube progressive MP4 formats (audio + video in one file):
#   18  = 360p  MP4 (H.264 + AAC)
#   22  = 720p  MP4 (H.264 + AAC)
#   37  = 1080p MP4 (H.264 + AAC)
#   59  = 480p  MP4 (H.264 + AAC)
#   78  = 480p  MP4 (H.264 + AAC)
PROGRESSIVE_FORMATS = {"18": "360p", "22": "720p", "37": "1080p", "59": "480p", "78": "480p"}

# Cap at 720p to keep files manageable
MAX_FORMAT_ID = "22"

# Max file size: ~500 MB
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024

# Download timeout (seconds)
DOWNLOAD_TIMEOUT = 120

# Temp file directory
TEMP_DIR = Path(tempfile.gettempdir()) / "ytdown_tmp"
TEMP_DIR.mkdir(exist_ok=True)

# Cookies file path (for YouTube bot detection)
# Place a cookies.txt file (Netscape format) next to main.py
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
if COOKIES_FILE.exists():
    print(f"[INFO] Using cookies file: {COOKIES_FILE}")
else:
    print("[WARN] No cookies.txt found. YouTube may block requests from cloud IPs.")
    COOKIES_FILE = None

def _cookies_opts() -> dict:
    """Return cookies option dict if cookies file exists."""
    if COOKIES_FILE and COOKIES_FILE.exists():
        return {"cookiefile": str(COOKIES_FILE)}
    return {}

# yt-dlp options for info extraction only
# NOTE: No "format" filter here! We extract ALL formats then filter in Python.
YTDL_INFO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,       # Minimal extraction, skips player responses
    "skip_download": True,
    "socket_timeout": 30,
    **_cookies_opts(),
}

# yt-dlp options for actual download
YTDL_DOWNLOAD_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": DOWNLOAD_TIMEOUT,
    "retries": 3,
    "fragment_retries": 3,
    "nocheckcertificate": True,
    **_cookies_opts(),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_valid_youtube_url(url: str) -> bool:
    """Validate that the URL is a YouTube watch URL."""
    patterns = [
        r"^(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]{11}",
        r"^(https?://)?(www\.)?youtu\.be/[\w-]{11}",
        r"^(https?://)?(www\.)?youtube\.com/shorts/[\w-]{11}",
        r"^(https?://)?(www\.)?m\.youtube\.com/watch\?v=[\w-]{11}",
    ]
    return any(re.match(p, url) for p in patterns)


async def get_info(url: str) -> dict:
    """Extract video info using yt-dlp (no download)."""
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_INFO_OPTS) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _extract), timeout=60
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request to YouTube timed out")
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Video unavailable" in msg:
            raise HTTPException(status_code=404, detail="Video unavailable or private")
        raise HTTPException(status_code=400, detail=f"YouTube error: {msg[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)[:200]}")

    # Find available progressive MP4 formats
    available_mp4 = []
    for fmt in info.get("formats", []):
        fid = fmt.get("format_id", "")
        ext = fmt.get("ext", "")
        acodec = fmt.get("acodec", "none")
        vcodec = fmt.get("vcodec", "none")

        # Only include progressive MP4 (both audio and video, mp4 container)
        if ext == "mp4" and acodec != "none" and vcodec != "none":
            # Cap at 720p (format 22)
            if fid in PROGRESSIVE_FORMATS and int(fid) <= 22:
                available_mp4.append({
                    "format_id": fid,
                    "quality": PROGRESSIVE_FORMATS[fid],
                    "resolution": fmt.get("resolution", "unknown"),
                    "filesize": fmt.get("filesize"),
                    "filesize_approx": fmt.get("filesize_approx"),
                    "fps": fmt.get("fps"),
                })

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "formats": available_mp4,
    }


async def download_video(url: str, format_id: str) -> Path:
    """Download a specific progressive MP4 format."""
    loop = asyncio.get_event_loop()

    # Sanitize format_id for filename
    outtmpl = str(TEMP_DIR / f"%(title).100s_{format_id}.%(ext)s")

    opts = {
        **YTDL_DOWNLOAD_OPTS_BASE,
        "format": format_id,  # exact progressive format
        "outtmpl": outtmpl,
        "merge_output_format": None,  # no merging!
        "postprocessors": [],  # no post-processing!
    }

    def _download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find the downloaded file
            filename = ydl.prepare_filename(info)
            # The actual file may have .mp4 extension
            actual = Path(filename)
            if not actual.exists():
                # yt-dlp may change extension
                actual = actual.with_suffix(".mp4")
            if not actual.exists():
                # search temp dir
                candidates = list(TEMP_DIR.glob(f"*{format_id}*.mp4"))
                if candidates:
                    actual = candidates[0]
                else:
                    candidates = list(TEMP_DIR.glob("*.mp4"))
                    if candidates:
                        # newest file
                        actual = max(candidates, key=lambda p: p.stat().st_mtime)
            return actual

    try:
        filepath = await asyncio.wait_for(
            loop.run_in_executor(None, _download), timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Download timed out")
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "requested format" in msg and "not available" in msg:
            raise HTTPException(
                status_code=400,
                detail="This quality is not available as progressive MP4. Try a lower quality."
            )
        raise HTTPException(status_code=400, detail=f"Download error: {str(e)[:200]}")

    if not filepath or not filepath.exists():
        raise HTTPException(status_code=500, detail="Download failed – file not found")

    file_size = filepath.stat().st_size
    if file_size > MAX_FILE_SIZE_BYTES:
        filepath.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="File too large (>500 MB)")

    return filepath


def cleanup_old_files(max_age_seconds: int = 600):
    """Remove temp files older than max_age_seconds (default 10 min)."""
    now = Path(TEMP_DIR).stat().st_mtime if TEMP_DIR.exists() else 0
    for f in TEMP_DIR.glob("*.mp4"):
        try:
            age = now - f.stat().st_mtime
            if age > max_age_seconds:
                f.unlink(missing_ok=True)
        except Exception:
            pass


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint for Render.com."""
    return {"status": "ok", "service": "ytdown-api"}


@app.get("/debug")
async def debug_info():
    """Debug endpoint – shows cookies status, paths, yt-dlp version."""
    import yt_dlp.version
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "cookies_file": str(COOKIES_FILE) if COOKIES_FILE else None,
        "cookies_exists": COOKIES_FILE.exists() if COOKIES_FILE else False,
        "cookies_opts": _cookies_opts(),
        "cwd": os.getcwd(),
        "main_py_dir": str(Path(__file__).parent),
    }


@app.post("/api/info")
async def video_info(request: Request, body: URLRequest):
    """
    Get video info: title, thumbnail, available progressive MP4 formats.
    """
    # Rate limit check
    ip = get_client_ip(request)
    if not rate_limiter.check(ip, 10, 60):
        return JSONResponse(
            status_code=429,
            content={"error": "Too many requests. Try again later."},
        )

    url = body.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    if not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    info = await get_info(url)

    if not info["formats"]:
        raise HTTPException(
            status_code=404,
            detail="No progressive MP4 formats available for this video. "
                    "The video may be too new or available only in adaptive formats."
        )

    return {
        "success": True,
        **info,
    }


@app.post("/api/download")
async def download_video_endpoint(request: Request, body: DownloadRequest):
    """
    Download a video in the selected progressive MP4 format.
    Returns the file as a streaming response.
    """
    # Rate limit check
    ip = get_client_ip(request)
    if not rate_limiter.check(ip, 5, 60):
        return JSONResponse(
            status_code=429,
            content={"error": "Too many requests. Try again later."},
        )

    url = body.url.strip()
    format_id = body.format_id.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not format_id:
        raise HTTPException(status_code=400, detail="Format ID is required")

    if not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    if format_id not in PROGRESSIVE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format. Available: {list(PROGRESSIVE_FORMATS.keys())}"
        )

    # Disallow formats above 720p
    if int(format_id) > int(MAX_FORMAT_ID):
        raise HTTPException(
            status_code=400,
            detail="Max supported quality is 720p (format 22)"
        )

    # Cleanup old files first
    cleanup_old_files()

    filepath = await download_video(url, format_id)

    # Extract a safe filename
    safe_title = re.sub(r'[\\/*?:"<>|]', "", filepath.stem[:80])
    download_name = f"{safe_title}.mp4"

    return FileResponse(
        path=str(filepath),
        media_type="video/mp4",
        filename=download_name,
        background=None,  # Let FastAPI handle cleanup
    )


# ─── Periodic Cleanup on Startup ──────────────────────────────────────────────

@app.on_event("startup")
async def startup_cleanup():
    """Clean up any leftover temp files on startup."""
    for f in TEMP_DIR.glob("*.mp4"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    for f in TEMP_DIR.glob("*.part"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    for f in TEMP_DIR.glob("*.ytdl"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
