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


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="AllDownloader API",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# DOWNLOAD DIRECTORY
# ============================================================

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/tmp/downloads")
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# REQUEST MODEL
# ============================================================

class DownloadRequest(BaseModel):
    url: str = Field(
        min_length=8,
        max_length=4096
    )

    media: str = "video"

    quality: str = "best"


# ============================================================
# URL SECURITY
# ============================================================

def valid_url(raw: str) -> str:

    url = raw.strip()

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid http/https URL."
        )

    if not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL."
        )

    host = parsed.hostname.lower().rstrip(".")

    blocked_hosts = {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }

    if host in blocked_hosts or host.endswith(".local"):
        raise HTTPException(
            status_code=400,
            detail="Host not allowed."
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

        for address in addresses:

            ip = ipaddress.ip_address(address)

            if any([
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            ]):
                raise HTTPException(
                    status_code=400,
                    detail="Private/internal address not allowed."
                )

    except socket.gaierror:

        raise HTTPException(
            status_code=400,
            detail="Host could not be resolved."
        )

    return url


# ============================================================
# SAFE FILE NAME
# ============================================================

def clean_name(name: str) -> str:

    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name
    )

    name = name.strip("_.-")

    return name[:90] or "download"


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
async def root():

    return {
        "service": "AllDownloader API",
        "status": "ok"
    }


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


# ============================================================
# DOWNLOAD API
# ============================================================

@app.post("/api/download")
async def download(
    request: DownloadRequest,
    bg: BackgroundTasks
):

    # --------------------------------------------------------
    # Validate URL
    # --------------------------------------------------------

    url = valid_url(request.url)

    # --------------------------------------------------------
    # Validate media
    # --------------------------------------------------------

    media = request.media.lower().strip()

    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            status_code=400,
            detail="Invalid media type."
        )

    # --------------------------------------------------------
    # Validate quality
    # --------------------------------------------------------

    quality = request.quality.lower().strip()

    allowed_quality = {
        "best",
        "1080",
        "720",
        "480",
        "360",
    }

    if quality not in allowed_quality:

        raise HTTPException(
            status_code=400,
            detail="Invalid quality."
        )

    # --------------------------------------------------------
    # Temporary working directory
    # --------------------------------------------------------

    workdir = Path(
        tempfile.mkdtemp(
            prefix="dl_",
            dir=DOWNLOAD_DIR
        )
    )

    # IMPORTANT:
    # ID based filename prevents very long filename errors.

    outtmpl = str(
        workdir / "%(id)s.%(ext)s"
    )

    # ========================================================
    # COMMON YT-DLP OPTIONS
    # ========================================================

    common = {

        # Output
        "outtmpl": outtmpl,

        # Never download playlist
        "noplaylist": True,

        # Cleaner server logs
        "quiet": True,
        "no_warnings": True,

        # Retry
        "retries": 5,
        "fragment_retries": 5,

        # Network timeout
        "socket_timeout": 30,

        # Safe filenames
        "restrictfilenames": True,

        # Faster fragmented downloads
        "concurrent_fragment_downloads": 4,

        # ----------------------------------------------------
        # YouTube JavaScript challenge support
        # ----------------------------------------------------

        "js_runtimes": {
            "deno": {}
        },

        # EJS remote components
        "remote_components": {
            "ejs": ["github"]
        },

        # Continue interrupted downloads
        "continuedl": True,

        # Don't overwrite unnecessarily
        "overwrites": False,
    }


    # ========================================================
    # AUDIO DOWNLOAD
    # ========================================================

    if media == "audio":

        opts = {
            **common,

            # Best available audio
            "format": "bestaudio/best",

            # Convert to MP3
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }


    # ========================================================
    # VIDEO DOWNLOAD
    # ========================================================

    else:

        # ----------------------------------------------------
        # BEST QUALITY
        # ----------------------------------------------------

        if quality == "best":

            # VERY IMPORTANT:
            #
            # bestvideo + bestaudio
            #
            # This downloads separate video and audio streams
            # when the website provides them.
            #
            # FFmpeg then merges them into one MP4.
            #

            fmt = (
                "bestvideo+bestaudio/"
                "best"
            )

        # ----------------------------------------------------
        # SPECIFIC QUALITY
        # ----------------------------------------------------

        else:

            height = int(quality)

            fmt = (
                f"bestvideo[height<={height}]"
                "+"
                "bestaudio/"
                f"best[height<={height}]"
                "/best"
            )

        opts = {
            **common,

            # Video + Audio
            "format": fmt,

            # ------------------------------------------------
            # THIS IS THE IMPORTANT PART
            # ------------------------------------------------
            #
            # FFmpeg combines:
            #
            # video stream
            # +
            # audio stream
            #
            # into one MP4 file.
            #

            "merge_output_format": "mp4",
        }


    # ========================================================
    # ACTUAL DOWNLOAD
    # ========================================================

    try:

        await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL(opts).download([url])
        )

    except Exception as exc:

        # Remove temporary files
        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        message = str(exc).replace(
            "\n",
            " "
        )

        raise HTTPException(
            status_code=400,
            detail=(
                "Download failed: "
                + message[-1000:]
            )
        )


    # ========================================================
    # FIND GENERATED FILE
    # ========================================================

    files = [
        file
        for file in workdir.iterdir()
        if file.is_file()
        and not file.name.endswith(".part")
        and not file.name.endswith(".ytdl")
    ]


    if not files:

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        raise HTTPException(
            status_code=500,
            detail="No file was created."
        )


    # Newest generated file
    file_path = max(
        files,
        key=lambda file: file.stat().st_mtime
    )


    # ========================================================
    # FILE EXTENSION
    # ========================================================

    ext = file_path.suffix.lower()

    if not ext:

        if media == "audio":
            ext = ".mp3"
        else:
            ext = ".mp4"


    # ========================================================
    # DOWNLOAD FILE NAME
    # ========================================================

    filename = (
        clean_name(file_path.stem)
        + ext
    )


    # ========================================================
    # MIME TYPE
    # ========================================================

    if media == "audio":

        media_type = "audio/mpeg"

    else:

        media_type = "video/mp4"


    # ========================================================
    # CLEANUP AFTER RESPONSE
    # ========================================================

    bg.add_task(
        shutil.rmtree,
        workdir,
        True
    )


    # ========================================================
    # RETURN FILE
    # ========================================================

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type,
    )
