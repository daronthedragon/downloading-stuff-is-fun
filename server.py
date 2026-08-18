"""downloading stuff is fun — paste a link, get the file.

Backend: FastAPI + yt-dlp (1800+ sites). Frontend: web/index.html.
Bind to localhost only unless you know what you're doing (see README).
"""

from __future__ import annotations

import ipaddress
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

ROOT = Path(__file__).parent
WEB = ROOT / "web"
WORK = Path(os.environ.get("INEEDIT_WORKDIR") or (Path(tempfile.gettempdir()) / "ineedit"))
WORK.mkdir(parents=True, exist_ok=True)

# how long a finished file sticks around before the janitor eats it
JOB_TTL = int(os.environ.get("INEEDIT_JOB_TTL", "3600"))
# concurrency leash (matters when the instance is public)
MAX_JOBS = int(os.environ.get("INEEDIT_MAX_JOBS", "4"))
MAX_PER_IP = int(os.environ.get("INEEDIT_MAX_PER_IP", "1"))
# biggest single file we'll fetch, in GB
MAX_SIZE = float(os.environ.get("INEEDIT_MAX_SIZE_GB", "4")) * 1024**3
# virus scanning: "auto" scans when a scanner exists, "required" refuses to
# serve anything unscanned, "off" skips it
SCAN_MODE = os.environ.get("INEEDIT_SCAN", "auto").lower()
# private/LAN addresses are refused by default — on a public instance letting
# people fetch 127.0.0.1 or 169.254.169.254 would hand them the server's guts
ALLOW_PRIVATE = os.environ.get("INEEDIT_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

app = FastAPI(title="downloading stuff is fun")

# let the browser extension talk to a local instance — extensions only, never
# ordinary web pages, or any site you visit could drive your downloader
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome|moz|safari-web)-extension://[\w-]+$",
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------- yt-dlp glue

def base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 5,
        "ignoreerrors": False,
    }


def with_cookies(opts: dict[str, Any], browser: str | None) -> dict[str, Any]:
    """Optionally pull cookies from a local browser (age-gated / private media)."""
    if browser and browser != "none":
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def build_format(mode: str, quality: str, audio_format: str, video_format: str) -> dict[str, Any]:
    """Translate the UI's three knobs into yt-dlp options."""
    opts: dict[str, Any] = {}

    if mode == "audio":
        opts["format"] = "bestaudio/best"
        pp: list[dict[str, Any]] = []
        if audio_format != "best":
            pp.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0" if audio_format in ("mp3", "m4a") else None,
            })
        pp.append({"key": "FFmpegMetadata", "add_metadata": True})
        pp.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        opts["postprocessors"] = pp
        opts["writethumbnail"] = True
        return opts

    height = "" if quality in ("max", "best") else f"[height<={quality}]"
    if mode == "mute":
        opts["format"] = f"bestvideo{height}/best{height}/best"
    else:  # auto = video + audio
        opts["format"] = (
            f"bestvideo{height}+bestaudio/best{height}/best"
        )
    if video_format != "auto":
        opts["merge_output_format"] = video_format
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": video_format}]
    else:
        opts["merge_output_format"] = "mp4"
    return opts


