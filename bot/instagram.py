"""Instagram-specific download strategies that work without authentication.

Fallback chain:
1. Embed page scraping (/p/XXX/embed/) — least restricted endpoint
2. DDInstagram proxy — third-party frontend that serves raw media
3. Direct page scraping — extract CDN URLs from page HTML/JSON
"""

import os
import re
import uuid

import httpx

from config import DOWNLOAD_DIR, PROXY

_TIMEOUT = 30

_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)

_VIDEO_URL_RE = re.compile(r'"video_url"\s*:\s*"(https?:[^"]+)"')
_DISPLAY_URL_RE = re.compile(r'"display_url"\s*:\s*"(https?:[^"]+)"')
_DISPLAY_SRC_RE = re.compile(r'"display_src"\s*:\s*"(https?:[^"]+)"')
_OG_VIDEO_RE = re.compile(r'<meta\s+(?:property|name)="og:video"\s+content="(https?:[^"]+)"', re.I)
_OG_IMAGE_RE = re.compile(r'<meta\s+(?:property|name)="og:image"\s+content="(https?:[^"]+)"', re.I)
_EMBED_VIDEO_RE = re.compile(r'<video[^>]+src="(https?:[^"]+)"', re.I)

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


def _get_shortcode(url):
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _unescape(url):
    return url.replace("\\u0026", "&").replace("\\/", "/").replace("\\", "")


def _clean_ig_url(url):
    """Strip tracking params from Instagram URLs."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    strip = {"igsh", "igshid", "img_index", "utm_source", "utm_medium", "fbclid", "ref"}
    cleaned = {k: v for k, v in params.items() if k not in strip}
    return urlunparse(parsed._replace(query=urlencode(cleaned, doseq=True)))


def _fetch(url, headers=None):
    """Fetch a URL, trying with proxy first, then without."""
    h = headers or HEADERS
    if PROXY:
        try:
            with httpx.Client(timeout=_TIMEOUT, headers=h, follow_redirects=True, proxy=PROXY) as client:
                resp = client.get(url)
                if resp.status_code < 400:
                    return resp
        except Exception as exc:
            print(f"[instagram] proxy fetch failed, trying direct: {exc}")
    with httpx.Client(timeout=_TIMEOUT, headers=h, follow_redirects=True) as client:
        return client.get(url)


def _download_url(media_url, ext="jpg"):
    """Download a single URL to the temp directory."""
    uid = uuid.uuid4().hex[:12]
    filepath = os.path.join(DOWNLOAD_DIR, f"{uid}.{ext}")
    resp = _fetch(media_url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "video" in content_type:
        filepath = filepath.rsplit(".", 1)[0] + ".mp4"
    elif "png" in content_type:
        filepath = filepath.rsplit(".", 1)[0] + ".png"
    with open(filepath, "wb") as f:
        f.write(resp.content)
    return filepath


def _download_urls(media_urls, ext="jpg"):
    """Download multiple URLs, return list of file paths."""
    paths = []
    for i, url in enumerate(media_urls):
        uid = uuid.uuid4().hex[:12]
        filepath = os.path.join(DOWNLOAD_DIR, f"{uid}_{i:02d}.{ext}")
        try:
            resp = _fetch(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "video" in content_type:
                filepath = filepath.rsplit(".", 1)[0] + ".mp4"
            with open(filepath, "wb") as f:
                f.write(resp.content)
            paths.append(filepath)
        except Exception as exc:
            print(f"[instagram] failed to download {url}: {exc}")
    return paths


# ── Strategy 1: Embed page ────────────────────────────────────────────

def _try_embed(shortcode):
    """Scrape Instagram's embed endpoint for media URLs."""
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
    print(f"[instagram] trying embed: {embed_url}")

    resp = _fetch(embed_url)
    if resp.status_code != 200:
        print(f"[instagram] embed returned {resp.status_code}")
        return None
    html = resp.text
    print(f"[instagram] embed page len={len(html)}")

    # Try video first
    videos = _EMBED_VIDEO_RE.findall(html) or _OG_VIDEO_RE.findall(html) or _VIDEO_URL_RE.findall(html)
    if videos:
        url = _unescape(videos[0])
        print(f"[instagram] embed found video: {url[:80]}...")
        filepath = _download_url(url, "mp4")
        return {"type": "video", "files": [filepath]}

    # Try images from embedded JSON data
    images = _DISPLAY_URL_RE.findall(html) or _DISPLAY_SRC_RE.findall(html)
    if images:
        urls = list(dict.fromkeys(_unescape(u) for u in images))
        print(f"[instagram] embed found {len(urls)} image(s)")
        paths = _download_urls(urls)
        if paths:
            return {"type": "image", "files": paths}

    # Try og:image as last resort
    og_imgs = _OG_IMAGE_RE.findall(html)
    if og_imgs:
        url = _unescape(og_imgs[0])
        print(f"[instagram] embed found og:image: {url[:80]}...")
        filepath = _download_url(url)
        return {"type": "image", "files": [filepath]}

    print("[instagram] embed: no media found in HTML")
    return None


