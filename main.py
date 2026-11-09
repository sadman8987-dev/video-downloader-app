import asyncio
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
import sentry_sdk
from redis.asyncio import Redis
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, FSInputFile,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
# Telegram cloud Bot API upload ceiling; keep a safety margin below 50 MB.
TELEGRAM_UPLOAD_LIMIT_MB = int(os.getenv("TELEGRAM_UPLOAD_LIMIT_MB", "49"))
TELEGRAM_UPLOAD_LIMIT_BYTES = TELEGRAM_UPLOAD_LIMIT_MB * 1024 * 1024
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "600"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
ENABLE_ARIA2 = os.getenv("ENABLE_ARIA2", "false").lower() in {"1", "true", "yes"}
BGUTIL_POT_BASE_URL = os.getenv("BGUTIL_POT_BASE_URL", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
redis_client: Redis | None = None
if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0, send_default_pii=False)
router = Router()
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
pending_links: dict[tuple[int, str], str] = {}
user_busy: set[int] = set()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("media-downloader")


def valid_public_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"}:
            return False
        # Reject literal private/reserved IPs and hostnames resolving to them.
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            records = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            return bool(records) and all(ipaddress.ip_address(r[4][0]).is_global for r in records)
    except (ValueError, OSError, socket.gaierror):
        return False


def _extractor_options(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    is_youtube = host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    options = {}
    if is_youtube:
        # The provider is optional: use it only when an internal endpoint was configured.
        if BGUTIL_POT_BASE_URL:
            options["extractor_args"] = {
                "youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_BASE_URL]}
            }
    return options


def _preflight(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        **_extractor_options(url),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_once(url: str, folder: str, mode: str, quality: str) -> tuple[str, dict]:
    outtmpl = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    if mode == "audio":
        fmt = "bestaudio/best"
    else:
        height = "" if quality == "best" else f"[height<={quality}]"
        fmt = (
            f"best{height}[ext=mp4]/best{height}/"
            f"best[ext=mp4]/best"
        )

    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "continuedl": True,
        "overwrites": True,
        "max_filesize": MAX_FILE_BYTES,
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "restrictfilenames": True,
        **_extractor_options(url),
    }
    if ENABLE_ARIA2 and shutil.which("aria2c"):
        opts["external_downloader"] = {"http": "aria2c", "https": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "8", "-s", "8", "-k", "1M", "--file-allocation=none"]
        }
    if mode == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    allowed = {".mp3"} if mode == "audio" else {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    candidates = []
    for item in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            if item.get(key):
                candidates.append(Path(item[key]))
    for key in ("filepath", "_filename"):
        if info.get(key):
            candidates.append(Path(info[key]))
    candidates.extend(p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in allowed)
    files, seen = [], set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved not in seen and resolved.is_file() and resolved.suffix.lower() in allowed:
                seen.add(resolved)
                files.append(resolved)
        except OSError:
            pass
    if not files:
        raise FileNotFoundError("لم يتم العثور على الملف بعد اكتمال التحميل.")
    final = max(files, key=lambda p: p.stat().st_mtime)
    if final.stat().st_size <= 0:
        raise ValueError("الملف الناتج فارغ.")
    if final.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_MB}MB).")
    return str(final), info


def download_media(url: str, folder: str, mode: str, quality: str) -> tuple[str, dict]:
    last_error = None
    for attempt in range(1, 4):
        try:
            info = _preflight(url)
            duration = info.get("duration")
            if duration is not None and duration > MAX_DURATION_SECONDS:
                raise ValueError(f"مدة الفيديو أطول من {MAX_DURATION_SECONDS // 60} دقيقة.")
            approx = info.get("filesize") or info.get("filesize_approx")
            if approx and approx > MAX_FILE_BYTES:
                raise ValueError(f"الحجم المتوقع أكبر من {MAX_FILE_MB}MB.")
            try:
                return _download_once(url, folder, mode, quality)
            except yt_dlp.utils.DownloadError:
                host = (urlparse(url).hostname or "").lower()
                is_instagram = host == "instagram.com" or host.endswith(".instagram.com")
                if is_instagram and mode == "video" and shutil.which("gallery-dl"):
                    command = [
                        "gallery-dl", "--quiet", "--no-mtime",
                        "--directory", folder,
                        "--filter", "extension in ('mp4', 'mkv', 'webm', 'mov', 'm4v')",
                        url,
                    ]
                    subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
                    found = [
                        p for p in Path(folder).rglob("*")
                        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
                    ]
                    if not found:
                        raise FileNotFoundError("gallery-dl لم ينتج ملف فيديو.")
                    final = max(found, key=lambda p: p.stat().st_mtime)
                    if final.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_MB}MB).")
                    return str(final), {"title": final.stem, "extractor": "gallery-dl"}
                raise
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc
            log.warning("Download attempt %s/3 failed (%s)", attempt, type(exc).__name__)
            if attempt < 3:
                time.sleep(attempt * 2)
    raise last_error


