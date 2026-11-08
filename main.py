import asyncio
import base64
import binascii
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_DURATION_SECONDS = 20 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("video-downloader")


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def download_video(url: str, folder: str) -> tuple[str, dict]:
    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    is_instagram = "instagram.com" in (urlparse(url).hostname or "").lower()
    options = {
        "http_headers": (
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
            }
            if is_instagram else {}
        ),
        "format": (
            "best[height<=720][ext=mp4][filesize<=49M]/"
            "best[height<=720][filesize<=49M]/"
            "best[filesize<=49M]/"
            "best[height<=720]/best"
        ),
        "format_sort": ["res:720", "ext:mp4:m4a"],
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration <= {MAX_DURATION_SECONDS}"
        ),
        "merge_output_format": "mp4",
        "js_runtimes": {"deno": {}},
        "remote_components": ["ejs:github"],
        "quiet": True,
        "no_warnings": False,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
    }

    # Optional Instagram session cookies. Store the base64-encoded Netscape
    # cookies.txt content in Railway as INSTAGRAM_COOKIES_BASE64. Never log it.
    if is_instagram:
        encoded_cookies = os.getenv("INSTAGRAM_COOKIES_BASE64", "").strip()
        if encoded_cookies:
            cookie_path = Path(folder) / "instagram-cookies.txt"
            try:
                cookie_bytes = base64.b64decode(encoded_cookies, validate=True)
                if not cookie_bytes.startswith(b"# Netscape HTTP Cookie File") and not cookie_bytes.startswith(b"# HTTP Cookie File"):
                    raise ValueError("Instagram cookies must use Netscape cookies.txt format")
                cookie_path.write_bytes(cookie_bytes)
                options["cookiefile"] = str(cookie_path)
            except (binascii.Error, ValueError, OSError):
                logger.exception("Invalid INSTAGRAM_COOKIES_BASE64; continuing without Instagram cookies")

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    # prepare_filename() may return the pre-merge extension (for example
    # .webm) while FFmpeg writes the final file as .mp4. Prefer yt-dlp's
    # actual requested_downloads filepath, then search the temp directory.
    candidates = []
    for entry in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            value = entry.get(key)
            if value:
                candidates.append(Path(value))

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value:
            candidates.append(Path(value))

    candidates.extend(
        p for p in Path(folder).rglob("*")
        if p.is_file() and p.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".mov", ".m4v"
        }
    )

    existing = []
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved not in seen and resolved.is_file():
                seen.add(resolved)
                existing.append(resolved)
        except OSError:
            continue

    if not existing:
        files_found = [str(p.name) for p in Path(folder).rglob("*") if p.is_file()]
        logger.error(
            "yt-dlp returned without a discoverable media file. extractor=%s "
            "id=%s requested_downloads=%r files_in_temp=%r",
            info.get("extractor"), info.get("id"),
            info.get("requested_downloads"), files_found,
        )
        raise FileNotFoundError(
            "yt-dlp did not produce a media file; see extractor details in Railway logs"
        )

    final_path = max(existing, key=lambda p: p.stat().st_mtime)
    if final_path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("حجم الفيديو أكبر من 49 ميجابايت. جرّب فيديو أقصر أو أقل جودة.")

    return str(final_path), info


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً بيك في Video Downloader Bot 🎬\n\n"
        "ابعت رابط فيديو عام من منصة يدعمها yt-dlp، مثل YouTube وShorts "
        "وTikTok وInstagram وFacebook وX وReddit وVimeo.\n\n"
        "الحد الأقصى 49MB والمدة 20 دقيقة. قد تختلف الإتاحة حسب المنصة. "
        "لا يدعم المحتوى الخاص أو المحمي بـ DRM أو الذي يتطلب تسجيل دخول. "
        "نزّل فقط المحتوى الذي تملك حق تنزيله."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "طريقة الاستخدام: ابعت رابط الفيديو كاملًا ويبدأ بـ http:// أو https://.\n"
        "الحد الأقصى 49MB و20 دقيقة. استخدم روابط عامة فقط."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    url = message.text.strip()
    if not valid_url(url):
        await message.reply_text("ابعت رابط صحيح يبدأ بـ http:// أو https://")
        return

    status = await message.reply_text("⏳ بفحص الرابط وبحمّل الفيديو...")
    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action=ChatAction.TYPING,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-video-") as folder:
            filename, info = await asyncio.to_thread(download_video, url, folder)
            title = (info.get("title") or "الفيديو")[:900]

            await status.edit_text("✅ التحميل اكتمل، جاري إرسال الفيديو...")
            with open(filename, "rb") as media:
                await message.reply_video(
                    video=media,
                    caption=title,
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
            await status.delete()

    except yt_dlp.utils.DownloadError as exc:
        # Keep the detailed reason in Railway logs; never expose cookies/tokens.
        logger.exception("yt-dlp download failed: %s", exc)
        await status.edit_text(
            "❌ المنصة رفضت التحميل أو تعذّر استخراج الفيديو. "
            "قد يكون الرابط خاصًا أو يحتاج تسجيل دخول أو أن المنصة غيّرت طريقة الحماية. "
            "جرّب رابطًا عامًا آخر."
        )
    except ValueError as exc:
        await status.edit_text(f"❌ {exc}")
    except Exception:
        logger.exception("Unexpected error while downloading/sending media")
        await status.edit_text(
            "❌ حصل خطأ أثناء التحميل أو الإرسال. راجع سجلات Railway أو جرّب رابطًا آخر."
        )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Video downloader bot is starting; yt-dlp version: %s", yt_dlp.version.__version__)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
