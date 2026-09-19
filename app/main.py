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

    # Short and safe filename
    out = str(
        wd / "%(id)s.%(ext)s"
    )

    # yt-dlp settings
    base_opts = {
        "outtmpl": out,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # JavaScript runtime for current YouTube extraction
        "js_runtimes": {
            "deno": {}
        },

        # Faster downloads
        "concurrent_fragment_downloads": 8,

        # Connection/retry settings
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,
        "socket_timeout": 45,

        # Continue partial downloads
        "continuedl": True,

        # Avoid playlist processing
        "extract_flat": False,

        # Safer filenames
        "restrictfilenames": True,

        # Do not download playlist
        "noplaylist": True,
    }

    if media == "audio":

        opts = {
            **base_opts,

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

        if q == "best":

            fmt = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/"
                "best[ext=mp4]/"
                "best"
            )

        else:

            limit = int(q)

            fmt = (
                f"bestvideo[height<={limit}][ext=mp4]+"
                f"bestaudio[ext=m4a]/"
                f"bestvideo[height<={limit}]+"
                f"bestaudio/"
                f"best[height<={limit}][ext=mp4]/"
                f"best[height<={limit}]"
            )

        opts = {
            **base_opts,

            "format": fmt,

            # Merge video + audio into MP4
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

        error_text = str(e)

        raise HTTPException(
            400,
            "Download failed: " + error_text[-700:]
        )

    files = [
        p
        for p in wd.iterdir()
        if p.is_file()
        and not p.name.endswith(".part")
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

    # Keep directory alive until response completes
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

    elif extension == ".m4a":
        mime = "audio/mp4"

    else:
        mime = "application/octet-stream"

    return FileResponse(
        str(file_path),
        filename=clean(file_path.stem) + extension,
        media_type=mime,
)
