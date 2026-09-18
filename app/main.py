import os
import re
import uuid
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp


app = FastAPI(title="AllDownloader API")

# Allow Netlify frontend to communicate with Render backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        raise HTTPException(
            status_code=400,
            detail="Only HTTP/HTTPS URLs are allowed"
        )

    host = parsed.hostname

    if not host:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL"
        )

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
                raise HTTPException(
                    status_code=400,
                    detail="Private URLs are not allowed"
                )

    except socket.gaierror:
        raise HTTPException(
            status_code=400,
            detail="Unable to resolve URL"
        )


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

    def extract_info():

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    try:

        info = await asyncio.to_thread(extract_info)

        qualities = {}
        formats = info.get("formats", [])

        for fmt in formats:

            height = fmt.get("height")

            if not height:
                continue

            if fmt.get("vcodec") == "none":
                continue

            qualities[height] = {
                "height": height,
                "ext": fmt.get("ext", "mp4"),
                "format_id": fmt.get("format_id")
            }

        available = sorted(
            qualities.values(),
            key=lambda x: x["height"],
            reverse=True
        )

        return {
            "title": info.get("title", "video"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": available
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
        raise HTTPException(
            status_code=400,
            detail="Invalid media type"
        )

    quality = str(request.quality).lower().strip()

    if request.media == "audio":

        format_selector = "bestaudio/best"

    elif quality == "best":

        format_selector = "bestvideo+bestaudio/best"

    elif quality.isdigit():

        format_selector = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]"
        )

    else:

        format_selector = "bestvideo+bestaudio/best"

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

        # Faster downloading
        "concurrent_fragment_downloads": 8,

        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,

        "format": format_selector,

        "merge_output_format": "mp4",
    }

    if request.media == "audio":

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    def perform_download():

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(
                url,
                download=True
            )

    try:

        await asyncio.to_thread(perform_download)

        downloaded_files = []

        for filename in os.listdir(DOWNLOAD_DIR):

            if filename.startswith(file_id + "_"):

                downloaded_files.append(
                    os.path.join(
                        DOWNLOAD_DIR,
                        filename
                    )
                )

        if not downloaded_files:

            raise Exception(
                "Downloaded file was not found"
            )

        filepath = downloaded_files[0]

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
