import os
import re
import uuid
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
import yt_dlp


app = FastAPI(title="AllDownloader API")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class DownloadRequest(BaseModel):
    url: HttpUrl
    media: str = "video"
    quality: str = "best"


def clean_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name.strip("_")[:160] or "download"


def validate_public_url(url: str):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only HTTP/HTTPS URLs are allowed")

    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="Invalid URL")

    try:
        addresses = socket.getaddrinfo(host, None)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise HTTPException(status_code=400, detail="Private URLs are not allowed")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Unable to resolve URL")


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "AllDownloader API"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/info")
async def get_info(request: DownloadRequest):
    url = str(request.url)
    validate_public_url(url)

    def extract():
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(extract)

        formats = []

        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec") != "none":
                formats.append({
                    "height": height,
                    "ext": f.get("ext"),
                    "format_id": f.get("format_id")
                })

        unique = {}
        for item in formats:
            unique[item["height"]] = item

        qualities = sorted(unique.values(), key=lambda x: x["height"], reverse=True)

        return {
            "title": info.get("title", "video"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": qualities
        }

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not detect video information: {str(e)}"
        )


@app.post("/api/download")
async def download(request: DownloadRequest):
    url = str(request.url)
    validate_public_url(url)

    if request.media not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Invalid media type")

    allowed_quality = {
        "best": "bestvideo+bestaudio/best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    }

    quality = request.quality

    if quality.isdigit():
        format_selector = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]"
        )
    else:
        format_selector = allowed_quality.get(
            quality,
            allowed_quality["best"]
        )

    file_id = uuid.uuid4().hex
    output_template = os.path.join(
        DOWNLOAD_DIR,
        f"{file_id}_%(title)s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "format": format_selector,
    }

    if request.media == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    def perform_download():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return info

    try:
        info = await asyncio.to_thread(perform_download)

        files = [
            os.path.join(DOWNLOAD_DIR, f)
            for f in os.listdir(DOWNLOAD_DIR)
            if f.startswith(file_id + "_")
        ]

        if not files:
            raise Exception("Downloaded file was not found")

        filepath = files[0]
        filename = os.path.basename(filepath)

        return FileResponse(
            filepath,
            filename=filename,
            media_type="application/octet-stream"
        )

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Download failed: {str(e)}"
                                     )