def pick_thumb(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else None


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    """Metadata the frontend cares about, plus the heights actually on offer."""
    entries = info.get("entries")
    is_playlist = bool(entries)
    first = (entries[0] if entries else info) or {}

    heights = sorted(
        {f["height"] for f in (first.get("formats") or []) if f.get("height")},
        reverse=True,
    )
    return {
        "title": info.get("title") or first.get("title") or "untitled",
        "uploader": info.get("uploader") or first.get("uploader") or info.get("channel"),
        "duration": first.get("duration") or info.get("duration"),
        "thumbnail": pick_thumb(first) or pick_thumb(info),
        "extractor": (info.get("extractor_key") or "").lower(),
        "webpage_url": info.get("webpage_url"),
        "is_playlist": is_playlist,
        "playlist_count": len(entries) if entries else 0,
        "heights": heights,
        "has_video": bool(heights),
    }


# -------------------------------------------------------- reach anything safely

def host_is_public(url: str) -> bool:
    """False for LAN/loopback/cloud-metadata addresses (SSRF guard)."""
    if ALLOW_PRIVATE:
        return True
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # can't resolve — let the downloader report the real error
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def name_from_response(resp: Any, url: str) -> str:
    """Best filename we can work out from headers, falling back to the URL."""
    disp = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", disp, re.I) or \
        re.search(r'filename="?([^";]+)"?', disp, re.I)
    if m:
        return sanitize(urllib.parse.unquote(m.group(1)))

    name = sanitize(urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name))
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if not Path(name).suffix and ctype:
        name = (name or "download") + (mimetypes.guess_extension(ctype) or "")
    return name or "download"


# extensions that are plainly a file, not a page with media on it
PLAIN_EXT = {
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".xz", ".bz2", ".iso", ".dmg",
    ".exe", ".msi", ".apk", ".deb", ".rpm", ".jar", ".bin",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".epub", ".mobi",
    ".txt", ".csv", ".json", ".xml", ".md", ".yml", ".yaml", ".sql", ".log",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".psd",
    ".ttf", ".otf", ".woff", ".woff2",
}


def is_plain_file(url: str) -> bool:
    return Path(urllib.parse.urlparse(url).path).suffix.lower() in PLAIN_EXT


def direct_fetch(url: str, outdir: Path, jid: str, allow_html: bool = True) -> Path:
    """Plain HTTP download — the catch-all for links yt-dlp has no extractor for."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not allow_html and ctype in ("text/html", "application/xhtml+xml"):
            # a blocked video page — handing over its HTML would be worse than failing
            raise RuntimeError("no file here, just a web page")
        total = int(resp.headers.get("content-length") or 0)
        if total and total > MAX_SIZE:
            raise RuntimeError(f"file is too big ({total / 1024**3:.1f} GB)")

        path = outdir / name_from_response(resp, url)
        done = 0
        started = time.time()
        set_job(jid, state="downloading", filename=path.name, total=total or None)

        with open(path, "wb") as f:
            while chunk := resp.read(256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if done > MAX_SIZE:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise RuntimeError("file is too big")
                elapsed = max(time.time() - started, 0.001)
                set_job(
                    jid,
                    state="downloading",
                    downloaded=done,
                    total=total or None,
                    progress=round(done / total * 100, 1) if total else None,
                    speed=done / elapsed,
                    eta=int((total - done) / (done / elapsed)) if total and done else None,
                )
    return path


# file signatures, longest first so more specific ones win
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", ".pdf"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"PK\x03\x04", ".zip"),
    (b"Rar!\x1a\x07", ".rar"), (b"7z\xbc\xaf\x27\x1c", ".7z"), (b"\x1f\x8b", ".gz"),
    (b"ID3", ".mp3"), (b"OggS", ".ogg"), (b"fLaC", ".flac"), (b"RIFF", ".wav"),
    (b"\x1aE\xdf\xa3", ".mkv"), (b"MZ", ".exe"), (b"\x7fELF", ".elf"),
    (b"{\\rtf", ".rtf"), (b"\xfd7zXZ", ".xz"), (b"BZh", ".bz2"),
)
# extensions yt-dlp invents when it can't tell what something is
BOGUS_EXT = {".unknown_video", ".bin", ".part", ".none", ""}


def sniff_extension(path: Path) -> str | None:
    """Work out what a file actually is from its first bytes."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    for sig, ext in MAGIC:
        if head.startswith(sig):
            if ext == ".wav" and head[8:12] == b"WEBP":
                return ".webp"
            return ext
    if head[4:8] == b"ftyp":
        return ".m4a" if head[8:12] in (b"M4A ", b"M4B ") else ".mp4"
    if head.lstrip()[:1] in (b"<", b"{", b"["):
        return None  # html/json — leave whatever name it came with
    return None


def tidy_name(path: Path) -> Path:
    """Repair yt-dlp's placeholder extensions and 'name [name]' duplication."""
    stem, ext = path.stem, path.suffix.lower()

    if ext in BOGUS_EXT:
        real = sniff_extension(path)
        if real:
            ext = real

    # "%(title)s [%(id)s]" collapses to "dummy [dummy]" when both are the same
    m = re.match(r"^(.*?) \[([^\]]+)\]$", stem)
    if m and m.group(1).strip().lower() == m.group(2).strip().lower():
        stem = m.group(1).strip()

    target = path.with_name(sanitize(stem) + ext)
    if target != path and not target.exists():
        try:
            path.rename(target)
            return target
        except OSError:
            pass
    return path


# ---------------------------------------------------------------- virus scanner

def scanner() -> str | None:
    """Which on-disk scanner we can use, if any."""
    if SCAN_MODE == "off":
        return None
    return shutil.which("clamdscan") or shutil.which("clamscan")


def scan_file(path: Path) -> tuple[str, str]:
    """Returns (verdict, detail): clean | infected | skipped."""
    tool = scanner()
    if not tool:
        if SCAN_MODE == "required":
            raise RuntimeError("virus scanning is required but no scanner is installed")
        return "skipped", ""

    cmd = [tool, "--no-summary"]
    if tool.endswith("clamdscan"):
        cmd.append("--fdpass")  # let the daemon read a file it doesn't own
    cmd.append(str(path))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        if SCAN_MODE == "required":
            raise RuntimeError(f"virus scan failed: {e}") from e
        return "skipped", str(e)

    if p.returncode == 0:
        return "clean", ""
    if p.returncode == 1:
        found = re.search(r":\s*(.+?)\s+FOUND", p.stdout or "")
        return "infected", found.group(1) if found else "malware"
    if SCAN_MODE == "required":
        raise RuntimeError(f"virus scan failed: {(p.stderr or p.stdout or '').strip()[:200]}")
    return "skipped", (p.stderr or "").strip()[:200]


# ------------------------------------------------------------------- job loop

def janitor() -> None:
    now = time.time()
    for d in WORK.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > JOB_TTL:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    with jobs_lock:
        for jid in [k for k, v in jobs.items() if now - v["created"] > JOB_TTL]:
            jobs.pop(jid, None)


def set_job(jid: str, **kw: Any) -> None:
    with jobs_lock:
        if jid in jobs:
            jobs[jid].update(kw)


def run_job(jid: str, url: str, req: "DownloadRequest") -> None:
    outdir = WORK / jid
    outdir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            set_job(
                jid,
                state="downloading",
                progress=round(done / total * 100, 1) if total else None,
                downloaded=done,
                total=total or None,
                speed=d.get("speed"),
                eta=d.get("eta"),
                filename=Path(d.get("filename") or "").name,
            )
        elif d.get("status") == "finished":
            set_job(jid, state="processing", progress=100.0, speed=None, eta=None)

    def pp_hook(d: dict[str, Any]) -> None:
        if d.get("status") == "started":
            set_job(jid, state="processing", step=d.get("postprocessor"))

    opts = base_opts()
    opts.update(build_format(req.mode, req.quality, req.audioFormat, req.videoFormat))
    with_cookies(opts, req.cookiesFrom)
    opts["noplaylist"] = not req.playlist
    opts["outtmpl"] = str(outdir / "%(title).150B [%(id)s].%(ext)s")
    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    if req.playlist:
        opts["ignoreerrors"] = True
    if req.subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en.*", "-live_chat"]
        opts["embedsubtitles"] = req.mode != "audio"

    try:
        set_job(jid, state="resolving")
        def via_ytdlp() -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if info:
                set_job(jid, title=(info.get("title") or "download"))

        def via_http(allow_html: bool) -> None:
            set_job(jid, state="downloading", title=None)
            direct_fetch(url, outdir, jid, allow_html=allow_html)

        # each route covers the other's blind spots: yt-dlp knows media sites but
        # mangles plain files; a raw fetch gets anything but can't parse a page
        if is_plain_file(url):
            try:
                via_http(allow_html=True)
            except Exception as e:  # noqa: BLE001
                try:
                    via_ytdlp()
                except Exception:
                    raise e from None
        else:
            try:
                via_ytdlp()
            except Exception as e:  # noqa: BLE001
                try:
                    via_http(allow_html=False)
                except Exception:
                    raise e from None  # the original complaint is the useful one

        files = [p for p in outdir.rglob("*") if p.is_file() and not p.name.endswith(".part")]
        # thumbnails are only there to be embedded — don't ship them
        keep = [p for p in files if p.suffix.lower() not in (".webp", ".jpg", ".png")] or files
        if not keep:
            raise RuntimeError("nothing was downloaded")

        if len(keep) > 1:
            zpath = outdir / f"{sanitize(jobs[jid].get('title', 'ineedit'))}.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                for p in keep:
                    z.write(p, p.name)
            final = zpath
        else:
            final = keep[0]

        final = tidy_name(final)

        # never hand over a file we haven't looked at
        set_job(jid, state="scanning", progress=100.0)
        verdict, detail = scan_file(final)
        if verdict == "infected":
            shutil.rmtree(outdir, ignore_errors=True)
            raise RuntimeError(f"that file is infected ({detail}) — it was deleted, not served")

        set_job(
            jid,
            state="done",
            progress=100.0,
            path=str(final),
            filename=final.name,
            size=final.stat().st_size,
            count=len(keep),
            scan=verdict,
        )
    except Exception as e:  # noqa: BLE001 — surface whatever yt-dlp says
        set_job(jid, state="error", error=clean_error(str(e)))
        shutil.rmtree(outdir, ignore_errors=True)  # nothing worth keeping


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120].strip() or "download"


# phrasings that all really mean "this site wants you signed in"
BLOCKED = (
    "403", "sign in to confirm", "only images are available",
    "requested format is not available", "private video", "login required",
    "confirm your age", "members-only", "unable to download video data",
)


def clean_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    low = msg.lower()

    if "unable to obtain file audio codec" in low:
        return "this link has no audio track — try auto or mute mode instead"
    if "could not copy" in low and "cookie" in low:
        return (
            "couldn't read that browser's cookies — it locks the database while it's open.\n\n"
            "quit the browser completely (check the tray) and try again."
        )
    if "drm" in low:
        return (
            "this track is DRM-protected — the site only offers it as an encrypted "
            "stream, so there's no file to fetch. nothing to fix on our end; most "
            "other tracks work normally."
        )
    if "unsupported url" in low:
        return "no extractor for that site — a direct link to the media file may still work"
    if any(b in low for b in BLOCKED):
        return (
            msg[:280]
            + '\n\nthe site is refusing anonymous downloads. open settings, set "cookies '
            "from browser\" to a browser you're signed in with, and try again."
        )
    return msg[:600]


# ---------------------------------------------------------------------- API

class InfoRequest(BaseModel):
    url: str
    cookiesFrom: str | None = None


class DownloadRequest(BaseModel):
    url: str
    mode: Literal["auto", "audio", "mute"] = "auto"
    quality: str = "max"
    audioFormat: str = "mp3"
    videoFormat: str = "auto"
    playlist: bool = False
    subtitles: bool = False
    cookiesFrom: str | None = None


def check_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url, re.I):
        raise HTTPException(400, "that doesn't look like a link")
    if not host_is_public(url):
        raise HTTPException(400, "that address is on a private network")
    return url


