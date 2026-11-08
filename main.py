import asyncio
import base64
import binascii
import logging
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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


def download_media(url: str, folder: str, audio_only: bool = False, video_quality: str = "720") -> tuple[str, dict]:
    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    is_instagram = "instagram.com" in (urlparse(url).hostname or "").lower()
    instagram_cookie_path = None
    quality_filter = "" if video_quality == "best" else f"[height<={video_quality}]"
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
            "bestaudio[filesize<=49M]/bestaudio/best"
            if audio_only else
            f"best{quality_filter}[ext=mp4][filesize<=49M]/"
            f"best{quality_filter}[filesize<=49M]/"
            f"best{quality_filter}/best"
        ),
        "format_sort": ["abr", "ext:m4a"] if audio_only else (["ext:mp4:m4a"] if video_quality == "best" else [f"res:{video_quality}", "ext:mp4:m4a"]),
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

    if audio_only:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    # Optional Instagram session cookies. Keep the value in Railway Variables only.
    if is_instagram:
        encoded_cookies = os.getenv("INSTAGRAM_COOKIES_BASE64", "").strip()
        if encoded_cookies:
            cookie_path = Path(folder) / "instagram-cookies.txt"
            try:
                cookie_bytes = base64.b64decode(encoded_cookies, validate=True)
                if not cookie_bytes.startswith(b"# Netscape HTTP Cookie File") and not cookie_bytes.startswith(b"# HTTP Cookie File"):
                    raise ValueError("Instagram cookies must use Netscape cookies.txt format")
                cookie_path.write_bytes(cookie_bytes)
                instagram_cookie_path = cookie_path
                options["cookiefile"] = str(cookie_path)
            except (binascii.Error, ValueError, OSError):
                logger.exception("Invalid INSTAGRAM_COOKIES_BASE64; continuing without Instagram cookies")

    # Optional YouTube session cookies. Store the Base64 value in Railway Variables only.
    hostname = (urlparse(url).hostname or "").lower()
    is_youtube = (
        hostname == "youtu.be"
        or hostname == "youtube-nocookie.com"
        or hostname.endswith(".youtube-nocookie.com")
        or hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
    )
    if is_youtube:
        options["extractor_args"] = {
            "youtubepot-bgutilhttp": {
                "base_url": [os.getenv("BGUTIL_POT_BASE_URL", "http://bgutil-pot-provider.railway.internal:4416")]
            }
        }
        encoded_cookies = os.getenv("YOUTUBE_COOKIES_BASE64", "").strip()
        if encoded_cookies:
            cookie_path = Path(folder) / "youtube-cookies.txt"
            try:
                cookie_bytes = base64.b64decode(encoded_cookies, validate=True)
                if not (
                    cookie_bytes.startswith(b"# Netscape HTTP Cookie File")
                    or cookie_bytes.startswith(b"# HTTP Cookie File")
                ):
                    raise ValueError("YouTube cookies must use Netscape cookies.txt format")
                cookie_path.write_bytes(cookie_bytes)
                options["cookiefile"] = str(cookie_path)
            except (binascii.Error, ValueError, OSError):
                logger.exception(
                    "Invalid YOUTUBE_COOKIES_BASE64; continuing without YouTube cookies"
                )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError:
        # Instagram-only fallback: gallery-dl can use the same Netscape cookies file.
        # It is limited to video files and the configured Telegram size ceiling.
        if not is_instagram or audio_only:
            raise
        command = [
            "gallery-dl", "--quiet", "--no-input",
            "--directory", folder,
            "--filesize-max", "49M",
            "--filter", "extension in ('mp4', 'mkv', 'webm', 'mov', 'm4v')",
        ]
        if instagram_cookie_path and instagram_cookie_path.is_file():
            command.extend(["--cookies", str(instagram_cookie_path)])
        command.append(url)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=240)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as fallback_error:
            logger.warning("gallery-dl Instagram fallback failed: %s", str(fallback_error)[:500])
            raise
        fallback_files = [
            p for p in Path(folder).rglob("*")
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        ]
        if not fallback_files:
            raise FileNotFoundError("gallery-dl did not produce an Instagram video file")
        final_fallback = max(fallback_files, key=lambda p: p.stat().st_mtime)
        if final_fallback.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("حجم الملف أكبر من 49 ميجابايت.")
        return str(final_fallback), {"title": final_fallback.stem, "extractor": "gallery-dl"}

    allowed_extensions = (
        {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav"}
        if audio_only else
        {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    )
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
        if p.is_file() and p.suffix.lower() in allowed_extensions
    )

    existing = []
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved not in seen and resolved.is_file() and resolved.suffix.lower() in allowed_extensions:
                seen.add(resolved)
                existing.append(resolved)
        except OSError:
            continue

    if not existing:
        files_found = [str(p.name) for p in Path(folder).rglob("*") if p.is_file()]
        logger.error(
            "yt-dlp returned without a discoverable %s file. extractor=%s id=%s files=%r",
            "audio" if audio_only else "video", info.get("extractor"), info.get("id"), files_found,
        )
        raise FileNotFoundError("yt-dlp did not produce the requested media file")

    final_path = max(existing, key=lambda p: p.stat().st_mtime)
    if final_path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("حجم الملف أكبر من 49 ميجابايت.")
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

    token = secrets.token_urlsafe(6)
    pending = context.user_data.setdefault("pending_downloads", {})
    pending[token] = url
    # Keep only the newest few pending links per user.
    while len(pending) > 10:
        pending.pop(next(iter(pending)))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 تحميل فيديو", callback_data=f"download:video:{token}"),
        InlineKeyboardButton("🎵 تحميل صوت MP3", callback_data=f"download:audio:{token}"),
    ]])
    await message.reply_text("اختار عايز تحمل إيه:", reply_markup=keyboard)


