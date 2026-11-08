import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE = 49 * 1024 * 1024  # Keep below Telegram Bot API upload limit.
MAX_DURATION = 20 * 60  # 20 minutes.


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً بيك! 👋\n"
        "ابعت رابط فيديو عام تملك حق تنزيله، وهحاول أحمّله وأبعتهولك هنا.\n\n"
        "مهم: لا يدعم المحتوى المحمي بـ DRM أو المحتوى الخاص أو المدفوع."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "طريقة الاستخدام:\n"
        "1) ابعت رابط الفيديو.\n"
        "2) انتظر لحد ما التحميل يكتمل.\n"
        "3) هيوصلك الفيديو هنا إذا كان حجمه أقل من 49 ميجابايت ومدته لا تتجاوز 20 دقيقة.\n\n"
        "استخدمه فقط مع فيديوهات مسموح لك بتنزيلها."
    )


def download_video(url: str, folder: str) -> tuple[str, dict]:
    output_template = str(Path(folder) / "%(title).80s.%(ext)s")
    options = {
        "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "match_filter": yt_dlp.utils.match_filter_func("duration <= 1200"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 25,
        "retries": 2,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            # yt-dlp may change the extension after post-processing.
            base = os.path.splitext(filename)[0]
            candidates = list(Path(folder).glob(Path(base).name + ".*"))
            if not candidates:
                raise FileNotFoundError("Downloaded file was not found")
            filename = str(candidates[0])
        return filename, info


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    url = (message.text or "").strip()

    if not valid_url(url):
        await message.reply_text("ابعت رابط صحيح يبدأ بـ http:// أو https://")
        return

    status = await message.reply_text("⏳ جاري فحص الرابط وتحميل الفيديو...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-video-") as folder:
            filename, info = await asyncio.to_thread(download_video, url, folder)
            size = os.path.getsize(filename)
            if size > MAX_FILE_SIZE:
                await status.edit_text("حجم الفيديو أكبر من الحد المسموح (49 ميجابايت).")
                return

            title = (info.get("title") or "الفيديو")[:900]
            await status.edit_text("✅ التحميل اكتمل، جاري إرسال الفيديو...")
            with open(filename, "rb") as video:
                await message.reply_video(
                    video=video,
                    caption=title,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            await status.delete()
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("Download failed: %s", exc)
        await status.edit_text(
            "تعذر تنزيل الفيديو. تأكد أن الرابط عام ومتاح، وأن تنزيله مسموح، "
            "وجرب رابطًا آخر."
        )
    except Exception:
        logger.exception("Unexpected download error")
        await status.edit_text("حصل خطأ أثناء التحميل أو الإرسال. جرّب رابطًا آخر.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    logger.info("Telegram video downloader bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
