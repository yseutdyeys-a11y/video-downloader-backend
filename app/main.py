import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


app = FastAPI(title="All Video Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/tmp/downloads")
)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    media: str = "video"
    quality: str = "best"


def valid_url(raw):
    u = raw.strip()
    p = urlparse(u)

    if p.scheme not in {"http", "https"} or not p.hostname:
        raise HTTPException(
            400,
            "Enter a valid http/https URL."
        )

    host = p.hostname.lower().rstrip(".")

    if (
        host in {
            "localhost",
            "localhost.localdomain",
            "metadata.google.internal",
        }
        or host.endswith(".local")
    ):
        raise HTTPException(
            400,
            "Host not allowed."
        )

    try:
        addresses = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM
        )

        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])

            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise HTTPException(
                    400,
                    "Private/internal address not allowed."
                )

    except socket.gaierror:
        raise HTTPException(
            400,
            "Host could not be resolved."
        )

    return u


def clean(n):
    return (
        re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            n
        ).strip("_")[:160]
        or "download"
    )


@app.get("/")
async def root():
    return {
        "service": "All Video Downloader API",
        "status": "ok"
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# -----------------------------
# QUALITY DETECTION
# -----------------------------

@app.post("/api/info")
async def info(x: DownloadRequest):

    u = valid_url(x.url)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    def extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(
                u,
                download=False
            )

    try:
        data = await asyncio.to_thread(extract)

        found = {}

        for f in data.get("formats", []):

            height = f.get("height")

            if not height:
                continue

            # Only formats containing video
            if f.get("vcodec") == "none":
                continue

            height = int(height)

            found[height] = {
                "height": height,
                "label": f"{height}p"
            }

        qualities = sorted(
            found.values(),
            key=lambda item: item["height"],
            reverse=True
        )

        return {
            "title": data.get("title", "video"),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "qualities": qualities
        }

    except Exception as e:

        raise HTTPException(
            400,
            "Quality detection failed: " + str(e)[-700:]
        )


# -----------------------------
# FAST DOWNLOAD
# -----------------------------

@app.post("/api/download")
async def download(
    x: DownloadRequest,
    bg: BackgroundTasks
):

    u = valid_url(x.url)

    media = x.media.lower().strip()
    q = x.quality.lower().strip()

    allowed = {
        "best",
        "1080",
        "720",
        "480",
        "360"
    }

    if media not in {"video", "audio"}:
        raise HTTPException(
            400,
            "Invalid media type."
        )

    if q not in allowed:
        raise HTTPException(
            400,
            "Invalid quality."
        )

    # Separate temporary directory
    wd = Path(
        tempfile.mkdtemp(
            prefix="dl_",
            dir=DOWNLOAD_DIR
        )
    )

    out = str(
        wd / "%(title)s.%(ext)s"
    )

    # Faster common yt-dlp settings
    base_opts = {
        "outtmpl": out,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # Faster downloads
        "concurrent_fragment_downloads": 8,

        # Connection/retry settings
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,

        # Avoid unnecessary playlist processing
        "extract_flat": False,
    }

    if media == "audio":

        opts = {
            **base_opts,
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

    else:

        if q == "best":
            fmt = "bestvideo*+bestaudio/best"

        else:
            limit = int(q)

            fmt = (
                f"bestvideo*[height<={limit}]"
                f"+bestaudio/"
                f"best[height<={limit}]"
            )

        opts = {
            **base_opts,
            "format": fmt,

            # MP4 when merging video + audio
            "merge_output_format": "mp4",

            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
        }

    def run_download():

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([u])

    try:

        await asyncio.to_thread(
            run_download
        )

    except Exception as e:

        shutil.rmtree(
            wd,
            ignore_errors=True
        )

        raise HTTPException(
            400,
            "Download failed: " + str(e)[-700:]
        )

    files = [
        p
        for p in wd.iterdir()
        if p.is_file()
    ]

    if not files:

        shutil.rmtree(
            wd,
            ignore_errors=True
        )

        raise HTTPException(
            500,
            "No file was created."
        )

    file_path = max(
        files,
        key=lambda p: p.stat().st_mtime
    )

    # Keep directory alive until response is completed
    bg.add_task(
        shutil.rmtree,
        wd,
        True
    )

    extension = file_path.suffix.lower()

    if extension == ".mp4":
        mime = "video/mp4"
    elif extension == ".mp3":
        mime = "audio/mpeg"
    elif extension == ".webm":
        mime = "video/webm"
    else:
        mime = "application/octet-stream"

    return FileResponse(
        str(file_path),
        filename=clean(file_path.stem) + extension,
        media_type=mime,
            )
