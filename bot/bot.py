import os
import re
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN
from downloader import download_media, MODE_HIGHEST, MODE_LOWEST, MODE_AUDIO, MODE_VIDEO

URL_PATTERN = re.compile(r"https?://[^\s]+")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    urls = URL_PATTERN.findall(update.message.text)
    if not urls:
        return

    for url in urls:
        # Store URL with a short key to fit Telegram's 64-byte callback_data limit
        key = uuid.uuid4().hex[:8]
        context.bot_data[key] = url

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Best Quality", callback_data=f"{MODE_HIGHEST}:{key}")],
            [InlineKeyboardButton("Lowest Quality", callback_data=f"{MODE_LOWEST}:{key}")],
            [InlineKeyboardButton("Audio Only", callback_data=f"{MODE_AUDIO}:{key}")],
            [InlineKeyboardButton("Video Only", callback_data=f"{MODE_VIDEO}:{key}")],
        ])

        await update.message.reply_text("Choose download format:", reply_markup=keyboard)


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
                await query.edit_message_text(
                    "Could not download this link. "
                    "The platform may not be supported or the content is private."
                )
            return

        # Fallback: send stream URL if no file was downloaded
        if result.stream_url and not result.filepath:
            await query.edit_message_text(
                f"File too large for Telegram. Direct link:\n{result.stream_url}"
            )
            return

        # Send the file
        try:
            if result.is_audio:
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

    except Exception as exc:
        print(f"Unhandled error for {url}: {exc}")
        try:
            await query.edit_message_text(
                "Could not download this link. "
                "The platform may not be supported or the content is private."
            )
        except Exception:
            pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("Bot started. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