def build_quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="360p", callback_data=f"dl:q360:{token}"),
            InlineKeyboardButton(text="480p", callback_data=f"dl:q480:{token}"),
        ],
        [
            InlineKeyboardButton(text="720p HD", callback_data=f"dl:q720:{token}"),
            InlineKeyboardButton(text="1080p", callback_data=f"dl:q1080:{token}"),
        ],
        [InlineKeyboardButton(text="⭐ أعلى جودة", callback_data=f"dl:qbest:{token}")],
    ])


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "أهلاً بيك في Video Downloader Bot 🎬\n\n"
        "ابعت رابط فيديو عام، وبعدها اختار فيديو أو صوت MP3 والجودة.\n"
        f"حد التحميل الحالي: {MAX_FILE_MB}MB والمدة: {MAX_DURATION_SECONDS // 60} دقيقة.\n"
        "قد تختلف الإتاحة حسب المنصة. لا يدعم المحتوى الخاص أو المحمي بـ DRM."
    )


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "1) ابعت رابط HTTPS عام.\n2) اختار فيديو أو صوت MP3.\n"
        "3) لو اخترت فيديو، حدد الجودة.\n"
        "الحدود: 100MB/30 دقيقة افتراضياً. نزّل فقط المحتوى الذي تملك حق تنزيله."
    )


