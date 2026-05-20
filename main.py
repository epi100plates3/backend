"""
YouTube Downloader API - Backend (FastAPI + Invidious Proxy)
NO FFMPEG - Progressive MP4 streams only.
Uses public Invidious instances to bypass Render.com IP blocking.
"""

import re
import time
import httpx
from pathlib import Path
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
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


# ─── Invidious Instances ───────────────────────────────────────────────────

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.fdn.fr",
    "https://yewtu.be",
    "https://vid.puffyan.us",
    "https://invidious.privacyredirect.com",
    "https://yt.artemislena.eu",
    "https://invidious.nerdvpn.de",
]


# ─── App Init ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="YT Downloader API",
    description="YouTube progressive MP4 downloader (no ffmpeg, Invidious proxy)",
    version="2.0.0",
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
    format_id: str


# ─── Constants ─────────────────────────────────────────────────────────────

PROGRESSIVE_FORMATS = {"18": "360p", "22": "720p"}


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


async def fetch_invidious(video_id: str) -> dict:
    """Try Invidious instances until one responds."""
    async with httpx.AsyncClient(timeout=15) as client:
        for instance in INVIDIOUS_INSTANCES:
            try:
                r = await client.get(
                    f"{instance}/api/v1/videos/{video_id}",
                    headers={"User-Agent": "ytdown/2.0"},
                )
                if r.status_code == 200:
                    return r.json()
            except Exception:
                continue
    return {"error": "all instances unreachable"}


def find_progressive_mp4(streams: list[dict]) -> list[dict]:
    """Extract progressive MP4 formats (itags 18, 22)."""
    result = []
    for s in streams:
        itag = str(s.get("itag", ""))
        if itag in PROGRESSIVE_FORMATS and s.get("container") == "mp4":
            result.append({
                "format_id": itag,
                "quality": PROGRESSIVE_FORMATS[itag],
                "resolution": s.get("qualityLabel", s.get("resolution", "?")),
                "filesize": s.get("clen"),
                "fps": s.get("fps"),
                "url": s.get("url", ""),
            })
    return result


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ytdown-api", "version": "2.0.0"}


@app.get("/debug")
async def debug():
    """Test Invidious connectivity."""
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
        raise HTTPException(status_code=502, detail="All Invidious instances are down. Try later.")

    formats = find_progressive_mp4(
        data.get("formatStreams", []) + data.get("adaptiveFormats", [])
    )

    if not formats:
        raise HTTPException(status_code=404, detail="No progressive MP4 formats available")

    thumb = ""
    thumbs = data.get("videoThumbnails") or []
    for t in thumbs:
        if t.get("quality") == "maxres":
            thumb = t["url"]
            break
    if not thumb and thumbs:
        thumb = thumbs[0]["url"]

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
    fmt_id = body.format_id.strip()

    if not url or not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    if fmt_id not in PROGRESSIVE_FORMATS:
        raise HTTPException(status_code=400, detail="Unsupported format (use 18 or 22)")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Could not extract video ID")

    data = await fetch_invidious(vid)
    if "error" in data:
        raise HTTPException(status_code=502, detail="Invidious instances down")

    for s in data.get("formatStreams", []) + data.get("adaptiveFormats", []):
        if str(s.get("itag")) == fmt_id:
            dl_url = s.get("url", "")
            if dl_url:
                return RedirectResponse(url=dl_url)

    raise HTTPException(status_code=404, detail="Format not available")
