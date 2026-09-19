import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


app = FastAPI(
    title="AllDownloader API",
    version="2.0"
)

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


JOBS = {}
JOBS_LOCK = threading.Lock()


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

    blocked = {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }

    if host in blocked or host.endswith(".local"):
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
    )

    name = name.strip("_.-")

    return name[:90] or "download"


def common_options(outtmpl=None):

    options = {

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "retries": 5,

        "fragment_retries": 5,

        "socket_timeout": 30,

        "restrictfilenames": True,

        "continuedl": True,

        "overwrites": False,

        "concurrent_fragment_downloads": 4,

        # YouTube JavaScript support
        "js_runtimes": {
            "deno": {}
        },

        # EJS remote components
        "remote_components": {
            "ejs": ["github"]
        },
    }

    if outtmpl:
        options["outtmpl"] = outtmpl

    return options


def choose_format(
    media: str,
    quality: str
) -> str:

    if media == "audio":

        return "bestaudio/best"

    if quality == "best":

        # Video + audio separately,
        # then FFmpeg merges them.
        return "bestvideo+bestaudio/best"

    height = int(quality)

    return (
        f"bestvideo[height<={height}]"
        "+"
        "bestaudio/"
        f"best[height<={height}]"
        "/best"
    )


def find_output_files(workdir: Path):

    return [
        p
        for p in workdir.iterdir()
        if p.is_file()
        and not p.name.endswith(".part")
        and not p.name.endswith(".ytdl")
    ]


def run_download(
    job_id: str,
    url: str,
    media: str,
    quality: str
):

    workdir = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=DOWNLOAD_DIR
        )
    )

    outtmpl = str(
        workdir / "%(id)s.%(ext)s"
    )

    with JOBS_LOCK:

        JOBS[job_id]["workdir"] = str(
            workdir
        )


    def progress_hook(data):

        status = data.get("status")

        with JOBS_LOCK:

            job = JOBS.get(job_id)

            if not job:
                return


            if status == "downloading":

                downloaded = (
                    data.get("downloaded_bytes")
                    or 0
                )

                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                    or 0
                )

                if total:

                    job["percent"] = min(
                        99.0,
                        downloaded * 100 / total
                    )

                job["downloaded_bytes"] = downloaded

                job["total_bytes"] = total

                job["speed"] = (
                    data.get("_speed_str")
                    or "Downloading..."
                )

                job["eta"] = (
                    data.get("_eta_str")
                    or ""
                )

                job["status"] = "downloading"


            elif status == "finished":

                job["percent"] = max(
                    job.get("percent", 0),
                    99.0
                )

                job["status"] = "merging"

                job["speed"] = (
                    "FFmpeg processing..."
                )

                job["eta"] = ""


    options = common_options(
        outtmpl
    )

    options["format"] = choose_format(
        media,
        quality
    )

    options["progress_hooks"] = [
        progress_hook
    ]


    if media == "audio":

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    else:

        # IMPORTANT:
        # Video + Audio -> MP4
        options["merge_output_format"] = "mp4"


    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download([url])


        files = find_output_files(
            workdir
        )

        if not files:

            raise RuntimeError(
                "No file was created."
            )


        file_path = max(
            files,
            key=lambda p: p.stat().st_mtime
        )


        with JOBS_LOCK:

            JOBS[job_id]["status"] = (
                "finished"
            )

            JOBS[job_id]["percent"] = (
                100.0
            )

            JOBS[job_id]["file"] = (
                str(file_path)
            )

            JOBS[job_id]["filename"] = (
                clean_name(
                    file_path.stem
                )
                +
                file_path.suffix.lower()
            )

            JOBS[job_id]["speed"] = (
                "Complete"
            )

            JOBS[job_id]["eta"] = ""


    except Exception as exc:

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        message = str(exc).replace(
            "\n",
            " "
        )

        with JOBS_LOCK:

            JOBS[job_id]["status"] = (
                "error"
            )

            JOBS[job_id]["error"] = (
                message[-1000:]
            )


async def start_job(
    url: str,
    media: str,
    quality: str
):

    job_id = uuid.uuid4().hex

    with JOBS_LOCK:

        JOBS[job_id] = {

            "status": "starting",

            "percent": 0.0,

            "downloaded_bytes": 0,

            "total_bytes": 0,

            "speed": "Starting...",

            "eta": "",

            "file": None,

            "filename": None,

            "error": None,

            "created": time.time(),
        }


    asyncio.create_task(
        asyncio.to_thread(
            run_download,
            job_id,
            url,
            media,
            quality,
        )
    )

    return job_id


@app.get("/")
async def root():

    return {
        "service": "AllDownloader API",
        "status": "ok",
    }


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