@app.post("/api/info")
def api_info(req: InfoRequest) -> dict[str, Any]:
    url = check_url(req.url)
    opts = with_cookies(base_opts(), req.cookiesFrom)
    opts["extract_flat"] = "in_playlist"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, clean_error(str(e))) from e
    if not info:
        raise HTTPException(422, "couldn't read anything from that link")
    return summarize(info)


@app.post("/api/download")
def api_download(req: DownloadRequest, request: Request) -> dict[str, str]:
    url = check_url(req.url)
    janitor()

    # public instances need a leash: cap work per visitor and overall
    who = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
           or (request.client.host if request.client else "?"))
    with jobs_lock:
        live = [j for j in jobs.values() if j.get("state") not in ("done", "error")]
        if len(live) >= MAX_JOBS:
            raise HTTPException(429, "server is busy right now — try again in a minute")
        if sum(1 for j in live if j.get("who") == who) >= MAX_PER_IP:
            raise HTTPException(429, "one download at a time, please — let that one finish first")

    jid = uuid.uuid4().hex[:16]
    with jobs_lock:
        jobs[jid] = {"state": "queued", "progress": None, "created": time.time(),
                     "url": url, "who": who}
    threading.Thread(target=run_job, args=(jid, url, req), daemon=True).start()
    return {"id": jid}


