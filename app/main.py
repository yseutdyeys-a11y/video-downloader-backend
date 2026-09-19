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
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="All Video Downloader API")

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
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


class DownloadRequest(BaseModel):
    url: str = Field(
        min_length=8,
        max_length=4096
    )
    media: str = "video"
    quality: str = "best"


def valid_url(raw: str) -> str:
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
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                None,
                type=socket.SOCK_STREAM
            )
        }

        for addr in addresses:
            ip = ipaddress.ip_address(addr)

            if any(
                (
                    ip.is_private,
                    ip.is_loopback,
                    ip.is_link_local,
                    ip.is_multicast,
                    ip.is_reserved,
                    ip.is_unspecified,
                )
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


def clean_name(name: str) -> str:
    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name
    ).strip("_.-")

    return name[:90] or "download"


@app.get("/")
async def root():
    return {
        "service": "All Video Downloader API",
        "status": "ok",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


@app.post("/api/download")
async def download(
    x: DownloadRequest,
    bg: BackgroundTasks
):
    url = valid_url(x.url)

    media = x.media.lower().strip()
    quality = x.quality.lower().strip()

    if media not in {"video", "audio"}:
        raise HTTPException(
            400,
            "Invalid media type."
        )

    if quality not in {
        "best",
        "1080",
        "720",
        "480",
        "360",
    }:
        raise HTTPException(
            400,
            "Invalid quality."
        )

    workdir = Path(
        tempfile.mkdtemp(
            prefix="dl_",
            dir=DOWNLOAD_DIR
        )
    )

    outtmpl = str(
        workdir / "%(id)s.%(ext)s"
    )

    common = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # Better retry handling
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,
        "socket_timeout": 45,

        # Faster downloads
        "concurrent_fragment_downloads": 8,

        # Continue partial downloads
        "continuedl": True,

        # Safe filenames
        "restrictfilenames": True,

        # Current YouTube extraction support
        "js_runtimes": {
            "deno": {}
        },
    }

    if media == "audio":

        opts = {
            **common,

            "format": (
                "bestaudio[ext=m4a]/"
                "bestaudio/best"
            ),

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

    else:

        if quality == "best":

            fmt = (
                "bestvideo[ext=mp4]+"
                "bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/"
                "best[ext=mp4]/"
                "best"
            )

        else:

            n = int(quality)

            fmt = (
                f"bestvideo[height<={n}][ext=mp4]+"
                f"bestaudio[ext=m4a]/"
                f"bestvideo[height<={n}]+"
                f"bestaudio/"
                f"best[height<={n}][ext=mp4]/"
                f"best[height<={n}]"
            )

        opts = {
            **common,
            "format": fmt,
            "merge_output_format": "mp4",
        }

    def run_download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:

        await asyncio.to_thread(
            run_download
        )

    except Exception as exc:

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        message = str(exc).replace(
            "\n",
            " "
        )

        raise HTTPException(
            400,
            "Download failed: " + message[-700:]
        )

    files = [
        p
        for p in workdir.iterdir()
        if p.is_file()
        and not p.name.endswith(".part")
    ]

    if not files:

        shutil.rmtree(
            workdir,
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

    ext = file_path.suffix.lower()

    if not ext:
        ext = (
            ".mp3"
            if media == "audio"
            else ".mp4"
        )

    filename = (
        clean_name(file_path.stem)
        + ext
    )

    if media == "audio":
        media_type = "audio/mpeg"
    elif ext == ".webm":
        media_type = "video/webm"
    else:
        media_type = "video/mp4"

    bg.add_task(
        shutil.rmtree,
        workdir,
        True
    )

    return FileResponse(
        str(file_path),
        filename=filename,
        media_type=media_type,
)