@app.get("/api/info")
async def info(
    url: str,
    media: str = "video"
):

    url = valid_url(url)

    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            400,
            "Invalid media type."
        )


    options = common_options()

    options["skip_download"] = True


    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            data = ydl.extract_info(
                url,
                download=False
            )


        if data.get("_type") == "playlist":

            raise RuntimeError(
                "Playlists are not supported."
            )


        formats = data.get(
            "formats"
        ) or []


        qualities = []


        if media == "video":

            heights = sorted(
                {
                    int(f["height"])
                    for f in formats
                    if f.get("height")
                    and f.get("vcodec")
                    not in (None, "none")
                    and int(f["height"]) <= 2160
                },
                reverse=True
            )


            for height in heights[:8]:

                size = None

                for f in reversed(formats):

                    if f.get(
                        "height"
                    ) == height:

                        size = (
                            f.get("filesize")
                            or
                            f.get(
                                "filesize_approx"
                            )
                        )

                        if size:
                            break


                qualities.append(
                    {
                        "value": str(height),

                        "height": height,

                        "label": f"{height}p",

                        "filesize": size,
                    }
                )


            if not qualities:

                qualities = [
                    {
                        "value": "best",

                        "height": None,

                        "label": (
                            "Best available"
                        ),

                        "filesize": None,
                    }
                ]


        else:

            qualities = [
                {
                    "value": "best",

                    "height": None,

                    "label": (
                        "Best audio available"
                    ),

                    "filesize": None,
                }
            ]


        return {

            "title": (
                data.get("title")
                or "Video"
            ),

            "thumbnail": (
                data.get("thumbnail")
                or ""
            ),

            "duration": (
                data.get("duration")
                or 0
            ),

            "uploader": (
                data.get("uploader")
                or data.get("channel")
                or ""
            ),

            "qualities": qualities,
        }


    except Exception as exc:

        raise HTTPException(
            400,
            "Could not detect this URL: "
            +
            str(exc).replace(
                "\n",
                " "
            )[:500],
        )


@app.post("/api/start")
async def start(
    request: DownloadRequest
):

    url = valid_url(
        request.url
    )

    media = (
        request.media
        .lower()
        .strip()
    )

    quality = (
        request.quality
        .lower()
        .strip()
    )


    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            400,
            "Invalid media type."
        )


    if quality not in {
        "best",
        "1080",
        "720",
        "480",
        "360"
    }:

        raise HTTPException(
            400,
            "Invalid quality."
        )


    job_id = await start_job(
        url,
        media,
        quality
    )

    return {
        "job_id": job_id
    }


@app.get("/api/progress/{job_id}")
async def progress(
    job_id: str
):

    with JOBS_LOCK:

        job = JOBS.get(
            job_id
        )

        if not job:

            raise HTTPException(
                404,
                "Download job not found."
            )


        return {

            "status": job["status"],

            "percent": job["percent"],

            "speed": job["speed"],

            "eta": job["eta"],

            "filename": job["filename"],

            "error": job["error"],
        }


@app.get("/api/file/{job_id}")
async def get_file(
    job_id: str,
    bg: BackgroundTasks
):

    with JOBS_LOCK:

        job = JOBS.get(
            job_id
        )

        if not job:

            raise HTTPException(
                404,
                "Download job not found."
            )


        if (
            job["status"] != "finished"
            or not job["file"]
        ):

            raise HTTPException(
                409,
                "Download is not finished yet."
            )


        file_path = Path(
            job["file"]
        )

        filename = (
            job["filename"]
            or file_path.name
        )


    if not file_path.exists():

        raise HTTPException(
            404,
            "Downloaded file is no longer available."
        )


    if (
        file_path.suffix.lower()
        == ".mp3"
    ):

        media_type = "audio/mpeg"

    else:

        media_type = "video/mp4"


    def cleanup():

        with JOBS_LOCK:

            current = JOBS.pop(
                job_id,
                None
            )

        if current:

            shutil.rmtree(
                current.get(
                    "workdir",
                    ""
                ),
                ignore_errors=True
            )


    bg.add_task(
        cleanup
    )


    return FileResponse(
        str(file_path),
        filename=filename,
        media_type=media_type,
    )


# ============================================================
# BACKWARD COMPATIBILITY
# Existing frontend can still use /api/download
# ============================================================

@app.post("/api/download")
async def download_legacy(
    request: DownloadRequest,
    bg: BackgroundTasks
):

    url = valid_url(
        request.url
    )

    media = (
        request.media
        .lower()
        .strip()
    )

    quality = (
        request.quality
        .lower()
        .strip()
    )


    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            400,
            "Invalid media type."
        )


    if quality not in {
        "best",
        "1080",
        "720",
        "480",
        "360"
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


    options = common_options(
        outtmpl
    )

    options["format"] = choose_format(
        media,
        quality
    )


    if media == "audio":

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    else:

        options["merge_output_format"] = (
            "mp4"
        )


    try:

        await asyncio.to_thread(
            lambda:
            yt_dlp.YoutubeDL(
                options
            ).download([url])
        )


    except Exception as exc:

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        raise HTTPException(
            400,
            "Download failed: "
            +
            str(exc).replace(
                "\n",
                " "
            )[-1000:]
        )


    files = find_output_files(
        workdir
    )


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


    filename = (
        clean_name(
            file_path.stem
        )
        +
        file_path.suffix.lower()
    )


    if media == "audio":

        media_type = "audio/mpeg"

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