async def handle_download_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        parts = query.data.split(":")
        action = parts[0]
        choice = parts[1]
        token = parts[-1]
    except (AttributeError, IndexError):
        await query.edit_message_text("❌ الاختيار غير صالح. ابعت الرابط مرة تانية.")
        return

    pending = context.user_data.get("pending_downloads", {})
    url = pending.get(token)
    if action != "download" or not url:
        await query.edit_message_text("انتهت صلاحية الاختيار. ابعت الرابط مرة تانية.")
        return

    # Video selection opens a quality menu; download starts after a quality is chosen.
    if choice == "video":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data=f"download:q360:{token}"),
                InlineKeyboardButton("480p", callback_data=f"download:q480:{token}"),
            ],
            [
                InlineKeyboardButton("720p HD", callback_data=f"download:q720:{token}"),
                InlineKeyboardButton("1080p Full HD", callback_data=f"download:q1080:{token}"),
            ],
            [InlineKeyboardButton("⭐ أعلى جودة متاحة", callback_data=f"download:qbest:{token}")],
        ])
        await query.edit_message_text("اختار جودة الفيديو المطلوبة:", reply_markup=keyboard)
        return

    if choice == "audio":
        audio_only = True
        video_quality = "720"
    elif choice.startswith("q"):
        audio_only = False
        video_quality = {"q360": "360", "q480": "480", "q720": "720", "q1080": "1080", "qbest": "best"}.get(choice)
        if not video_quality:
            await query.edit_message_text("❌ الجودة المختارة غير صالحة.")
            return
    else:
        await query.edit_message_text("❌ الاختيار غير صالح.")
        return

    pending.pop(token, None)
    await query.edit_message_text(
        "⏳ جاري تحميل الصوت وتحويله إلى MP3..." if audio_only
        else f"⏳ جاري تحميل الفيديو بجودة {video_quality if video_quality != 'best' else 'أعلى جودة متاحة'}..."
    )
    await context.bot.send_chat_action(
        chat_id=query.message.chat_id,
        action=ChatAction.TYPING,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-media-") as folder:
            filename, info = await asyncio.to_thread(
                download_media, url, folder, audio_only, video_quality
            )
            title = (info.get("title") or "الملف")[:900]
            with open(filename, "rb") as media:
                if audio_only:
                    await query.message.reply_audio(
                        audio=media,
                        title=title[:64],
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                    )
                else:
                    await query.message.reply_video(
                        video=media,
                        caption=f"{title} | الجودة المطلوبة: {video_quality if video_quality != 'best' else 'أعلى جودة متاحة'}",
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                    )
            await query.edit_message_text("✅ تم التحميل والإرسال بنجاح.")
    except yt_dlp.utils.DownloadError as exc:
        error_text = str(exc)
        logger.error("yt-dlp %s download failed: %s", "audio" if audio_only else "video", error_text, exc_info=True)

        if "Sign in to confirm you're not a bot" in error_text or (
            "youtube" in error_text.lower() and "cookies" in error_text.lower()
        ):
            user_message = (
                "⚠️ يوتيوب رفض إتاحة الفيديو للسيرفر وطلب تسجيل دخول/تحقق.\n"
                "البوت شغال، لكن الفيديو ده غير متاح للتنزيل حاليًا من بيئة الاستضافة.\n"
                "جرّب رابط يوتيوب عام آخر، أو جرّب منصة مدعومة أخرى."
            )
        else:
            user_message = (
                "❌ المنصة رفضت التحميل أو الجودة المطلوبة غير متاحة. "
                "جرّب جودة أقل أو رابطًا عامًا آخر."
            )
        await query.edit_message_text(user_message)
    except ValueError as exc:
        await query.edit_message_text(f"❌ {exc}")
    except Exception:
        logger.exception("Unexpected error while downloading/sending media")
        await query.edit_message_text(
            "❌ حصل خطأ أثناء التحميل أو الإرسال. جرّب رابطًا آخر."
        )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_download_choice, pattern=r"^download:(video|audio|q360|q480|q720|q1080|qbest):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Video downloader bot is starting; yt-dlp version: %s", yt_dlp.version.__version__)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
