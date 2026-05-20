"""
YouTube Downloader API - Backend (FastAPI + Invidious + ffmpeg)
Uses Invidious to get stream URLs, ffmpeg to merge video+audio.
Supports up to 1080p.
"""

import re
import time
import httpx
import asyncio
import tempfile
import subprocess
from pathlib import Path
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel


# ─── Rate Limiter ──────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str, max_requests: int, window_seconds: int = 60) -> bool:
        now = time.time()
        bucket = self._buckets[ip]
        while bucket and bucket[0] < now - window_seconds:
            bucket.pop(0)
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        return True


rate_limiter = RateLimiter()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Invidious ─────────────────────────────────────────────────────────────

INVIDIOUS_INSTANCES = [
    "https://invidious.slipfox.xyz",
    "https://invidious.darkness.services",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
]

PROGRESSIVE_ITAGS = {"18": "360p", "22": "720p"}

# Common adaptive itags → quality label
RESOLUTION_MAP = {
    "137": "1080p", "136": "720p", "135": "480p", "134": "360p",
    "133": "240p", "160": "144p",
    "247": "720p", "248": "1080p",  # webm
    "298": "720p", "299": "1080p",  # mp4 (no audio)
    "399": "1080p", "398": "720p", "397": "480p", "396": "360p",  # av01
}


# ─── App ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="YT Downloader API",
    description="YouTube downloader with ffmpeg merging (Invidious proxy)",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://epii.pl", "http://localhost", "http://127.0.0.1"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Models ────────────────────────────────────────────────────────────────

class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    itag: str  # video itag (e.g. "137" for 1080p)


# ─── Helpers ───────────────────────────────────────────────────────────────

def is_valid_youtube_url(url: str) -> bool:
    patterns = [
        r"^(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]{11}",
        r"^(https?://)?(www\.)?youtu\.be/[\w-]{11}",
        r"^(https?://)?(www\.)?youtube\.com/shorts/[\w-]{11}",
        r"^(https?://)?(www\.)?m\.youtube\.com/watch\?v=[\w-]{11}",
    ]
    return any(re.match(p, url) for p in patterns)


def extract_video_id(url: str) -> str:
    for p in [
        r"(?:v=|youtu\.be/|shorts/)([\w-]{11})",
        r"m\.youtube\.com/watch\?v=([\w-]{11})",
    ]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return ""


