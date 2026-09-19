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
    version="4.0"
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

        "js_runtimes": {
            "deno": {}
        },

        "remote_components": {
            "ejs": ["github"]
        },
    }

    if outtmpl:
        options["outtmpl"] = outtmpl

    return options


def normalize_quality(quality):

    if quality is None:
        return "best"

    quality = str(quality).lower().strip()

    if quality in {
        "",
        "best",
        "best available",
        "best_available"
    }:
        return "best"

    quality = quality.replace(
        "best available",
        "best"
    )

    if quality == "best":
        return "best"

    quality = quality.replace(
        "p",
        ""
    ).strip()

    try:
        height = int(quality)

        if height <= 0:
            return "best"

        return str(height)

    except (TypeError, ValueError):

        return "best"


def available_heights(formats):

    result = set()

    for f in formats or []:

        height = f.get("height")

        if not height:
            continue

        try:

            height = int(height)

            if (
                height > 0
                and f.get("vcodec")
                not in (None, "none")
            ):
                result.add(height)

        except (TypeError, ValueError):
            pass

    return sorted(
        result,
        reverse=True
    )


def resolve_quality(
    quality,
    heights
):

    heights = sorted(
        set(heights or []),
        reverse=True
    )

    quality = normalize_quality(
        quality
    )

    if quality == "best":
        return "best"

    requested = int(quality)

    if requested in heights:
        return str(requested)

    lower = [
        h
        for h in heights
        if h <= requested
    ]

    if lower:
        return str(
            max(lower)
        )

    return "best"


def choose_format(
    media,
    quality,
    heights=None
):

    if media == "audio":
        return "bestaudio/best"

    resolved = resolve_quality(
        quality,
        heights or []
    )

    if resolved == "best":

        return (
            "bestvideo+bestaudio/"
            "best"
        )

    height = int(resolved)

    return (
        f"bestvideo[height={height}]+bestaudio/"
        f"best[height={height}]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/"
        "best"
    )


def find_output_files(workdir):

    return [
        p
        for p in Path(workdir).iterdir()
        if p.is_file()
        and not p.name.endswith(".part")
        and not p.name.endswith(".ytdl")
    ]


def run_download(
    job_id,
    url,
    media,
    quality
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

    try:

        detect_options = common_options()

        detect_options["skip_download"] = True

        with yt_dlp.YoutubeDL(
            detect_options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

        if info.get("_type") == "playlist":

            raise RuntimeError(
                "Playlists are not supported."
            )

        formats = info.get(
            "formats"
        ) or []

        heights = available_heights(
            formats
        )

        requested_quality = normalize_quality(
            quality
        )

        resolved_quality = resolve_quality(
            requested_quality,
            heights
        )

        with JOBS_LOCK:

            JOBS[job_id][
                "available_qualities"
            ] = heights

            JOBS[job_id][
                "requested_quality"
            ] = requested_quality

            JOBS[job_id][
                "resolved_quality"
            ] = resolved_quality

        options = common_options(
            outtmpl
        )

        options["format"] = choose_format(
            media,
            requested_quality,
            heights
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

            options["merge_output_format"] = (
                "mp4"
            )

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

            if job_id in JOBS:

                JOBS[job_id]["status"] = (
                    "error"
                )

                JOBS[job_id]["error"] = (
                    message[-1000:]
                )


async def start_job(
    url,
    media,
    quality
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
            quality
        )
    )

    return job_id


@app.get("/")
async def root():

    return {
        "service": "AllDownloader API",
        "status": "ok",
        "version": "4.0"
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

    media = (
        media
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

        heights = available_heights(
            formats
        )

        qualities = []

        if media == "video":

            qualities.append(
                {
                    "value": "best",
                    "height": None,
                    "label": "Best available",
                    "filesize": None
                }
            )

            for height in heights:

                size = None

                for f in formats:

                    if f.get("height") != height:
                        continue

                    size = (
                        f.get("filesize")
                        or
                        f.get("filesize_approx")
                    )

                    if size:
                        break

                qualities.append(
                    {
                        "value": str(height),
                        "height": height,
                        "label": f"{height}p",
                        "filesize": size
                    }
                )

        else:

            qualities = [
                {
                    "value": "best",
                    "height": None,
                    "label": "Best audio available",
                    "filesize": None
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

            "available_qualities": heights,

            "qualities": qualities
        }

    except Exception as exc:

        raise HTTPException(
            400,
            "Could not detect this URL: "
            +
            str(exc).replace(
                "\n",
                " "
            )[:500]
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

    quality = normalize_quality(
        request.quality
    )

    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            400,
            "Invalid media type."
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

            "resolved_quality": job.get(
                "resolved_quality"
            ),

            "available_qualities": job.get(
                "available_qualities",
                []
            )
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

    if file_path.suffix.lower() == ".mp3":

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
        media_type=media_type
    )


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

    quality = normalize_quality(
        request.quality
    )

    if media not in {
        "video",
        "audio"
    }:

        raise HTTPException(
            400,
            "Invalid media type."
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

    try:

        detect_options = common_options()

        detect_options["skip_download"] = True

        with yt_dlp.YoutubeDL(
            detect_options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

        heights = available_heights(
            info.get("formats") or []
        )

        options = common_options(
            outtmpl
        )

        options["format"] = choose_format(
            media,
            quality,
            heights
        )

        if media == "audio":

            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192"
                }
            ]

        else:

            options["merge_output_format"] = (
                "mp4"
            )

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
        media_type=media_type
)
