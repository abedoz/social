"""Headless browser fallback using Playwright.

Last-resort strategy when yt-dlp, gallery-dl, and platform-specific
scrapers all fail. Loads the page in Chromium, uses platform-aware
DOM extraction and network interception to capture media.
"""

import asyncio
import os
import re
import subprocess
import uuid

from config import DOWNLOAD_DIR, MAX_FILE_MB

MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

# Minimum file size to keep (filter out tiny thumbnails)
MIN_IMAGE_SIZE = 10000  # 10KB
MIN_VIDEO_SIZE = 50000  # 50KB


def _is_instagram(url):
    return "instagram.com/" in url


def _is_facebook(url):
    return "facebook.com/" in url or "fb.watch" in url


async def browser_download(url):
    """Load a URL in headless Chromium and extract media."""
    from downloader import DownloadResult

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[browser] playwright not installed")
        return None

    print(f"[browser] launching headless Chromium for: {url}")

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

            # Capture video URLs from network traffic
            captured_videos = []

            def on_response(response):
                resp_url = response.url
                content_type = response.headers.get("content-type", "")
                if "video" in content_type:
                    if resp_url not in captured_videos:
                        captured_videos.append(resp_url)
                        print(f"[browser] captured video: {resp_url[:100]}")

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"[browser] page load error (continuing): {exc}")

            await asyncio.sleep(5)

            # Platform-specific extraction
            if _is_instagram(url):
                result = await _extract_instagram(page, captured_videos)
            elif _is_facebook(url):
                result = await _extract_facebook(page, captured_videos)
            else:
                result = await _extract_generic(page, captured_videos)

            await browser.close()

            if result:
                return result

    except Exception as exc:
        print(f"[browser] error: {exc}")

    return None


async def _extract_instagram(page, captured_videos):
    """Extract media from Instagram post/reel page."""
    from downloader import DownloadResult

    print("[browser] Instagram-specific extraction...")

    # Wait for post content to load
    await asyncio.sleep(3)

    # Dismiss login popup if present
    for sel in ['button:has-text("Not Now")', 'button:has-text("Not now")',
                '[role="button"]:has-text("Not Now")']:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                print("[browser] dismissed login popup")
                await asyncio.sleep(1)
                break
        except Exception:
            continue

    # Check for video (Reels)
    if captured_videos:
        print(f"[browser] found {len(captured_videos)} video(s) from network")
        filepath = await _download_and_remux(captured_videos[0])
        if filepath:
            title = await _get_page_title(page)
            return DownloadResult(filepath=filepath, is_audio=False, title=title)

    # Try to get video src from DOM
    try:
        video_src = await page.evaluate("""
            () => {
                const v = document.querySelector('article video, main video');
                return v ? (v.src || v.querySelector('source')?.src) : null;
            }
        """)
        if video_src and video_src.startswith("http"):
            print(f"[browser] DOM video src: {video_src[:80]}")
            filepath = await _download_and_remux(video_src)
            if filepath:
                title = await _get_page_title(page)
                return DownloadResult(filepath=filepath, is_audio=False, title=title)
    except Exception:
        pass

    # Extract images from the post (carousel support)
    image_urls = set()

    # Get images from current view
    await _collect_instagram_images(page, image_urls)

    # Try clicking through carousel slides
    for _ in range(20):  # max 20 slides
        try:
            next_btn = await page.query_selector(
                'button[aria-label="Next"], '
                'button svg[aria-label="Next"],'
                'div[role="button"] svg[aria-label="Next"]'
            )
            if not next_btn:
                # Try parent if we found the SVG
                next_btn = await page.query_selector('button:has(svg[aria-label="Next"])')
            if not next_btn:
                break

            await next_btn.click()
            await asyncio.sleep(1)
            await _collect_instagram_images(page, image_urls)
        except Exception:
            break

    if image_urls:
        urls = list(image_urls)
        print(f"[browser] collected {len(urls)} unique Instagram image(s)")
        paths = []
        for img_url in urls:
            filepath = await _download_captured(img_url, ext="jpg")
            if filepath:
                paths.append(filepath)

        title = await _get_page_title(page)
        if len(paths) == 1:
            return DownloadResult(filepath=paths[0], is_image=True, title=title)
        elif paths:
            return DownloadResult(filepaths=paths, is_image=True, title=title)

    print("[browser] Instagram: no media extracted")
    return None


