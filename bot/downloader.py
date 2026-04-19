import asyncio
import glob
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import yt_dlp

from config import COOKIES_FILE, DOWNLOAD_DIR, MAX_FILE_MB, PROXY
from facebook import is_facebook_url, facebook_download
from instagram import is_instagram_url, instagram_download

RESOLUTION_STEPS = [1080, 720, 480, 360]
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 180

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

IMPERSONATE_TARGETS = ["chrome", "chrome110", "edge99", "safari15_5"]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

try:
    import curl_cffi  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

MODE_HIGHEST = "highest"
MODE_LOWEST = "lowest"
MODE_AUDIO = "audio"
MODE_VIDEO = "video"

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "si", "feature", "fbclid", "igshid", "igsh", "img_index", "s", "t", "ref",
}


class DownloadResult:
    def __init__(self, filepath=None, filepaths=None, is_audio=False, is_image=False,
                 stream_url=None, title=None, error=None):
        self.filepath = filepath
        self.filepaths = filepaths or []
        self.is_audio = is_audio
        self.is_image = is_image
        self.stream_url = stream_url
        self.title = title or "media"
        self.error = error

    @property
    def ok(self):
        return self.error is None and (self.filepath or self.filepaths or self.stream_url)


def _clean_url(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if k not in TRACKING_PARAMS}
    clean_query = urlencode(cleaned, doseq=True)
    hostname = parsed.hostname or ""
    netloc = parsed.netloc
    if hostname == "x.com":
        netloc = netloc.replace("x.com", "twitter.com", 1)
    return urlunparse(parsed._replace(netloc=netloc, query=clean_query))


def _base_opts():
    opts = {
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
            "tiktok": {"api_hostname": ["api22-normal-c-useast2a.tiktokv.com"]},
        },
    }
    if PROXY:
        opts["proxy"] = PROXY
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


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


def _is_image(filepath):
    return Path(filepath).suffix.lower() in IMAGE_EXTENSIONS