@app.get("/api/job/{jid}")
def api_job(jid: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job")
    hide = ("path", "created", "who")  # never echo the requester's IP back out
    return JSONResponse({k: v for k, v in job.items() if k not in hide})


@app.get("/api/file/{jid}")
def api_file(jid: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(jid)
    if not job or job.get("state") != "done":
        raise HTTPException(404, "file isn't ready")
    path = Path(job["path"])
    if not path.exists():
        raise HTTPException(410, "file expired — download it again")

    def cleanup() -> None:
        shutil.rmtree(path.parent, ignore_errors=True)
        with jobs_lock:
            jobs.pop(jid, None)

    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        background=BackgroundTask(cleanup),
    )


@app.delete("/api/job/{jid}")
def api_cancel(jid: str) -> dict[str, bool]:
    """Best-effort: drop the job record and its files. The thread finishes on its own."""
    with jobs_lock:
        jobs.pop(jid, None)
    shutil.rmtree(WORK / jid, ignore_errors=True)
    return {"ok": True}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    """Versions, plus whether the PO token helper is up (matters for YouTube)."""
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=1.5)
        pot = True
    except Exception:  # noqa: BLE001
        pot = False
    tool = scanner()
    return {
        "ytdlp": yt_dlp.version.__version__,
        "potProvider": pot,
        "jobs": len(jobs),
        "scanner": Path(tool).stem if tool else None,
        "scanMode": SCAN_MODE,
    }


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("INEEDIT_HOST", "127.0.0.1")
    port = int(os.environ.get("INEEDIT_PORT", "7788"))
    print(f"\n  ineedit -> http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
