import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_DURATION = int(os.getenv("MAX_DURATION", 15 * 60))  # seconds
MAX_FILE_MB = 49  # Telegram bot upload limit is 50 MB
# "mp3" = convert to 320 kbps MP3 (default). "original" = send YouTube's audio untouched (m4a/opus).
AUDIO_MODE = os.getenv("AUDIO_MODE", "mp3")
URL_RE = re.compile(r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+)")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ytmusic-bot")

semaphore = asyncio.Semaphore(2)

# Render's Python runtime has no ffmpeg, so use the pip-installed binary (falls back to system ffmpeg)
try:
    import imageio_ffmpeg

    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None


def download_audio(url: str, out_dir: str) -> tuple[Path, dict]:
    postprocessors = []
    if AUDIO_MODE == "mp3":
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
        )
    postprocessors.append({"key": "FFmpegMetadata"})

    opts = {
        "format": "bestaudio/best",  # highest audio bitrate YouTube offers
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_DURATION}"),
        "postprocessors": postprocessors,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise ValueError("Video was skipped (too long or unavailable).")

    files = [p for p in Path(out_dir).iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".ogg"}]
    if not files:
        raise ValueError("Download failed.")
    return files[0], info


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a YouTube / YouTube Music link and I'll send back the audio.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = URL_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Please send a valid YouTube link.")
        return

    status = await update.message.reply_text("Downloading...")
    async with semaphore:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path, info = await asyncio.to_thread(download_audio, match.group(1), tmp)
                size_mb = path.stat().st_size / 1024 / 1024
                if size_mb > MAX_FILE_MB:
                    await status.edit_text(f"File is {size_mb:.0f} MB, over Telegram's bot limit.")
                    return
                await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                with open(path, "rb") as f:
                    await update.message.reply_audio(
                        audio=f,
                        title=info.get("track") or info.get("title"),
                        performer=info.get("artist") or info.get("uploader"),
                        duration=info.get("duration"),
                    )
                await status.delete()
            except Exception as e:
                log.exception("download failed")
                await status.edit_text(f"Failed: {str(e)[:200]}")


def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    base_url = os.getenv("RENDER_EXTERNAL_URL")  # set automatically by Render
    if base_url:
        # Webhook mode (Render web service): Telegram wakes the service when a message arrives
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", 10000)),
            url_path=BOT_TOKEN,
            webhook_url=f"{base_url}/{BOT_TOKEN}",
            drop_pending_updates=False,
        )
    else:
        app.run_polling()  # local / Termux


if __name__ == "__main__":
    main()