async def _collect_instagram_images(page, image_urls):
    """Collect high-res image URLs from the current Instagram page view."""
    try:
        urls = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('article img, main img, [role="presentation"] img');
                const urls = [];
                imgs.forEach(img => {
                    const src = img.src || img.getAttribute('srcset')?.split(',').pop()?.trim()?.split(' ')[0];
                    if (src && src.startsWith('http') && !src.includes('s150x150')
                        && !src.includes('s44x44') && !src.includes('s32x32')
                        && !src.includes('/s64x64/') && !src.includes('/s128x128/')
                        && !src.includes('profile_pic') && img.naturalWidth > 200) {
                        urls.push(src);
                    }
                });
                return urls;
            }
        """)
        for u in urls:
            image_urls.add(u)
    except Exception as exc:
        print(f"[browser] collect images error: {exc}")


async def _extract_facebook(page, captured_videos):
    """Extract media from Facebook video/reel page."""
    from downloader import DownloadResult

    print("[browser] Facebook-specific extraction...")
    await asyncio.sleep(3)

    # Dismiss cookie/login popups
    for sel in ['button:has-text("Close")', 'div[aria-label="Close"]',
                'button:has-text("Not Now")', 'button:has-text("Decline")']:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await asyncio.sleep(1)
                break
        except Exception:
            continue

    # Try clicking play
    for sel in ['div[data-sigil="playInlineVideo"]', 'div[aria-label="Play"]',
                '[role="button"][aria-label*="Play"]', 'video']:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                print(f"[browser] clicked play: {sel}")
                await asyncio.sleep(5)
                break
        except Exception:
            continue

    # Check captured videos
    if captured_videos:
        print(f"[browser] found {len(captured_videos)} video(s) from network")
        filepath = await _download_and_remux(captured_videos[0])
        if filepath:
            title = await _get_page_title(page)
            return DownloadResult(filepath=filepath, is_audio=False, title=title)

    # Try DOM video src
    try:
        video_src = await page.evaluate("""
            () => {
                const v = document.querySelector('video[src]');
                return v ? v.src : null;
            }
        """)
        if video_src and video_src.startswith("http"):
            filepath = await _download_and_remux(video_src)
            if filepath:
                title = await _get_page_title(page)
                return DownloadResult(filepath=filepath, is_audio=False, title=title)
    except Exception:
        pass

    print("[browser] Facebook: no media extracted")
    return None


async def _extract_generic(page, captured_videos):
    """Generic extraction for unknown platforms."""
    from downloader import DownloadResult

    print("[browser] generic extraction...")
    await asyncio.sleep(3)

    # Try clicking play
    for sel in ['video', '[aria-label*="Play"]', 'button.play']:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await asyncio.sleep(3)
                break
        except Exception:
            continue

    if captured_videos:
        filepath = await _download_and_remux(captured_videos[0])
        if filepath:
            title = await _get_page_title(page)
            return DownloadResult(filepath=filepath, is_audio=False, title=title)

    # Try DOM
    try:
        video_src = await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                return v ? (v.src || v.querySelector('source')?.src) : null;
            }
        """)
        if video_src and video_src.startswith("http"):
            filepath = await _download_and_remux(video_src)
            if filepath:
                title = await _get_page_title(page)
                return DownloadResult(filepath=filepath, is_audio=False, title=title)
    except Exception:
        pass

    return None


async def _get_page_title(page):
    """Extract a clean title from the page."""
    try:
        # Try meta og:title first
        title = await page.evaluate("""
            () => {
                const meta = document.querySelector('meta[property="og:title"]');
                if (meta) return meta.content;
                const title = document.querySelector('title');
                return title ? title.textContent : '';
            }
        """)
        if title:
            # Clean up common suffixes
            title = re.sub(r'\s*[|•·–-]\s*(Instagram|Facebook|TikTok).*$', '', title)
            title = title.strip()
            if title and len(title) > 3:
                return title
    except Exception:
        pass
    return "media"


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
    min_size = MIN_VIDEO_SIZE if ext == "mp4" else MIN_IMAGE_SIZE
    if size < min_size:
        os.remove(filepath)
        return None
    if size > MAX_FILE_BYTES:
        os.remove(filepath)
        print(f"[browser] file too large: {size} bytes")
        return None

    print(f"[browser] downloaded: {filepath} ({size} bytes)")
    return filepath


async def _download_and_remux(video_url):
    """Download a video and remux to Telegram-compatible MP4."""
    filepath = await _download_captured(video_url, ext="mp4")
    if not filepath:
        return None
    return await _remux_mp4(filepath)


async def _remux_mp4(filepath):
    """Remux video to a Telegram-compatible MP4 using ffmpeg."""
    out_path = filepath.replace(".mp4", "_fixed.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-c", "copy", "-movflags", "+faststart",
        out_path,
    ]
    print(f"[browser] remuxing...")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            os.remove(filepath)
            return out_path
        if os.path.exists(out_path):
            os.remove(out_path)
    except Exception as exc:
        print(f"[browser] remux error: {exc}")
        if os.path.exists(out_path):
            os.remove(out_path)

    # Fallback: full transcode
    try:
        proc = subprocess.run([
            "ffmpeg", "-y", "-i", filepath,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            out_path,
        ], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            os.remove(filepath)
            return out_path
    except Exception as exc:
        print(f"[browser] transcode error: {exc}")

    if os.path.exists(out_path):
        os.remove(out_path)
    return filepath
