"""Facebook-specific download strategies that work without authentication.

Fallback chain:
1. Resolve share URL → retry yt-dlp with the resolved URL
2. Mobile page scrape (mbasic.facebook.com) — lightest HTML, video URLs in page source
3. Desktop page scrape — extract hd_src/sd_src/playable_url from inline JS/JSON
"""

import os
import re
import uuid

import httpx

from config import DOWNLOAD_DIR, PROXY

_TIMEOUT = 30

# Patterns to detect Facebook URLs
_FB_DOMAINS = {"facebook.com", "www.facebook.com", "m.facebook.com",
               "mbasic.facebook.com", "fb.watch", "fb.com", "www.fb.com"}

# Video URL patterns found in Facebook page source
_HD_SRC_RE = re.compile(r'hd_src\s*:\s*"(https?://[^"]+)"')
_SD_SRC_RE = re.compile(r'sd_src\s*:\s*"(https?://[^"]+)"')
_PLAYABLE_HD_RE = re.compile(r'"playable_url_quality_hd"\s*:\s*"(https?://[^"]+)"')
_PLAYABLE_SD_RE = re.compile(r'"playable_url"\s*:\s*"(https?://[^"]+)"')
_BROWSER_URL_RE = re.compile(r'"browser_native_(?:hd|sd)_url"\s*:\s*"(https?://[^"]+)"')
_OG_VIDEO_RE = re.compile(r'<meta\s+property="og:video(?::url)?"\s+content="(https?://[^"]+)"', re.I)
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="(https?://[^"]+)"', re.I)
_VIDEO_DIRECT_RE = re.compile(r'"(https?://video[^"]*fbcdn\.net/[^"]+)"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _unescape(url):
    return url.replace("\\u0025", "%").replace("\\u0026", "&").replace("\\/", "/").replace("\\", "")


def _make_client(**extra):
    kwargs = {"timeout": _TIMEOUT, "headers": HEADERS, "follow_redirects": True}
    if PROXY:
        kwargs["proxy"] = PROXY
    kwargs.update(extra)
    return httpx.Client(**kwargs)


def _download_video_url(video_url):
    """Download a video from a direct CDN URL."""
    uid = uuid.uuid4().hex[:12]
    filepath = os.path.join(DOWNLOAD_DIR, f"{uid}.mp4")
    with _make_client() as client:
        resp = client.get(video_url)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
    if os.path.getsize(filepath) < 1000:
        os.remove(filepath)
        return None
    return filepath


def _extract_best_video(html):
    """Extract the best quality video URL from HTML source."""
    # Try HD first, then SD
    patterns = [
        _PLAYABLE_HD_RE,
        _HD_SRC_RE,
        _BROWSER_URL_RE,
        _PLAYABLE_SD_RE,
        _SD_SRC_RE,
        _VIDEO_DIRECT_RE,
        _OG_VIDEO_RE,
    ]
    for pattern in patterns:
        matches = pattern.findall(html)
        if matches:
            url = _unescape(matches[0])
            if "fbcdn.net" in url or "video" in url:
                return url
    return None


# ── Strategy 1: Resolve share URL ─────────────────────────────────────

def _resolve_url(url):
    """Follow redirects to get the actual Facebook video page URL."""
    try:
        with _make_client() as client:
            resp = client.get(url)
            resolved = str(resp.url)
            if resolved != url:
                print(f"[facebook] resolved: {url} → {resolved}")
            return resolved
    except Exception as exc:
        print(f"[facebook] resolve failed: {exc}")
        return url


# ── Strategy 2: mbasic.facebook.com ───────────────────────────────────

def _try_mbasic(url):
    """Scrape mbasic.facebook.com — serves the simplest HTML with video links."""
    mbasic_url = re.sub(
        r"https?://(?:www\.|m\.)?facebook\.com",
        "https://mbasic.facebook.com",
        url,
    )
    print(f"[facebook] trying mbasic: {mbasic_url}")

    try:
        with _make_client(headers=MOBILE_HEADERS) as client:
            resp = client.get(mbasic_url)
            if resp.status_code != 200:
                print(f"[facebook] mbasic returned {resp.status_code}")
                return None
            html = resp.text

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] mbasic found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}

        # Check for redirect to video page in the HTML
        video_redirect = re.findall(r'href="(/video_redirect/[^"]+)"', html)
        if video_redirect:
            redirect_url = "https://mbasic.facebook.com" + _unescape(video_redirect[0])
            print(f"[facebook] following video redirect...")
            with _make_client(headers=MOBILE_HEADERS) as client:
                resp = client.get(redirect_url)
                final_url = str(resp.url)
                if "fbcdn.net" in final_url or "video" in final_url:
                    filepath = _download_video_url(final_url)
                    if filepath:
                        return {"type": "video", "file": filepath}

    except Exception as exc:
        print(f"[facebook] mbasic failed: {exc}")
    return None


# ── Strategy 3: Mobile page (m.facebook.com) ──────────────────────────

def _try_mobile(url):
    """Scrape m.facebook.com — richer than mbasic, more video data in JS."""
    mobile_url = re.sub(
        r"https?://(?:www\.|mbasic\.)?facebook\.com",
        "https://m.facebook.com",
        url,
    )
    print(f"[facebook] trying mobile: {mobile_url}")

    try:
        with _make_client(headers=MOBILE_HEADERS) as client:
            resp = client.get(mobile_url)
            if resp.status_code != 200:
                print(f"[facebook] mobile returned {resp.status_code}")
                return None
            html = resp.text

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] mobile found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}

    except Exception as exc:
        print(f"[facebook] mobile failed: {exc}")
    return None


# ── Strategy 4: Desktop page scrape ───────────────────────────────────

def _try_desktop(url):
    """Scrape the desktop page — most data in inline JS/JSON."""
    print(f"[facebook] trying desktop: {url}")

    try:
        with _make_client() as client:
            resp = client.get(url)
            if resp.status_code != 200:
                print(f"[facebook] desktop returned {resp.status_code}")
                return None
            html = resp.text

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] desktop found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}

    except Exception as exc:
        print(f"[facebook] desktop failed: {exc}")
    return None


# ── Public API ────────────────────────────────────────────────────────

def is_facebook_url(url):
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    return hostname.replace("www.", "") in _FB_DOMAINS or hostname in _FB_DOMAINS


def facebook_download(url):
    """Try all Facebook strategies. Returns a DownloadResult or None."""
    from downloader import DownloadResult

    # Resolve share/short URLs first
    resolved = _resolve_url(url)

    strategies = [
        ("mbasic", lambda: _try_mbasic(resolved)),
        ("mobile", lambda: _try_mobile(resolved)),
        ("desktop", lambda: _try_desktop(resolved)),
    ]

    for name, strategy in strategies:
        try:
            result = strategy()
            if result and result.get("file"):
                return DownloadResult(
                    filepath=result["file"],
                    is_image=False,
                    is_audio=False,
                    title="facebook_video",
                )
        except Exception as exc:
            print(f"[facebook] strategy '{name}' exception: {exc}")
            continue

    print("[facebook] all strategies exhausted")
    return None
