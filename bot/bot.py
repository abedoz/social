import os
import re
import shutil
import subprocess
import uuid

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, PROXY
from downloader import download_media, MODE_HIGHEST, MODE_LOWEST, MODE_AUDIO, MODE_VIDEO
from facebook import is_facebook_url, facebook_download
from instagram import is_instagram_url, instagram_download

URL_PATTERN = re.compile(r"https?://[^\s]+")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    urls = URL_PATTERN.findall(update.message.text)
    if not urls:
        return

    for url in urls:
        key = uuid.uuid4().hex[:8]
        context.bot_data[key] = url

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Best Quality", callback_data=f"{MODE_HIGHEST}:{key}")],
            [InlineKeyboardButton("Lowest Quality", callback_data=f"{MODE_LOWEST}:{key}")],
            [InlineKeyboardButton("Audio Only", callback_data=f"{MODE_AUDIO}:{key}")],
            [InlineKeyboardButton("Video Only", callback_data=f"{MODE_VIDEO}:{key}")],
        ])

        await update.message.reply_text("Choose download format:", reply_markup=keyboard)


async def _send_result(result, query):
    """Send the download result back to the chat."""
    # Multiple images (carousel)
    if result.filepaths:
        try:
            # Telegram allows max 10 items per media group
            for i in range(0, len(result.filepaths), 10):
                batch = result.filepaths[i:i + 10]
                media = [InputMediaPhoto(open(f, "rb")) for f in batch]
                await query.message.reply_media_group(media=media)
            await query.delete_message()
        finally:
            for f in result.filepaths:
                if os.path.exists(f):
                    os.remove(f)
            # Clean up the gallery-dl subdirectory
            for f in result.filepaths:
                parent = os.path.dirname(f)
                if parent != os.path.dirname(parent) and os.path.isdir(parent):
                    shutil.rmtree(parent, ignore_errors=True)
                    break
        return

    # Single file
    try:
        if result.is_image:
            await query.message.reply_photo(photo=open(result.filepath, "rb"))
        elif result.is_audio:
            await query.message.reply_audio(
                audio=open(result.filepath, "rb"),
                title=result.title,
            )
        else:
            await query.message.reply_video(
                video=open(result.filepath, "rb"),
                supports_streaming=True,
            )
        await query.delete_message()
    finally:
        if result.filepath and os.path.exists(result.filepath):
            os.remove(result.filepath)
            parent = os.path.dirname(result.filepath)
            if parent.startswith(os.path.join("/tmp", "tgbot")) and os.path.isdir(parent):
                try:
                    os.rmdir(parent)
                except OSError:
                    pass


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        mode, key = query.data.split(":", 1)
    except ValueError:
        await query.edit_message_text("Invalid selection.")
        return

    url = context.bot_data.pop(key, None)
    if not url:
        await query.edit_message_text("This link has expired. Please send it again.")
        return

    await query.edit_message_text("Downloading...")

    try:
        result = await download_media(url, mode)

        if not result.ok:
            if result.error == "timeout":
                await query.edit_message_text("Download timed out.")
            else:
                error_detail = result.error or "Unknown error"
                if len(error_detail) > 300:
                    error_detail = error_detail[:300] + "..."
                await query.edit_message_text(
                    f"Download failed.\n\nError: {error_detail}"
                )
            return

        if result.stream_url and not result.filepath and not result.filepaths:
            await query.edit_message_text(
                f"File too large for Telegram. Direct link:\n{result.stream_url}"
            )
            return

        await _send_result(result, query)

    except Exception as exc:
        print(f"Unhandled error for {url}: {exc}")
        try:
            await query.edit_message_text(
                "Could not download this link. "
                "The platform may not be supported or the content is private."
            )
        except Exception:
            pass


