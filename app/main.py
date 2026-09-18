import asyncio,ipaddress,os,re,socket,tempfile,shutil
from pathlib import Path
from urllib.parse import urlparse
import yt_dlp
from fastapi import FastAPI,HTTPException,BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field

app=FastAPI(title="All Video Downloader")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=False,allow_methods=["*"],allow_headers=["*"])
DOWNLOAD_DIR=Path(os.getenv("DOWNLOAD_DIR","/tmp/downloads"));DOWNLOAD_DIR.mkdir(parents=True,exist_ok=True)

class DownloadRequest(BaseModel):
    url:str=Field(min_length=8,max_length=4096)
    media:str="video"
    quality:str="best"

def valid_url(raw):
    u=raw.strip();p=urlparse(u)
    if p.scheme not in {"http","https"} or not p.hostname: raise HTTPException(400,"Enter a valid http/https URL.")
    host=p.hostname.lower().rstrip(".")
    if host in {"localhost","localhost.localdomain","metadata.google.internal"} or host.endswith(".local"): raise HTTPException(400,"Host not allowed.")
    try:
        for x in {i[4][0] for i in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)}:
            ip=ipaddress.ip_address(x)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified: raise HTTPException(400,"Private/internal address not allowed.")
    except socket.gaierror: raise HTTPException(400,"Host could not be resolved.")
    return u

def clean(n): return (re.sub(r"[\\/:*?"<>|]+","_",re.sub(r"\s+"," ",n)).strip(" .") or "download")[:160]

@app.get("/")
async def root(): return {"service":"All Video Downloader API","status":"ok"}

@app.get("/health")
async def health(): return {"status":"ok"}

@app.post("/api/download")
async def download(x:DownloadRequest,bg:BackgroundTasks):
    u=valid_url(x.url); media=x.media.lower();q=x.quality.lower()
    if media not in {"video","audio"} or q not in {"best","1080","720","480","360"}: raise HTTPException(400,"Invalid format or quality.")
    wd=Path(tempfile.mkdtemp(prefix="dl_",dir=DOWNLOAD_DIR)); out=str(wd/"%(title)s.%(ext)s")
    if media=="audio":
        fmt="bestaudio/best"; pp=[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]; opts={"format":fmt,"outtmpl":out,"noplaylist":True,"quiet":True,"no_warnings":True,"retries":2,"fragment_retries":2,"socket_timeout":30,"postprocessors":pp}
    else:
        fmt="bestvideo*+bestaudio/best" if q=="best" else f"bestvideo*[height<={int(q)}]+bestaudio/best[height<={int(q)}]"
        opts={"format":fmt,"outtmpl":out,"noplaylist":True,"quiet":True,"no_warnings":True,"retries":2,"fragment_retries":2,"socket_timeout":30,"merge_output_format":"mp4","postprocessors":[{"key":"FFmpegVideoConvertor","preferedformat":"mp4"}]}
    try:
        await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).download([u]))
    except Exception as e:
        shutil.rmtree(wd,ignore_errors=True);raise HTTPException(400,"Download failed: "+str(e)[-700:])
    fs=[p for p in wd.iterdir() if p.is_file()]
    if not fs: shutil.rmtree(wd,ignore_errors=True);raise HTTPException(500,"No file was created.")
    f=max(fs,key=lambda p:p.stat().st_mtime);bg.add_task(shutil.rmtree,wd,True)
    return FileResponse(str(f),filename=clean(f.stem)+f.suffix,media_type="application/octet-stream")
