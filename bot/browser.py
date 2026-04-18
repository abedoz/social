"""Headless browser fallback using Playwright.

Last-resort strategy when yt-dlp, gallery-dl, and platform-specific
scrapers all fail. Loads the page in Chromium, intercepts network
requests to capture video/image CDN URLs, and downloads them.
"""

import asyncio
import os
import re
import uuid

from config import DOWNLOAD_DIR, MAX_FILE_MB

MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

# CDN patterns for video/image URLs from social platforms
_VIDEO_CDN_RE = re.compile(
    r"https?://[^\s\"']*(?:"
    r"fbcdn\.net/v/|"
    r"video\.cdninstagram\.com|"
    r"scontent\.cdninstagram\.com|"
    r"scontent[^/]*\.fbcdn\.net|"
    r"video[^/]*\.fbcdn\.net|"
    r"tiktokcdn\.com|"
    r"tiktokv\.com|"
    r"muscdn\.com"
    r")[^\s\"']*",
    re.I,
)

_IMAGE_CDN_RE = re.compile(
    r"https?://[^\s\"']*(?:"
    r"cdninstagram\.com|"
    r"scontent[^/]*\.fbcdn\.net"
    r")[^\s\"']*\.(?:jpg|jpeg|png|webp)",
    re.I,
)


def _is_video_url(url):
    """Check if a URL looks like a video stream."""
    return any(x in url.lower() for x in [
        ".mp4", "/v/", "video", "mime=video", "bytestart",
    ])


async def browser_download(url):
    """Load a URL in headless Chromium and capture video/image from network traffic."""
    from downloader import DownloadResult

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[browser] playwright not installed")
        return None

    print(f"[browser] launching headless Chromium for: {url}")

    captured_videos = []
    captured_images = []

    def on_response(response):
        resp_url = response.url
        content_type = response.headers.get("content-type", "")

        if "video" in content_type or _is_video_url(resp_url):
            if resp_url not in captured_videos:
                captured_videos.append(resp_url)
                print(f"[browser] captured video: {resp_url[:100]}")
        elif _VIDEO_CDN_RE.match(resp_url):
            if resp_url not in captured_videos:
                captured_videos.append(resp_url)
                print(f"[browser] captured CDN video: {resp_url[:100]}")
        elif "image" in content_type and _IMAGE_CDN_RE.match(resp_url):
            if resp_url not in captured_images:
                captured_images.append(resp_url)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",
                    "--disable-extensions",
                ],
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )

            page = await context.new_page()
            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"[browser] page load error (continuing): {exc}")

            # Wait for video player to initialize and start loading
            await asyncio.sleep(5)

            # Try clicking play button if visible
            for selector in [
                'div[data-sigil="playInlineVideo"]',
                'div[aria-label="Play"]',
                'video',
                '[role="button"][aria-label*="Play"]',
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.click()
                        print(f"[browser] clicked: {selector}")
                        await asyncio.sleep(3)
                        break
                except Exception:
                    continue

            # Also try to extract video src from DOM
            try:
                video_srcs = await page.evaluate("""
                    () => {
                        const videos = document.querySelectorAll('video');
                        const srcs = [];
                        videos.forEach(v => {
                            if (v.src) srcs.push(v.src);
                            const sources = v.querySelectorAll('source');
                            sources.forEach(s => { if (s.src) srcs.push(s.src); });
                        });
                        return srcs;
                    }
                """)
                for src in video_srcs:
                    if src and src.startswith("http") and src not in captured_videos:
                        captured_videos.append(src)
                        print(f"[browser] DOM video src: {src[:100]}")
            except Exception:
                pass

            await browser.close()

    except Exception as exc:
        print(f"[browser] error: {exc}")
        return None

    # Download the best captured video
    if captured_videos:
        print(f"[browser] captured {len(captured_videos)} video URL(s), downloading best...")
        for video_url in captured_videos:
            try:
                filepath = await _download_captured(video_url)
                if filepath:
                    return DownloadResult(filepath=filepath, is_audio=False, title="media")
            except Exception as exc:
                print(f"[browser] download failed for {video_url[:80]}: {exc}")
                continue

    # Fall back to images if no video
    if captured_images:
        print(f"[browser] no video, trying {len(captured_images)} captured image(s)...")
        paths = []
        for img_url in captured_images[:10]:
            try:
                filepath = await _download_captured(img_url, ext="jpg")
                if filepath:
                    paths.append(filepath)
            except Exception:
                continue
        if len(paths) == 1:
            return DownloadResult(filepath=paths[0], is_image=True, title="image")
        elif paths:
            return DownloadResult(filepaths=paths, is_image=True, title="carousel")

    print("[browser] no video or image URLs captured")
    return None


async def _download_captured(url, ext="mp4"):
    """Download a captured CDN URL."""
    import httpx

    uid = uuid.uuid4().hex[:12]
    filepath = os.path.join(DOWNLOAD_DIR, f"br_{uid}.{ext}")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "video" in content_type and ext != "mp4":
            filepath = filepath.rsplit(".", 1)[0] + ".mp4"
        elif "image" in content_type and ext == "mp4":
            filepath = filepath.rsplit(".", 1)[0] + ".jpg"

        with open(filepath, "wb") as f:
            f.write(resp.content)

    size = os.path.getsize(filepath)
    if size < 1000:
        os.remove(filepath)
        return None
    if size > MAX_FILE_BYTES:
        os.remove(filepath)
        print(f"[browser] file too large: {size} bytes")
        return None

    print(f"[browser] downloaded: {filepath} ({size} bytes)")

    # Remux to proper MP4 for Telegram compatibility
    if filepath.endswith(".mp4"):
        filepath = await _remux_mp4(filepath)

    return filepath


async def _remux_mp4(filepath):
    """Remux video to a Telegram-compatible MP4 using ffmpeg."""
    import subprocess

    out_path = filepath.replace(".mp4", "_fixed.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-c", "copy",
        "-movflags", "+faststart",
        out_path,
    ]
    print(f"[browser] remuxing: {' '.join(cmd)}")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            os.remove(filepath)
            print(f"[browser] remux OK: {out_path}")
            return out_path
        else:
            print(f"[browser] remux failed (code={proc.returncode}), trying transcode...")
            if os.path.exists(out_path):
                os.remove(out_path)
    except Exception as exc:
        print(f"[browser] remux error: {exc}")
        if os.path.exists(out_path):
            os.remove(out_path)

    # Fallback: full transcode
    cmd_transcode = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd_transcode, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            os.remove(filepath)
            print(f"[browser] transcode OK: {out_path}")
            return out_path
        print(f"[browser] transcode failed: {proc.stderr[:200]}")
    except Exception as exc:
        print(f"[browser] transcode error: {exc}")

    if os.path.exists(out_path):
        os.remove(out_path)
    return filepath