async def fetch_invidious(video_id: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        async with httpx.AsyncClient(timeout=20) as client:
            for instance in INVIDIOUS_INSTANCES:
                try:
                    r = await client.get(
                        f"{instance}/api/v1/videos/{video_id}",
                        headers={"User-Agent": "ytdown/3.0"},
                    )
                    if r.status_code == 200:
                        return r.json()
                except Exception:
                    continue
        if attempt < retries - 1:
            await asyncio.sleep(2)  # Wait before retry
    return {"error": "all instances unreachable"}


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    # Check ffmpeg
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        ffmpeg_ok = result.returncode == 0
    except Exception:
        ffmpeg_ok = False
    return {"status": "ok", "service": "ytdown-api", "version": "3.0.0", "ffmpeg": ffmpeg_ok}


@app.get("/debug")
async def debug():
    async with httpx.AsyncClient(timeout=10) as client:
        results = {}
        for inst in INVIDIOUS_INSTANCES:
            try:
                r = await client.get(f"{inst}/api/v1/stats")
                results[inst] = "ok" if r.status_code == 200 else f"status:{r.status_code}"
            except Exception as e:
                results[inst] = f"error:{str(e)[:50]}"
    return {"instances": results}


@app.post("/api/info")
async def video_info(request: Request, body: URLRequest):
    ip = get_client_ip(request)
    if not rate_limiter.check(ip, 10, 60):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    url = body.url.strip()
    if not url or not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Could not extract video ID")

    data = await fetch_invidious(vid)
    if "error" in data:
        raise HTTPException(status_code=502, detail="All Invidious instances down")

    # Build format list: progressive MP4 + adaptive video with audio
    formats = []

    # Progressive (audio+video in one)
    for s in data.get("formatStreams", []):
        itag = str(s.get("itag", ""))
        if itag in PROGRESSIVE_ITAGS:
            formats.append({
                "itag": itag,
                "quality": PROGRESSIVE_ITAGS[itag],
                "resolution": s.get("qualityLabel", "?"),
                "fps": s.get("fps"),
                "type": "progressive",
                "size": s.get("clen"),
            })

    # Adaptive video (needs merging with audio)
    audio_available = any(
        str(s.get("itag", "")) in ("140", "251")
        for s in data.get("adaptiveFormats", [])
    )

    for s in data.get("adaptiveFormats", []):
        itag = str(s.get("itag", ""))
        label = s.get("qualityLabel", "") or RESOLUTION_MAP.get(itag, "")
        # Only include video-only formats (not audio-only)
        if label and s.get("type","").startswith("video"):
            # Deduplicate by resolution
            if not any(f["itag"] == itag for f in formats):
                formats.append({
                    "itag": itag,
                    "quality": label,
                    "resolution": label,
                    "fps": s.get("fps"),
                    "type": "adaptive",
                    "size": s.get("clen"),
                    "needs_audio": audio_available,
                })

    # Sort: progressive first, then by resolution (higher first)
    def sort_key(f):
        nums = "".join(c for c in f["quality"] if c.isdigit())
        return (0 if f["type"] == "progressive" else 1, -int(nums) if nums else 0)

    formats.sort(key=sort_key)

    thumb = ""
    thumbs = data.get("videoThumbnails") or []
    for t in thumbs:
        if t.get("quality") == "maxres":
            thumb = t["url"]
            break
    if not thumb and thumbs:
        thumb = thumbs[0]["url"]
    if thumb and thumb.startswith("/"):
        thumb = "https://i.ytimg.com" + thumb

    return {
        "success": True,
        "title": data.get("title", "Unknown"),
        "thumbnail": thumb,
        "duration": data.get("lengthSeconds", 0),
        "uploader": data.get("author", "Unknown"),
        "formats": formats,
    }


@app.post("/api/download")
async def download_video(request: Request, body: DownloadRequest):
    ip = get_client_ip(request)
    if not rate_limiter.check(ip, 5, 60):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    url = body.url.strip()
    itag = body.itag.strip()

    if not url or not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Could not extract video ID")

    data = await fetch_invidious(vid)
    if "error" in data:
        raise HTTPException(status_code=502, detail="Invidious instances down")

    title = (data.get("title") or "video").replace("/", "_")[:60]

    # Find video stream
    video_url = ""
    video_itag = ""
    all_streams = data.get("formatStreams", []) + data.get("adaptiveFormats", [])
    for s in all_streams:
        if str(s.get("itag")) == itag:
            video_url = s.get("url", "")
            video_itag = itag
            break

    if not video_url:
        raise HTTPException(status_code=404, detail="Requested format not available")

    # Check if this is a progressive format (no audio merging needed)
    is_progressive = itag in PROGRESSIVE_ITAGS

    if is_progressive:
        # Progressive MP4 – return direct URL
        return {"success": True, "url": video_url, "filename": f"{title}.mp4"}

    # Adaptive video – need to merge with audio
    # Find best audio stream (itag 140 = AAC 128k, itag 251 = Opus 160k)
    audio_url = ""
    for s in data.get("adaptiveFormats", []):
        if str(s.get("itag")) in ("140", "251") and s.get("type","").startswith("audio"):
            audio_url = s.get("url", "")
            break

    if not audio_url:
        raise HTTPException(status_code=404, detail="No audio stream available for merging")

    # Download video and audio to temp files
    tmpdir = Path(tempfile.gettempdir()) / "ytdown_tmp"
    tmpdir.mkdir(exist_ok=True)

    video_file = tmpdir / f"video_{vid}_{itag}.mp4"
    audio_file = tmpdir / f"audio_{vid}.m4a"
    output_file = tmpdir / f"{title}.mp4"

    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            # Download video
            async with client.stream("GET", video_url) as resp:
                with open(video_file, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)

            # Download audio
            async with client.stream("GET", audio_url) as resp:
                with open(audio_file, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)

        # Merge with ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(audio_file),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_file),
        ]

        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        )

        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"ffmpeg merge failed: {proc.stderr[:200]}",
            )

        # Clean up inputs
        video_file.unlink(missing_ok=True)
        audio_file.unlink(missing_ok=True)

        return FileResponse(
            path=str(output_file),
            media_type="video/mp4",
            filename=f"{title}.mp4",
            background=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        # Cleanup on error
        for f in [video_file, audio_file, output_file]:
            f.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)[:200]}")


# ─── Startup Cleanup ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    tmpdir = Path(tempfile.gettempdir()) / "ytdown_tmp"
    tmpdir.mkdir(exist_ok=True)
    for f in tmpdir.glob("*.mp4"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    for f in tmpdir.glob("*.m4a"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    print("[INFO] YT Downloader API v3.0 – Invidious + ffmpeg")