# ── Strategy 2: DDInstagram / InstaFix proxy ──────────────────────────

_PROXY_FRONTENDS = [
    ("ddinstagram.com", "d.ddinstagram.com"),
    ("imginn.com", "imginn.com"),
]


def _try_proxy_frontend(shortcode, original_url):
    """Try third-party Instagram proxy frontends that serve raw media."""
    clean_url = _clean_ig_url(original_url)

    for name, domain in _PROXY_FRONTENDS:
        try:
            proxy_url = re.sub(
                r"(https?://)(?:www\.)?instagram\.com",
                f"https://{domain}",
                clean_url,
            )
            print(f"[instagram] trying proxy frontend: {proxy_url}")

            resp = _fetch(proxy_url)
            if resp.status_code != 200:
                print(f"[instagram] {name} returned {resp.status_code}")
                continue
            html = resp.text

            videos = _OG_VIDEO_RE.findall(html) or _VIDEO_URL_RE.findall(html)
            if videos:
                url = _unescape(videos[0])
                print(f"[instagram] {name} found video")
                filepath = _download_url(url, "mp4")
                return {"type": "video", "files": [filepath]}

            images = _OG_IMAGE_RE.findall(html) or _DISPLAY_URL_RE.findall(html)
            if images:
                urls = list(dict.fromkeys(_unescape(u) for u in images))
                print(f"[instagram] {name} found {len(urls)} image(s)")
                paths = _download_urls(urls)
                if paths:
                    return {"type": "image", "files": paths}

        except Exception as exc:
            print(f"[instagram] {name} failed: {exc}")
            continue

    return None


# ── Strategy 3: Direct page JSON extraction ───────────────────────────

def _try_direct_scrape(shortcode):
    """Fetch the Instagram post page and extract CDN URLs from embedded JSON."""
    post_url = f"https://www.instagram.com/p/{shortcode}/"
    print(f"[instagram] trying direct scrape: {post_url}")

    try:
        mobile_headers = {
            **HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        }
        resp = _fetch(post_url, headers=mobile_headers)
        if resp.status_code != 200:
            print(f"[instagram] direct scrape returned {resp.status_code}")
            return None
        html = resp.text
        print(f"[instagram] direct scrape page len={len(html)}")

        videos = _VIDEO_URL_RE.findall(html)
        if videos:
            url = _unescape(videos[0])
            print(f"[instagram] direct scrape found video")
            filepath = _download_url(url, "mp4")
            return {"type": "video", "files": [filepath]}

        images = _DISPLAY_URL_RE.findall(html) or _DISPLAY_SRC_RE.findall(html)
        if images:
            urls = list(dict.fromkeys(_unescape(u) for u in images))
            print(f"[instagram] direct scrape found {len(urls)} image(s)")
            paths = _download_urls(urls)
            if paths:
                return {"type": "image", "files": paths}

        og_imgs = _OG_IMAGE_RE.findall(html)
        if og_imgs:
            url = _unescape(og_imgs[0])
            filepath = _download_url(url)
            return {"type": "image", "files": [filepath]}

    except Exception as exc:
        print(f"[instagram] direct scrape failed: {exc}")

    return None


# ── Public API ────────────────────────────────────────────────────────

def is_instagram_url(url):
    return "instagram.com/" in url or "instagr.am/" in url


def instagram_download(url):
    """Try all Instagram strategies. Returns a DownloadResult or None."""
    from downloader import DownloadResult

    url = _clean_ig_url(url)
    shortcode = _get_shortcode(url)
    if not shortcode:
        print(f"[instagram] could not extract shortcode from {url}")
        return None

    print(f"[instagram] shortcode: {shortcode}")

    strategies = [
        ("embed", lambda: _try_embed(shortcode)),
        ("proxy_frontend", lambda: _try_proxy_frontend(shortcode, url)),
        ("direct_scrape", lambda: _try_direct_scrape(shortcode)),
    ]

    for name, strategy in strategies:
        try:
            result = strategy()
            if result and result["files"]:
                files = result["files"]
                is_video = result["type"] == "video"

                if len(files) == 1:
                    return DownloadResult(
                        filepath=files[0],
                        is_image=not is_video,
                        is_audio=False,
                        title="instagram_media",
                    )
                else:
                    return DownloadResult(
                        filepaths=files,
                        is_image=True,
                        title="instagram_carousel",
                    )
        except Exception as exc:
            print(f"[instagram] strategy '{name}' exception: {exc}")
            continue

    print("[instagram] all strategies exhausted")
    return None