async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command: /test <url> — runs each download strategy and reports results."""
    if not context.args:
        await update.message.reply_text("Usage: /test <url>")
        return

    url = context.args[0]
    lines = [f"Testing: {url}\n"]

    # Step 1: resolve URL
    if is_facebook_url(url):
        from facebook import _resolve_url
        resolved = _resolve_url(url)
        lines.append(f"Resolved: {resolved}")
    else:
        resolved = url

    # Step 2: yt-dlp extract
    import yt_dlp
    from downloader import _base_opts
    try:
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(resolved, download=False)
            lines.append(f"yt-dlp: OK — {info.get('title', '?')}")
    except Exception as exc:
        lines.append(f"yt-dlp: FAILED — {str(exc)[:200]}")

    # Step 3: gallery-dl
    from downloader import _gallery_dl_download
    gdl = _gallery_dl_download(resolved)
    if gdl.ok:
        lines.append(f"gallery-dl: OK — {gdl.filepath or gdl.filepaths}")
        # clean up
        if gdl.filepath and os.path.exists(gdl.filepath):
            os.remove(gdl.filepath)
        for f in gdl.filepaths:
            if os.path.exists(f):
                os.remove(f)
    else:
        lines.append(f"gallery-dl: FAILED — {gdl.error}")

    # Step 4: platform-specific
    if is_facebook_url(url):
        fb = facebook_download(url)
        if fb and fb.ok:
            lines.append(f"facebook: OK — {fb.filepath}")
            if fb.filepath and os.path.exists(fb.filepath):
                os.remove(fb.filepath)
        else:
            lines.append("facebook: FAILED")
    elif is_instagram_url(url):
        ig = instagram_download(url)
        if ig and ig.ok:
            lines.append(f"instagram: OK — {ig.filepath or ig.filepaths}")
            if ig.filepath and os.path.exists(ig.filepath):
                os.remove(ig.filepath)
            for f in ig.filepaths:
                if os.path.exists(f):
                    os.remove(f)
        else:
            lines.append("instagram: FAILED")

    # Step 5: HTTP fetch + page analysis
    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(resolved)
            html = resp.text
            lines.append(f"HTTP GET: {resp.status_code} (len={len(html)})")

            # Page analysis — what keywords exist
            lines.append(f"\nPage analysis:")
            lines.append(f"  'fbcdn': {html.count('fbcdn')}")
            lines.append(f"  'playable_url': {html.count('playable_url')}")
            lines.append(f"  'video_url': {html.count('video_url')}")
            lines.append(f"  'hd_src': {html.count('hd_src')}")
            lines.append(f"  'sd_src': {html.count('sd_src')}")
            lines.append(f"  'og:video': {html.count('og:video')}")
            lines.append(f"  'browser_native': {html.count('browser_native')}")

            # Try to find any fbcdn video URL
            import re
            fbcdn_urls = re.findall(r'(https?:[^"]*?fbcdn[^"]*?video[^"]{0,200})', html)
            if fbcdn_urls:
                lines.append(f"\nFound {len(fbcdn_urls)} fbcdn video URL(s):")
                for u in fbcdn_urls[:3]:
                    lines.append(f"  {u[:120]}...")
            else:
                # Any fbcdn URL at all?
                any_fbcdn = re.findall(r'(https?:[^"]*?fbcdn\.net[^"]{0,100})', html)
                if any_fbcdn:
                    lines.append(f"\nFound {len(any_fbcdn)} fbcdn URLs (no 'video' in path):")
                    for u in any_fbcdn[:3]:
                        lines.append(f"  {u[:120]}...")
                else:
                    lines.append("\nNo fbcdn URLs found in page")

    except Exception as exc:
        lines.append(f"HTTP GET: FAILED — {str(exc)[:150]}")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "..."
    await update.message.reply_text(msg)

    # Send page analysis as a separate message for Facebook URLs
    if is_facebook_url(url):
        analysis = ["Page analysis:\n"]
        import re as _re
        from facebook import _unescape, BOT_USER_AGENTS

        # Test bot UAs — the key strategy
        for ua in BOT_USER_AGENTS[:3]:
            bot_name = ua.split("/")[0].split("(")[0].strip()
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={
                    "User-Agent": ua, "Accept": "*/*"
                }) as client:
                    r = await client.get(resolved)
                    h = r.text
                    og_count = h.count("og:video")
                    fbcdn_count = h.count("fbcdn")
                    analysis.append(f"[{bot_name}] {r.status_code} len={len(h)} og:video={og_count} fbcdn={fbcdn_count}")
                    if og_count:
                        og = _re.findall(r'content="(https?:[^"]+)"[^>]*property="og:video', h)
                        if not og:
                            og = _re.findall(r'property="og:video[^"]*"\s+content="([^"]+)"', h)
                        if og:
                            analysis.append(f"  → {_unescape(og[0][:150])}")
            except Exception as exc:
                analysis.append(f"[{bot_name}] FAILED: {str(exc)[:80]}")

        amsg = "\n".join(analysis)
        if len(amsg) > 4000:
            amsg = amsg[:4000] + "..."
        await update.message.reply_text(amsg)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            direct_ip = resp.json().get("ip", "unknown")
        lines.append(f"Direct IP: {direct_ip}")
    except Exception as exc:
        lines.append(f"Direct IP: failed ({exc})")

    try:
        async with httpx.AsyncClient(proxy="socks5://localhost:1055", timeout=10) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            lines.append(f"SOCKS5:    {resp.json().get('ip', '?')} (localhost:1055)")
    except Exception as exc:
        lines.append(f"SOCKS5:    failed ({exc})")

    try:
        async with httpx.AsyncClient(proxy="http://localhost:1056", timeout=10) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            lines.append(f"HTTP prx:  {resp.json().get('ip', '?')} (localhost:1056)")
    except Exception as exc:
        lines.append(f"HTTP prx:  failed ({exc})")

    try:
        ts = subprocess.run(["tailscale", "status", "--json=false"],
                            capture_output=True, text=True, timeout=5)
        lines.append(f"\nTailscale:\n{ts.stdout.strip() or ts.stderr.strip()}")
    except FileNotFoundError:
        lines.append("\nTailscale: not installed")
    except Exception as exc:
        lines.append(f"\nTailscale: error ({exc})")

    await update.message.reply_text("\n".join(lines))


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("test", handle_test))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("Bot started. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
