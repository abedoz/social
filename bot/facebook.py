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

# Facebook escapes URLs as https:\/\/... in JS — match both forms
_URL = r'https?:[^"]+?'  # catches both https:// and https:\/\/

_HD_SRC_RE = re.compile(r'hd_src\s*[":]\s*"(' + _URL + r')"')
_SD_SRC_RE = re.compile(r'sd_src\s*[":]\s*"(' + _URL + r')"')
_PLAYABLE_HD_RE = re.compile(r'"playable_url_quality_hd"\s*:\s*"(' + _URL + r')"')
_PLAYABLE_SD_RE = re.compile(r'"playable_url"\s*:\s*"(' + _URL + r')"')
_BROWSER_URL_RE = re.compile(r'"browser_native_(?:hd|sd)_url"\s*:\s*"(' + _URL + r')"')
_OG_VIDEO_RE = re.compile(r'<meta\s+property="og:video(?::url)?"\s+content="(' + _URL + r')"', re.I)
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="(' + _URL + r')"', re.I)
# Broad catch: any fbcdn video URL in any context
_FBCDN_VIDEO_RE = re.compile(r'"(https?:\\?/\\?/video[^"]*?fbcdn\.net[^"]+)"')
# Even broader: any escaped fbcdn URL containing /v/ (video segments)
_FBCDN_ANY_RE = re.compile(r'"(https?:\\?/\\?/[^"]*?fbcdn\.net\\?/v\\?/[^"]+)"')

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

# Social media embed bots get special treatment — Facebook serves them og:video
BOT_USER_AGENTS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "TelegramBot (like TwitterBot)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "WhatsApp/2.23.20.0",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
]


def _unescape(url):
    return url.replace("\\u0025", "%").replace("\\u0026", "&").replace("\\/", "/").replace("\\", "")


def _make_client(**extra):
    kwargs = {"timeout": _TIMEOUT, "headers": HEADERS, "follow_redirects": True}
    if PROXY:
        kwargs["proxy"] = PROXY
    kwargs.update(extra)
    try:
        client = httpx.Client(**kwargs)
        # Test the connection
        return client
    except Exception:
        pass
    # Fall back to no proxy
    kwargs.pop("proxy", None)
    return httpx.Client(**kwargs)


def _fetch(url, headers=None):
    """Fetch a URL, trying with proxy first, then without."""
    h = headers or HEADERS
    # Try with proxy
    if PROXY:
        try:
            with httpx.Client(timeout=_TIMEOUT, headers=h, follow_redirects=True, proxy=PROXY) as client:
                resp = client.get(url)
                return resp
        except Exception as exc:
            print(f"[facebook] proxy fetch failed, trying direct: {exc}")
    # Try without proxy
    with httpx.Client(timeout=_TIMEOUT, headers=h, follow_redirects=True) as client:
        return client.get(url)


def _download_video_url(video_url):
    """Download a video from a direct CDN URL."""
    uid = uuid.uuid4().hex[:12]
    filepath = os.path.join(DOWNLOAD_DIR, f"{uid}.mp4")
    resp = _fetch(video_url)
    resp.raise_for_status()
    with open(filepath, "wb") as f:
        f.write(resp.content)
    if os.path.getsize(filepath) < 1000:
        os.remove(filepath)
        return None
    return filepath


def _extract_best_video(html):
    """Extract the best quality video URL from HTML source."""
    patterns = [
        ("playable_url_quality_hd", _PLAYABLE_HD_RE),
        ("hd_src", _HD_SRC_RE),
        ("browser_native", _BROWSER_URL_RE),
        ("playable_url", _PLAYABLE_SD_RE),
        ("sd_src", _SD_SRC_RE),
        ("fbcdn_video", _FBCDN_VIDEO_RE),
        ("fbcdn_any", _FBCDN_ANY_RE),
        ("og:video", _OG_VIDEO_RE),
    ]
    for name, pattern in patterns:
        matches = pattern.findall(html)
        if matches:
            url = _unescape(matches[0])
            print(f"[facebook] pattern '{name}' matched: {url[:100]}...")
            if "fbcdn" in url or "video" in url or "scontent" in url:
                return url

    # Debug: check if fbcdn exists at all in the page
    fbcdn_count = html.count("fbcdn")
    video_count = html.count("video_url")
    playable_count = html.count("playable_url")
    print(f"[facebook] page stats: fbcdn={fbcdn_count} video_url={video_count} playable_url={playable_count}")

    return None


# ── Strategy 1: Resolve share URL ─────────────────────────────────────

def _resolve_url(url):
    """Follow redirects to get the actual Facebook video page URL."""
    try:
        resp = _fetch(url)
        resolved = str(resp.url)
        if resolved != url:
            print(f"[facebook] resolved: {url} → {resolved}")
        return resolved
    except Exception as exc:
        print(f"[facebook] resolve failed: {exc}")
        return url


