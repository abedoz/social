import os
import re

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from config import BOT_TOKEN
from downloader import download_media

URL_PATTERN = re.compile(r"https?://[^\s]+")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    urls = URL_PATTERN.findall(update.message.text)
    if not urls:
        return

    for url in urls:
        status_msg = await update.message.reply_text("Downloading...")

        try:
            result = await download_media(url)

            if not result.ok:
                if result.error == "timeout":
                    await status_msg.edit_text("Download timed out.")
                else:
                    await status_msg.edit_text(
                        "Could not download this link. "
                        "The platform may not be supported or the content is private."
                    )
                continue

            # Fallback: send stream URL if no file was downloaded
            if result.stream_url and not result.filepath:
                await status_msg.edit_text(
                    f"File too large for Telegram. Direct link:\n{result.stream_url}"
                )
                continue

            # Send the file
            try:
                if result.is_audio:
                    await update.message.reply_audio(
                        audio=open(result.filepath, "rb"),
                        title=result.title,
                    )
                else:
                    await update.message.reply_video(
                        video=open(result.filepath, "rb"),
                        supports_streaming=True,
                    )
                await status_msg.delete()
            finally:
                # Always clean up the temp file
                if result.filepath and os.path.exists(result.filepath):
                    os.remove(result.filepath)

        except Exception as exc:
            print(f"Unhandled error for {url}: {exc}")
            try:
                await status_msg.edit_text(
                    "Could not download this link. "
                    "The platform may not be supported or the content is private."
                )
            except Exception:
                pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot started. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
