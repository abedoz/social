import asyncio
import os
import uuid
from pathlib import Path

import yt_dlp

from config import DOWNLOAD_DIR, MAX_FILE_MB

RESOLUTION_STEPS = [1080, 720, 480, 360]
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 120

MODE_HIGHEST = "highest"
MODE_LOWEST = "lowest"
MODE_AUDIO = "audio"
MODE_VIDEO = "video"


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


def _build_opts(output_path, format_spec, merge=True):
    opts = {
        "outtmpl": output_path,
        "format": format_spec,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if merge:
        opts["merge_output_format"] = "mp4"
    return opts


def _file_under_limit(path):
    try:
        return os.path.getsize(path) <= MAX_FILE_BYTES
    except OSError:
        return False


def _extract_info(url):
    """Extract metadata without downloading."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
        return ydl.extract_info(url, download=False)


def _download_with_format(url, format_spec, merge=True):
    """Download with a specific format string. Returns (filepath, info) or raises."""
    uid = uuid.uuid4().hex[:12]
    output_path = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")
    opts = _build_opts(output_path, format_spec, merge=merge)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # yt-dlp may merge into mp4
        if not os.path.exists(filename):
            mp4 = Path(filename).with_suffix(".mp4")
            if mp4.exists():
                filename = str(mp4)
        return filename, info


def _try_download(url, format_spec, is_audio=False, title="media", merge=True):
    """Attempt a download and return a DownloadResult, or None if file is too large."""
    filepath, _ = _download_with_format(url, format_spec, merge=merge)
    if os.path.exists(filepath) and _file_under_limit(filepath):
        return DownloadResult(filepath=filepath, is_audio=is_audio, title=title)
    if os.path.exists(filepath):
        os.remove(filepath)
    return None


def _sync_download(url, mode):
    """Synchronous download with mode selection."""
    info = _extract_info(url)
    title = info.get("title", "media")

    if mode == MODE_AUDIO:
        return _download_audio(url, title)
    elif mode == MODE_VIDEO:
        return _download_video_only(url, title)
    elif mode == MODE_LOWEST:
        return _download_lowest(url, title)
    else:
        return _download_highest(url, title)


def _download_highest(url, title):
    """Best video+audio merged, with resolution step-down if over size limit."""
    # Try best merged
    try:
        result = _try_download(
            url, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            title=title,
        )
        if result:
            return result
    except Exception:
        pass

    # Resolution step-down
    for height in RESOLUTION_STEPS:
        try:
            fmt = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]"
            )
            result = _try_download(url, fmt, title=title)
            if result:
                return result
        except Exception:
            continue

    # Fallback: stream URL
    fallback_url = info.get("url") or info.get("webpage_url") or url
    return DownloadResult(stream_url=fallback_url, title=title)


def _download_lowest(url, title):
    """Lowest quality video+audio."""
    try:
        result = _try_download(
            url, "worstvideo+worstaudio/worst",
            title=title,
        )
        if result:
            return result
    except Exception:
        pass
    return DownloadResult(error="Could not download lowest quality.")


def _download_audio(url, title):
    """Audio only, best quality."""
    try:
        result = _try_download(
            url, f"bestaudio[filesize<={MAX_FILE_MB}M]/bestaudio",
            is_audio=True, title=title, merge=False,
        )
        if result:
            return result
    except Exception:
        pass
    return DownloadResult(error="Could not download audio.")


def _download_video_only(url, title):
    """Video only, no audio track."""
    try:
        result = _try_download(
            url, "bestvideo[ext=mp4]/bestvideo",
            title=title, merge=False,
        )
        if result:
            return result
    except Exception:
        pass

    # Step down if too large
    for height in RESOLUTION_STEPS:
        try:
            result = _try_download(
                url, f"bestvideo[height<={height}][ext=mp4]/bestvideo[height<={height}]",
                title=title, merge=False,
            )
            if result:
                return result
        except Exception:
            continue

    return DownloadResult(error="Could not download video.")


async def download_media(url, mode=MODE_HIGHEST):
    """Async wrapper that runs the blocking download in a thread with a timeout."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_download, url, mode),
            timeout=DOWNLOAD_TIMEOUT,
        )
        return result
    except asyncio.TimeoutError:
        return DownloadResult(error="timeout")
    except Exception as exc:
        return DownloadResult(error=str(exc))