# ── Strategy 1b: Embed bot User-Agents ────────────────────────────────

def _try_bot_ua(url):
    """Fetch page pretending to be a social embed bot (Telegram, Discord, etc.).
    Facebook serves og:video tags to these bots for link previews."""
    print(f"[facebook] trying bot UA strategy...")

    for ua in BOT_USER_AGENTS:
        bot_name = ua.split("/")[0].split("(")[0].strip()
        try:
            headers = {"User-Agent": ua, "Accept": "*/*"}
            resp = _fetch(url, headers=headers)
            if resp.status_code != 200:
                print(f"[facebook] {bot_name}: status {resp.status_code}")
                continue
            html = resp.text
            print(f"[facebook] {bot_name}: page len={len(html)}")

            # Look for og:video — this is what embed bots get
            og_videos = _OG_VIDEO_RE.findall(html)
            if og_videos:
                video_url = _unescape(og_videos[0])
                print(f"[facebook] {bot_name} found og:video: {video_url[:80]}...")
                filepath = _download_video_url(video_url)
                if filepath:
                    return {"type": "video", "file": filepath}

            # Also check for any video URL in the response
            video_url = _extract_best_video(html)
            if video_url:
                print(f"[facebook] {bot_name} found video URL: {video_url[:80]}...")
                filepath = _download_video_url(video_url)
                if filepath:
                    return {"type": "video", "file": filepath}

        except Exception as exc:
            print(f"[facebook] {bot_name} failed: {exc}")
            continue

    return None


# ── Strategy 1c: Facebook video embed plugin ──────────────────────────

def _try_embed_plugin(url):
    """Use Facebook's embed plugin endpoint which serves video for embedding."""
    # Extract video ID from URL
    video_id = re.search(r'/(?:reel|video|watch)/(\d+)', url)
    if not video_id:
        video_id = re.search(r'[?&]v=(\d+)', url)
    if not video_id:
        return None

    vid = video_id.group(1)
    embed_url = f"https://www.facebook.com/plugins/video.php?href=https%3A%2F%2Fwww.facebook.com%2Freel%2F{vid}&show_text=false"
    print(f"[facebook] trying embed plugin: {embed_url}")

    try:
        resp = _fetch(embed_url)
        if resp.status_code != 200:
            print(f"[facebook] embed plugin returned {resp.status_code}")
            return None
        html = resp.text
        print(f"[facebook] embed plugin page len={len(html)}")

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] embed plugin found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}

        # Check og:video
        og_videos = _OG_VIDEO_RE.findall(html)
        if og_videos:
            video_url = _unescape(og_videos[0])
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}

    except Exception as exc:
        print(f"[facebook] embed plugin failed: {exc}")
    return None


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
        resp = _fetch(mbasic_url, headers=MOBILE_HEADERS)
        if resp.status_code != 200:
            print(f"[facebook] mbasic returned {resp.status_code}")
            return None
        html = resp.text
        print(f"[facebook] mbasic page length: {len(html)}")

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
            resp = _fetch(redirect_url, headers=MOBILE_HEADERS)
            final_url = str(resp.url)
            if "fbcdn.net" in final_url or "video" in final_url:
                filepath = _download_video_url(final_url)
                if filepath:
                    return {"type": "video", "file": filepath}
        else:
            print("[facebook] mbasic: no video_redirect found in HTML")

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
        resp = _fetch(mobile_url, headers=MOBILE_HEADERS)
        if resp.status_code != 200:
            print(f"[facebook] mobile returned {resp.status_code}")
            return None
        html = resp.text
        print(f"[facebook] mobile page length: {len(html)}")

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] mobile found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}
        else:
            print("[facebook] mobile: no video URL found in page source")

    except Exception as exc:
        print(f"[facebook] mobile failed: {exc}")
    return None


# ── Strategy 4: Desktop page scrape ───────────────────────────────────

def _try_desktop(url):
    """Scrape the desktop page — most data in inline JS/JSON."""
    print(f"[facebook] trying desktop: {url}")

    try:
        resp = _fetch(url)
        if resp.status_code != 200:
            print(f"[facebook] desktop returned {resp.status_code}")
            return None
        html = resp.text
        print(f"[facebook] desktop page length: {len(html)}")

        video_url = _extract_best_video(html)
        if video_url:
            print(f"[facebook] desktop found video: {video_url[:80]}...")
            filepath = _download_video_url(video_url)
            if filepath:
                return {"type": "video", "file": filepath}
        else:
            print("[facebook] desktop: no video URL found in page source")

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
        ("bot_ua", lambda: _try_bot_ua(resolved)),
        ("embed_plugin", lambda: _try_embed_plugin(resolved)),
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
