import asyncio
import os
import uuid
from pathlib import Path

import yt_dlp

from config import DOWNLOAD_DIR, MAX_FILE_MB

RESOLUTION_STEPS = [1080, 720, 480, 360]
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 120


class DownloadResult:
    def __init__(self, filepath=None, is_audio=False, stream_url=None, title=None, error=None):
        self.filepath = filepath
        self.is_audio = is_audio
        self.stream_url = stream_url
        self.title = title or "media"
        self.error = error

    @property
    def ok(self):
        return self.error is None and (self.filepath or self.stream_url)


def _build_opts(output_path, format_spec):
    return {
        "outtmpl": output_path,
        "format": format_spec,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }


def _file_under_limit(path):
    try:
        return os.path.getsize(path) <= MAX_FILE_BYTES
    except OSError:
        return False


def _extract_info(url):
    """Extract metadata without downloading."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
        return ydl.extract_info(url, download=False)


def _download_with_format(url, format_spec):
    """Download with a specific format string. Returns (filepath, info) or raises."""
    uid = uuid.uuid4().hex[:12]
    output_path = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")
    opts = _build_opts(output_path, format_spec)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # yt-dlp may merge into mp4
        if not os.path.exists(filename):
            mp4 = Path(filename).with_suffix(".mp4")
            if mp4.exists():
                filename = str(mp4)
        return filename, info


def _sync_download(url):
    """Synchronous download logic with resolution step-down."""
    info = _extract_info(url)
    title = info.get("title", "media")
    is_audio = info.get("vcodec") == "none" or info.get("categories", [""])[0:1] == ["Music"]

    # Check if this is audio-only content
    if is_audio or not info.get("vcodec") or info.get("vcodec") == "none":
        # Try audio-only download
        try:
            filepath, _ = _download_with_format(url, "bestaudio[filesize<=%dM]/bestaudio" % MAX_FILE_MB)
            if os.path.exists(filepath) and _file_under_limit(filepath):
                return DownloadResult(filepath=filepath, is_audio=True, title=title)
            # Too large, clean up
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

    # Try best merged video+audio
    try:
        filepath, _ = _download_with_format(
            url, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        )
        if os.path.exists(filepath) and _file_under_limit(filepath):
            return DownloadResult(filepath=filepath, is_audio=False, title=title)
        # Too large, clean up and step down
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

    # Resolution step-down
    for height in RESOLUTION_STEPS:
        try:
            format_spec = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]"
            )
            filepath, _ = _download_with_format(url, format_spec)
            if os.path.exists(filepath) and _file_under_limit(filepath):
                return DownloadResult(filepath=filepath, is_audio=False, title=title)
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            continue

    # Fallback: return direct stream URL
    url_result = info.get("url") or info.get("webpage_url") or url
    return DownloadResult(stream_url=url_result, title=title)


async def download_media(url):
    """Async wrapper that runs the blocking download in a thread with a timeout."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_download, url),
            timeout=DOWNLOAD_TIMEOUT,
        )
        return result
    except asyncio.TimeoutError:
        return DownloadResult(error="timeout")
    except Exception as exc:
        return DownloadResult(error=str(exc))