@router.message(F.text)
async def receive_link(message: Message):
    url = (message.text or "").strip()
    if not valid_public_url(url):
        await message.answer("ابعت رابط HTTP/HTTPS عام وصحيح من منصة مدعومة.")
        return
    token = str(int(time.time()))[-7:] + str(message.from_user.id)[-3:]
    pending_links[(message.from_user.id, token)] = url
    # Bound memory use for each user.
    keys = [k for k in pending_links if k[0] == message.from_user.id]
    for old in keys[:-5]:
        pending_links.pop(old, None)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 فيديو", callback_data=f"dl:video:{token}"),
        InlineKeyboardButton(text="🎵 صوت MP3", callback_data=f"dl:audio:{token}"),
    ]])
    await message.answer("اختار نوع التحميل:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("dl:"))
async def choose_download(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not callback.from_user or not callback.message:
        return
    _, choice, token = parts
    key = (callback.from_user.id, token)
    url = pending_links.get(key)
    if not url:
        await callback.message.edit_text("انتهى الاختيار. ابعت الرابط مرة تانية.")
        return
    if choice == "video":
        await callback.message.edit_text("اختار جودة الفيديو:", reply_markup=build_quality_keyboard(token))
        return
    if choice == "audio":
        mode, quality = "audio", "best"
    elif choice.startswith("q"):
        mode = "video"
        quality = {"q360": "360", "q480": "480", "q720": "720", "q1080": "1080", "qbest": "best"}.get(choice)
        if not quality:
            await callback.message.edit_text("اختيار الجودة غير صالح.")
            return
    else:
        return

    user_id = callback.from_user.id
    if user_id in user_busy:
        await callback.message.answer("عندك عملية تحميل شغالة بالفعل. استنى لما تخلص.")
        return
    lock_value = f"{user_id}:{token}:{time.time_ns()}"
    redis_locked = False
    redis_check_succeeded = False
    if redis_client is not None:
        try:
            redis_locked = bool(await redis_client.set(f"downloader:user:{user_id}:busy", lock_value, nx=True, ex=DOWNLOAD_TIMEOUT + 120))
            redis_check_succeeded = True
        except Exception as exc:
            log.warning("Redis lock unavailable (%s); using process-local lock", type(exc).__name__)
        if redis_check_succeeded and not redis_locked:
            await callback.message.answer("عندك عملية تحميل شغالة بالفعل. استنى لما تخلص.")
            return
    user_busy.add(user_id)
    pending_links.pop(key, None)
    status = await callback.message.edit_text("⏳ جاري تجهيز التحميل...")
    try:
        async with download_slots:
            with tempfile.TemporaryDirectory(prefix="telegram-media-") as folder:
                filename, info = await asyncio.wait_for(
                    asyncio.to_thread(download_media, url, folder, mode, quality),
                    timeout=DOWNLOAD_TIMEOUT,
                )
                size = Path(filename).stat().st_size
                title = str(info.get("title") or "ملف")[:64]
                if size > TELEGRAM_UPLOAD_LIMIT_BYTES:
                    await status.edit_text(
                        f"⚠️ تم تنزيل الملف بنجاح ({size / 1024 / 1024:.1f}MB)، "
                        f"لكن Telegram Bot API لا يسمح للبوت برفع ملفات بهذا الحجم عبر "
                        f"الخادم السحابي. الحد العملي الحالي {TELEGRAM_UPLOAD_LIMIT_MB}MB. "
                        "جرّب جودة أقل أو ملفًا أصغر."
                    )
                    return
                await status.edit_text("✅ التحميل اكتمل، جاري الإرسال...")
                media = FSInputFile(filename, filename=Path(filename).name)
                if mode == "audio":
                    await bot.send_audio(
                        chat_id=callback.message.chat.id, audio=media,
                        title=title, caption="تم التحميل بصيغة MP3",
                    )
                else:
                    await bot.send_video(
                        chat_id=callback.message.chat.id, video=media,
                        caption=f"{title} | {quality if quality != 'best' else 'أفضل جودة متاحة'}",
                        supports_streaming=True,
                    )
                await status.edit_text(f"✅ تم الإرسال بنجاح ({size / 1024 / 1024:.1f}MB).")
    except asyncio.TimeoutError:
        await status.edit_text("انتهت مهلة التحميل. جرّب رابطاً آخر أو جودة أقل.")
    except yt_dlp.utils.DownloadError as exc:
        sentry_sdk.capture_exception(exc)
        detail = str(exc).lower()
        log.warning("Extractor/download failure: %s", str(exc)[:700])
        if "sign in to confirm" in detail or "not a bot" in detail:
            msg = "يوتيوب طلب تحقق/تسجيل دخول من السيرفر؛ جرّب رابطاً عاماً آخر أو منصة أخرى."
        else:
            msg = "المنصة رفضت التحميل أو الرابط/الجودة غير متاحين حالياً. جرّب جودة أقل."
        await status.edit_text("❌ " + msg)
    except ValueError as exc:
        await status.edit_text("❌ " + str(exc))
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        log.exception("Unexpected download/send failure")
        await status.edit_text("❌ حصل خطأ أثناء التحميل أو الإرسال. جرّب مرة أخرى.")
    finally:
        user_busy.discard(user_id)
        if redis_locked and redis_client is not None:
            try:
                await redis_client.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, f"downloader:user:{user_id}:busy", lock_value)
            except Exception as exc:
                log.warning("Could not release Redis lock (%s)", type(exc).__name__)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing from Railway Variables.")
    global redis_client
    if REDIS_URL:
        try:
            redis_client = Redis.from_url(REDIS_URL, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
            await redis_client.ping()
            log.info("Redis connected; distributed per-user locks enabled")
        except Exception as exc:
            log.warning("Redis unavailable (%s); falling back to local locks", type(exc).__name__)
            if redis_client is not None:
                await redis_client.aclose()
            redis_client = None
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot starting; yt-dlp=%s aria2=%s max_file=%sMB",
             yt_dlp.version.__version__, bool(shutil.which("aria2c")), MAX_FILE_MB)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if redis_client is not None:
            await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