def _extract_info(url):
    url = _clean_url(url)
    last_error = None

    if HAS_CURL_CFFI:
        for target in IMPERSONATE_TARGETS:
            try:
                opts = _base_opts()
                opts["impersonate"] = target
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as exc:
                last_error = exc
                print(f"[yt-dlp] extract failed with impersonate={target}: {exc}")
                continue

    try:
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        last_error = exc
        print(f"[yt-dlp] extract failed without impersonation: {exc}")

    try:
        opts = _base_opts()
        opts["force_generic_extractor"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        print(f"[yt-dlp] generic extractor also failed: {exc}")

    raise last_error or RuntimeError("All extraction attempts failed")


def _download_with_format(url, format_spec, merge=True):
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
            print(f"[yt-dlp] download failed with impersonate={target}: {exc}")
            continue

    try:
        uid = uuid.uuid4().hex[:12]
        output_path = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")
        opts = _build_opts(output_path, format_spec, merge=merge)
        opts["force_generic_extractor"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                mp4 = Path(filename).with_suffix(".mp4")
                if mp4.exists():
                    filename = str(mp4)
            return filename, info
    except Exception as exc:
        print(f"[yt-dlp] generic extractor download also failed: {exc}")

    raise last_error or RuntimeError("All download attempts failed")


def _try_download(url, format_spec, is_audio=False, title="media", merge=True):
    filepath, _ = _download_with_format(url, format_spec, merge=merge)
    if os.path.exists(filepath) and _file_under_limit(filepath):
        return DownloadResult(filepath=filepath, is_audio=is_audio, title=title)
    if os.path.exists(filepath):
        os.remove(filepath)
    return None


# ── gallery-dl fallback ──────────────────────────────────────────────

def _gallery_dl_download(url):
    """Use gallery-dl to download images/carousels. Returns a DownloadResult."""
    url = _clean_url(url)
    uid = uuid.uuid4().hex[:8]
    dest_dir = os.path.join(DOWNLOAD_DIR, f"gdl_{uid}")
    os.makedirs(dest_dir, exist_ok=True)

    cmd = [
        "gallery-dl",
        "--dest", dest_dir,
        "--no-mtime",
        "--filename", "{num:>02}.{extension}",
        "--directory", ".",
    ]

    if PROXY:
        cmd.extend(["--proxy", PROXY])
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd.extend(["--cookies", COOKIES_FILE])

    cmd.append(url)

    print(f"[gallery-dl] running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print(f"[gallery-dl] exit code: {proc.returncode}")
        if proc.stderr:
            print(f"[gallery-dl] stderr: {proc.stderr[:500]}")
    except subprocess.TimeoutExpired:
        return DownloadResult(error="timeout")
    except Exception as exc:
        print(f"[gallery-dl] exception: {exc}")
        return DownloadResult(error=str(exc))

    # Collect downloaded files
    files = sorted(glob.glob(os.path.join(dest_dir, "*")))
    files = [f for f in files if os.path.isfile(f)]

    if not files:
        os.rmdir(dest_dir)
        return DownloadResult(error="gallery-dl downloaded 0 files")

    # Check if all files are images
    all_images = all(_is_image(f) for f in files)

    if len(files) == 1:
        f = files[0]
        if _is_image(f):
            return DownloadResult(filepath=f, is_image=True, title="image")
        else:
            return DownloadResult(filepath=f, is_audio=False, title="media")

    # Multiple files (carousel)
    if all_images:
        return DownloadResult(filepaths=files, is_image=True, title="carousel")

    # Mixed content — return first file
    return DownloadResult(filepath=files[0], is_image=_is_image(files[0]), title="media")


# ── main download logic ──────────────────────────────────────────────

def _sync_download(url, mode):
    """Route to platform-specific or generic download logic."""
    if is_instagram_url(url):
        return _sync_download_instagram(url, mode)
    return _sync_download_generic(url, mode)


def _sync_download_instagram(url, mode):
    """Instagram-specific flow: yt-dlp for video, gallery-dl only for images."""
    url_clean = _clean_url(url)

    # 1. Try yt-dlp extraction + download
    try:
        info = _extract_info(url_clean)
        title = info.get("title", "media")

        if mode == MODE_AUDIO:
            result = _download_audio(url_clean, title)
        elif mode == MODE_VIDEO:
            result = _download_video_only(url_clean, title)
        elif mode == MODE_LOWEST:
            result = _download_lowest(url_clean, title)
        else:
            result = _download_highest(url_clean, title, info)

        # yt-dlp produced a file — done, no gallery-dl needed
        if result and result.filepath and os.path.exists(result.filepath):
            return result
        if result and result.stream_url:
            return result

        # yt-dlp extracted info but produced no file — image post
        print("[yt-dlp] extracted info but no video file — image post, trying gallery-dl")

    except Exception as exc:
        print(f"[yt-dlp] Instagram failed: {exc}")

    # 2. gallery-dl for image posts
    print("[gallery-dl] trying for Instagram images...")
    gdl_result = _gallery_dl_download(url_clean)
    if gdl_result.ok:
        return gdl_result

    # 3. Instagram scraper fallback (embed page, proxy frontends)
    print("[instagram] trying Instagram-specific fallback...")
    ig_result = instagram_download(url)
    if ig_result and ig_result.ok:
        return ig_result

    # No browser fallback for Instagram — it captures login wall garbage
    return DownloadResult(
        error="Instagram requires login. This content is not accessible without authentication."
    )


def _sync_download_generic(url, mode):
    """Generic flow for non-Instagram URLs."""
    # 1. Try yt-dlp
    try:
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
    except Exception as exc:
        print(f"[yt-dlp] all attempts failed for {url}: {exc}")

    # 2. Try gallery-dl
    print("[gallery-dl] trying fallback...")
    gdl_result = _gallery_dl_download(url)
    if gdl_result.ok:
        return gdl_result

    # 3. Try platform-specific scrapers
    if is_facebook_url(url):
        print("[facebook] trying Facebook-specific fallback...")
        fb_result = facebook_download(url)
        if fb_result and fb_result.ok:
            return fb_result

    # 4. Last resort: headless browser
    print("[browser] trying headless browser fallback...")
    try:
        from browser import browser_download
        br_result = asyncio.run(browser_download(url))
        if br_result and br_result.ok:
            return br_result
    except Exception as exc:
        print(f"[browser] fallback failed: {exc}")

    return DownloadResult(error="All download methods failed")


def _download_highest(url, title, info=None):
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

    fallback_url = (info or {}).get("url") or (info or {}).get("webpage_url") or url
    return DownloadResult(stream_url=fallback_url, title=title)


def _download_lowest(url, title):
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
