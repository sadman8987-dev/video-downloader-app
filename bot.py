import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
MAX_BYTES = 45 * 1024 * 1024
SAVEFROM_URL = "https://en1.savefrom.net/19wr/"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-saver-bot")

def valid_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except Exception:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("فتح SaveFrom", url=SAVEFROM_URL)]]
    await update.message.reply_text(
        "أهلاً بك في Video Saver Bot 📥\n\n"
        "أرسل رابط تحميل مباشر لملف فيديو بصيغة MP4 أو WebM، "
        "وسأحاول إرساله لك هنا (بحد أقصى 45 ميجابايت).\n\n"
        "لو الرابط من منصة فيديو وليس رابط ملف مباشر، افتح SaveFrom من الزر "
        "واستخدمه فقط للمحتوى الذي تملك حق تنزيله.\n\n"
        "لا يدعم البوت تجاوز تسجيل الدخول أو DRM أو المحتوى الخاص أو قيود المنصات.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "طريقة الاستخدام:\n"
        "1) أرسل رابط HTTPS مباشر لملف فيديو عام.\n"
        "2) يجب أن يكون حجم الملف 45MB أو أقل.\n"
        "3) أرسل /start لعرض زر SaveFrom.\n\n"
        "لا ترسل روابط خاصة أو محمية أو محتوى لا تملك إذن تنزيله."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    candidates = [word.strip("()[]<>,") for word in text.split()]
    url = next((word for word in candidates if word.startswith("https://")), None)
    if not url:
        await update.message.reply_text("ابعت رابط HTTPS مباشر للفيديو.")
        return
    if not valid_public_url(url):
        await update.message.reply_text("الرابط غير صالح أو غير عام. أرسل رابط HTTPS عام لملف تملكه أو لديك إذن بتنزيله.")
        return
    status = await update.message.reply_text("⏳ بفحص الرابط وحجم الفيديو...")
    timeout = aiohttp.ClientTimeout(total=120, connect=15)
    headers = {"User-Agent": "VideoSaverBot/1.0"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status not in (200, 206):
                    await status.edit_text("تعذر فتح الرابط. تأكد أنه رابط ملف فيديو مباشر ومتاح للعامة.")
                    return
                content_type = response.headers.get("Content-Type", "").lower()
                length = int(response.headers.get("Content-Length", "0") or 0)
                if not (content_type.startswith("video/") or urlparse(url).path.lower().endswith((".mp4", ".webm", ".mov", ".m4v"))):
                    await status.edit_text("ده مش باين إنه رابط ملف فيديو مباشر. استخدم رابط MP4/WebM مباشر.")
                    return
                if length > MAX_BYTES:
                    await status.edit_text("حجم الفيديو أكبر من 45MB، وده الحد المتاح في النسخة الحالية.")
                    return
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        await status.edit_text("حجم الفيديو أكبر من 45MB.")
                        return
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in (".mp4", ".webm", ".mov", ".m4v"):
            suffix = ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix) as file:
            file.write(data)
            file.flush()
            await status.edit_text("✅ تم التحميل، جاري الإرسال...")
            with open(file.name, "rb") as video:
                await update.message.reply_video(video=video, caption="تم الإرسال بواسطة Video Saver Bot")
        await status.delete()
    except Exception as exc:
        log.exception("Download failed: %s", exc)
        await status.edit_text("حصلت مشكلة أثناء تنزيل الملف. جرّب رابط فيديو مباشر آخر.")

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
