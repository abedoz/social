import asyncio
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import yt_dlp

from config import DOWNLOAD_DIR, MAX_FILE_MB

RESOLUTION_STEPS = [1080, 720, 480, 360]
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 120

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# TLS impersonation targets to cycle through on failure
IMPERSONATE_TARGETS = ["chrome", "chrome110", "edge99", "safari15_5"]

# Check if curl_cffi is available for TLS impersonation
try:
    import curl_cffi  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

MODE_HIGHEST = "highest"
MODE_LOWEST = "lowest"
MODE_AUDIO = "audio"
MODE_VIDEO = "video"

# Tracking parameters to strip from URLs
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "si", "feature", "fbclid", "igshid", "s", "t", "ref",
}


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


def _clean_url(url):
    """Strip tracking params and normalize the URL for better extraction."""
    parsed = urlparse(url)

    # Strip tracking parameters
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if k not in TRACKING_PARAMS}
    clean_query = urlencode(cleaned, doseq=True)

    # Normalize x.com → twitter.com (yt-dlp has better twitter.com support)
    hostname = parsed.hostname or ""
    netloc = parsed.netloc
    if hostname == "x.com":
        netloc = netloc.replace("x.com", "twitter.com", 1)

    return urlunparse(parsed._replace(netloc=netloc, query=clean_query))


def _base_opts():
    """Common yt-dlp options shared across all calls."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "http_headers": BROWSER_HEADERS,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "sleep_interval_requests": 0.5,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {"player_client": ["ios,web"]},
        },
    }


def _build_opts(output_path, format_spec, merge=True, impersonate=None):
    opts = _base_opts()
    opts["outtmpl"] = output_path
    opts["format"] = format_spec
    if merge:
        opts["merge_output_format"] = "mp4"
    if impersonate and HAS_CURL_CFFI:
        opts["impersonate"] = impersonate
    return opts


def _file_under_limit(path):
    try:
        return os.path.getsize(path) <= MAX_FILE_BYTES
    except OSError:
        return False


def _extract_info(url):
    """Extract metadata, cycling through impersonation targets on failure."""
    url = _clean_url(url)

    # Try with each impersonation target
    if HAS_CURL_CFFI:
        for target in IMPERSONATE_TARGETS:
            try:
                opts = _base_opts()
                opts["impersonate"] = target
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception:
                continue

    # Final attempt without impersonation
    opts = _base_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_with_format(url, format_spec, merge=True):
    """Download with a specific format, cycling impersonation targets on failure."""
    url = _clean_url(url)
    last_error = None

    targets = IMPERSONATE_TARGETS if HAS_CURL_CFFI else [None]
    for target in targets:
        try:
            uid = uuid.uuid4().hex[:12]
            output_path = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")
            opts = _build_opts(output_path, format_spec, merge=merge, impersonate=target)

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    mp4 = Path(filename).with_suffix(".mp4")
                    if mp4.exists():
                        filename = str(mp4)
                return filename, info
        except Exception as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError("All impersonation targets failed")


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
        return _download_highest(url, title, info)


def _download_highest(url, title, info=None):
    """Best video+audio merged, with resolution step-down if over size limit."""
    try:
        result = _try_download(
            url, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            title=title,
        )
        if result:
            return result
    except Exception:
        pass

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
    fallback_url = (info or {}).get("url") or (info or {}).get("webpage_url") or url
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
