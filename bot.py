import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
MAX_BYTES = 45 * 1024 * 1024
MAX_MB = 45
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-saver-bot")


def valid_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        return bool(infos) and all(
            ipaddress.ip_address(info[4][0]).is_global for info in infos
        )
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك في Video Saver Bot 📥\n\n"
        "ابعت رابط فيديو عام من منصة مدعومة، وأنا هحاول أنزّله وأبعتهولك هنا.\n"
        "يدعم روابط عامة من منصات كثيرة حسب ما تسمح به المنصة وأداة التحميل.\n"
        "الحد الأقصى: 45MB للفيديو.\n\n"
        "مهم: لا يدعم الفيديوهات الخاصة أو التي تتطلب تسجيل دخول، ولا يتجاوز "
        "DRM أو أنظمة الحماية أو قيود الوصول. نزّل فقط المحتوى الذي تملكه "
        "أو لديك إذن بتنزيله."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أرسل رابط HTTPS لفيديو عام من منصة مدعومة.\n"
        "لن أستطيع ضمان كل الروابط؛ بعض المنصات تغيّر أنظمتها أو تمنع التنزيل.\n"
        "لا يدعم الحسابات الخاصة أو تسجيل الدخول أو DRM. الحد 45MB."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    candidates = [word.strip("()[]<>,") for word in text.split()]
    url = next((word for word in candidates if word.startswith("https://")), None)
    if not url:
        await update.message.reply_text("ابعت رابط HTTPS للفيديو.")
        return
    if not valid_public_url(url):
        await update.message.reply_text(
            "الرابط غير صالح أو غير متاح للعامة. ابعت رابط HTTPS عام."
        )
        return

    status = await update.message.reply_text("⏳ بفحص الرابط وبحاول أنزّل الفيديو...")
    try:
        with tempfile.TemporaryDirectory(prefix="video-saver-") as tmp:
            output = str(Path(tmp) / "%(id)s.%(ext)s")
            command = [
                "yt-dlp",
                "--no-playlist",
                "--no-progress",
                "--no-warnings",
                "--socket-timeout", "20",
                "--retries", "2",
                "--max-filesize", f"{MAX_MB}M",
                "--format", "best[ext=mp4]/best",
                "--output", output,
                url,
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                await status.edit_text("انتهت مهلة التحميل. جرّب الرابط مرة أخرى.")
                return

            files = [p for p in Path(tmp).glob("*") if p.is_file()]
            if process.returncode != 0 or not files:
                detail = (stderr or stdout).decode("utf-8", "ignore")[-700:]
                log.warning("yt-dlp failed: %s", detail)
                await status.edit_text(
                    "مقدرتش أنزّل الرابط. ممكن المنصة لا تدعمه أو الفيديو خاص/محمي "
                    "أو الرابط غير متاح. جرّب رابطًا عامًا آخر."
                )
                return

            video_path = max(files, key=lambda p: p.stat().st_size)
            size = video_path.stat().st_size
            if size > MAX_BYTES:
                await status.edit_text("الفيديو أكبر من الحد المسموح (45MB).")
                return
            if size == 0:
                await status.edit_text("الملف الناتج فارغ؛ جرّب رابطًا آخر.")
                return

            await status.edit_text("✅ تم التحميل، جاري إرساله على تيليجرام...")
            with video_path.open("rb") as video:
                await update.message.reply_document(
                    document=video,
                    filename=video_path.name,
                    caption="تم التحميل بواسطة Video Saver Bot",
                    read_timeout=120,
                    write_timeout=120,
                )
            await status.delete()
    except FileNotFoundError:
        await status.edit_text("أداة التحميل غير مثبتة على السيرفر. راجع إعدادات Railway.")
    except Exception:
        log.exception("Download failed")
        await status.edit_text("حصل خطأ أثناء التحميل أو الإرسال. جرّب رابطًا آخر.")


async def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable before starting the bot.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("Video Saver Telegram bot is running")
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
