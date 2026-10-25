"""Обработчики Telegram-бота: скачивание медиа по ссылкам."""

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import uuid
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Ключи для application.bot_data (их ожидает main.py)
DB_KEY = "db"
REDIS_KEY = "redis"

# ================== НАСТРОЙКИ ==================
# Необязательно: путь к cookies.txt (помогает с Instagram и приватным контентом)
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_TEXT = os.getenv("COOKIES_TEXT", "").strip()
if COOKIES_TEXT and not COOKIES_FILE:
    COOKIES_FILE = str(Path(tempfile.gettempdir()) / "cookies.txt")
    Path(COOKIES_FILE).write_text(COOKIES_TEXT + "\n", encoding="utf-8")
    
MAX_FILE_SIZE = 49 * 1024 * 1024  # лимит Telegram для ботов ~50 МБ
MAX_ITEMS = 10                    # максимум файлов за одну ссылку
MAX_PARALLEL = 3                  # сколько скачиваний одновременно

# Стикер, который показывается, пока бот скачивает
STICKER_LOADING = "CAACAgIAAxkBAAP5at4iU67sExMlWjX2xVoefOOdxBQAAhABAAJSiZEjxEjx-swcBvM9BA"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
URL_RE = re.compile(r"https?://\S+")

# Сайты, которые бот не обрабатывает
BLOCKED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# Таймауты для загрузки больших файлов в Telegram
UPLOAD_TIMEOUTS = {"read_timeout": 60, "write_timeout": 120, "connect_timeout": 30}

# ffmpeg нужен, чтобы склеивать видео и звук; без него берём готовый файл
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = shutil.which("ffmpeg")
HAS_FFMPEG = bool(FFMPEG_PATH)
# ==============================================

logger = logging.getLogger(__name__)

# httpx пишет в лог полный URL запроса, а в нём токен бота — глушим эти логи
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

download_slots = asyncio.Semaphore(MAX_PARALLEL)


def is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто пришли мне ссылку на пост — я скачаю медиа и отправлю тебе.👀\n"
        "А если нужен только звук, нажми кнопку «Скачать аудио» после видео.\n\n"
        "Используй /help, чтобы посмотреть все возможности."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Доступные команды:</b>\n\n"
        "/start — перезапустить бота\n"
        "/help — показать это сообщение\n"
        "/audio ссылка — скачать только звук (MP3)\n\n"
        "<b>Как пользоваться:</b>\n"
        "Просто отправь ссылку на пост из:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• Twitter / X\n"
        "• и некоторых других сайтов\n\n"
        "Бот скачает фото или видео и пришлёт их тебе.\n"
        "Карусели тоже поддерживаются (до 10 файлов).\n\n"
        "После видео появится кнопка «Скачать аудио» — "
        "бот пришлёт музыку из ролика в формате MP3."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


def create_caption(url: str, title: str | None = None) -> str:
    """Подпись к медиа. Всё экранируется, чтобы HTML не ломался."""
    link = f'<a href="{html.escape(url, quote=True)}">🔗 Оригинальный пост</a>'
    title = (title or "").strip()
    if title and title.lower() not in {"na", "none"}:
        if len(title) > 120:
            title = title[:117] + "..."
        return f"<b>{html.escape(title)}</b>\n\n{link}"
    return link


def build_ydl_opts(temp_dir: Path) -> dict:
    opts = {
        # autonumber сохраняет порядок карусели
        "outtmpl": str(temp_dir / "%(autonumber)03d_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "playlistend": MAX_ITEMS,  # защита от скачивания огромных плейлистов
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "ignoreerrors": True,
        "max_filesize": MAX_FILE_SIZE,
    }
    if HAS_FFMPEG:
        opts["ffmpeg_location"] = FFMPEG_PATH
        opts["merge_output_format"] = "mp4"
        # Предпочитаем форматы, которые влезают в лимит Telegram
        opts["format"] = (
            f"bv*[filesize<?{MAX_FILE_SIZE}]+ba/"
            f"b[filesize<?{MAX_FILE_SIZE}]/b"
        )
    else:
        # Без ffmpeg склеивать нельзя — берём готовый одиночный файл
        opts["format"] = (
            f"b[ext=mp4][filesize<?{MAX_FILE_SIZE}]/"
            f"b[filesize<?{MAX_FILE_SIZE}]/b"
        )
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def build_audio_opts(temp_dir: Path) -> dict:
    opts = {
        "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def run_download(url: str, temp_dir: Path):
    with yt_dlp.YoutubeDL(build_ydl_opts(temp_dir)) as ydl:
        return ydl.extract_info(url, download=True)


def run_audio_download(url: str, temp_dir: Path):
    with yt_dlp.YoutubeDL(build_audio_opts(temp_dir)) as ydl:
        return ydl.extract_info(url, download=True)


def run_gallery_dl(url: str, temp_dir: Path):
    """Фото и карусели Instagram через gallery-dl."""
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "-D", str(temp_dir),
        "--no-mtime",
        "-o", "filename={num:>03}_{filename}.{extension}",
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gallery-dl: {result.stderr[-300:]}")


def extract_title(info) -> str | None:
    if not isinstance(info, dict):
        return None
    title = info.get("title") or info.get("description")
    if not title and info.get("entries"):
        first = next((e for e in info["entries"] if e), None)
        if first:
            title = first.get("title") or first.get("description")
    return title


def friendly_error(error: Exception) -> str:
    error_text = str(error).lower()
    if "private" in error_text or "login" in error_text or "403" in error_text:
        return (
            "Контент приватный или требует авторизации.\n"
            "Instagram часто блокирует скачивание без входа в аккаунт."
        )
    if "unsupported" in error_text or "no video" in error_text:
        return "Этот тип ссылки пока не поддерживается или контент недоступен."
    if "instagram" in error_text:
        return (
            "Instagram временно заблокировал скачивание.\n"
            "Попробуй позже или другую ссылку."
        )
    return "Не удалось скачать это медиа 😔\nПроверь ссылку или попробуй другую."


async def send_files(message: Message, files: list[Path], caption: str) -> int:
    """Фото и видео отправляем альбомами по 10, остальное — документами."""
    media, docs = [], []
    for f in files:
        ext = f.suffix.lower()
        if ext in PHOTO_EXT:
            media.append(("photo", f))
        elif ext in VIDEO_EXT:
            media.append(("video", f))
        else:
            docs.append(f)

    sent = 0
    caption_used = False

    for i in range(0, len(media), 10):
        chunk = media[i : i + 10]
        cap = None if caption_used else caption
        caption_used = True

        if len(chunk) == 1:
            kind, f = chunk[0]
            if kind == "photo":
                await message.reply_photo(
                    f, caption=cap, parse_mode=ParseMode.HTML, **UPLOAD_TIMEOUTS
                )
            else:
                await message.reply_video(
                    f,
                    caption=cap,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **UPLOAD_TIMEOUTS,
                )
        else:
            group = []
            for n, (kind, f) in enumerate(chunk):
                cls = InputMediaPhoto if kind == "photo" else InputMediaVideo
                group.append(
                    cls(
                        f.read_bytes(),
                        filename=f.name,
                        caption=cap if n == 0 else None,
                        parse_mode=ParseMode.HTML,
                    )
                )
            await message.reply_media_group(group, **UPLOAD_TIMEOUTS)
        sent += len(chunk)

    for f in docs:
        cap = None if caption_used else caption
        caption_used = True
        await message.reply_document(
            f, caption=cap, parse_mode=ParseMode.HTML, **UPLOAD_TIMEOUTS
        )
        sent += 1

    return sent


# ---------- Аудио ----------

def remember_url(context: ContextTypes.DEFAULT_TYPE, key: str, url: str) -> None:
    """Запоминаем ссылку для кнопки (callback_data ограничен 64 байтами)."""
    store = context.bot_data.setdefault("audio_urls", {})
    store[key] = url
    while len(store) > 200:
        store.pop(next(iter(store)))


async def send_audio(message: Message, url: str) -> None:
    """Скачивает звук по ссылке и отправляет MP3."""
    if is_blocked(url):
        await message.reply_text("YouTube не поддерживается.")
        return
    if not HAS_FFMPEG:
        await message.reply_text("Для скачивания аудио на сервере нужен ffmpeg.")
        return

    status = await message.reply_text("Достаю звук... 🎧")
    temp_dir = DOWNLOAD_DIR / f"audio_{message.chat_id}_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with download_slots:
            info = await asyncio.to_thread(run_audio_download, url, temp_dir)

        files = sorted(temp_dir.glob("*.mp3"))
        if not files:
            raise RuntimeError("Не удалось получить аудио")
        audio = files[0]
        if audio.stat().st_size > MAX_FILE_SIZE:
            await status.edit_text("Аудио слишком большое для отправки (лимит 49 МБ).")
            return

        title = extract_title(info)
        performer = None
        if isinstance(info, dict):
            performer = info.get("artist") or info.get("uploader")
        await status.edit_text("Отправляю аудио...")
        await message.reply_audio(
            audio,
            title=(title or "Аудио")[:64],
            performer=(performer or "")[:64] or None,
            caption=create_caption(url),
            parse_mode=ParseMode.HTML,
            **UPLOAD_TIMEOUTS,
        )
        await status.delete()

    except Exception as e:
        logger.exception("Ошибка при скачивании аудио %s", url)
        try:
            await status.edit_text(friendly_error(e))
        except Exception:
            await message.reply_text(friendly_error(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "Напиши команду со ссылкой, например:\n/audio https://..."
        )
        return
    await send_audio(update.message, match.group(0))


async def audio_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    url = context.bot_data.get("audio_urls", {}).get(query.data)
    if not url:
        await query.answer("Ссылка устарела, пришли её заново.", show_alert=True)
        return
    await query.answer()
    await send_audio(query.message, url)


# ---------- Скачивание медиа ----------

async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = URL_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Пришли, пожалуйста, ссылку на пост.")
        return
    url = match.group(0)

    if is_blocked(url):
        await update.message.reply_text(
            "YouTube не поддерживается. Пришли ссылку из Instagram или TikTok."
        )
        return

    status_msg = await update.message.reply_text("Скачиваю... ⏳")
    temp_dir = DOWNLOAD_DIR / f"{update.effective_user.id}_{update.message.message_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    sticker_msg = None
    try:
        sticker_msg = await update.message.reply_sticker(STICKER_LOADING)
    except Exception:
        logger.exception("Не удалось отправить стикер")
    try:
        async with download_slots:
            info = None
            if "instagram.com/p/" in url:
                try:
                    await asyncio.to_thread(run_gallery_dl, url, temp_dir)
                except Exception:
                    logger.exception("gallery-dl не справился, пробую yt-dlp")
            if not any(temp_dir.glob("*")):
                info = await asyncio.to_thread(run_download, url, temp_dir)

        title = extract_title(info)
        all_files = sorted(
            f for f in temp_dir.glob("*") if f.is_file() and f.stat().st_size > 0
        )
        if not all_files:
            raise RuntimeError("Не удалось скачать ни одного файла")

        files = [f for f in all_files if f.stat().st_size <= MAX_FILE_SIZE]
        skipped = len(all_files) - len(files)
        if skipped:
            await update.message.reply_text(
                f"Файлов больше 49 МБ пропущено: {skipped} (лимит Telegram)."
            )
        if not files:
            await status_msg.edit_text("Все файлы слишком большие для отправки.")
            return

        await status_msg.edit_text(f"Скачано файлов: {len(files)}. Отправляю...")
        sent = await send_files(update.message, files, create_caption(url, title))

        if not sent:
            await status_msg.edit_text("Не удалось отправить ни одного файла.")
        elif HAS_FFMPEG and any(f.suffix.lower() in VIDEO_EXT for f in files):
            # Вместо удаления превращаем статус в кнопку «Скачать аудио»
            key = f"a:{status_msg.chat_id}:{status_msg.message_id}"
            remember_url(context, key, url)
            await status_msg.edit_text(
                "🎵 Нужен только звук?",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎵 Скачать аудио", callback_data=key)]]
                ),
            )
        else:
            await status_msg.delete()

    except Exception as e:
        logger.exception("Ошибка при обработке %s", url)
        msg = friendly_error(e)
        try:
            await status_msg.edit_text(msg)
        except Exception:
            await update.message.reply_text(msg)

        finally:
            if sticker_msg:
                try:
                    await sticker_msg.delete()
            except Exception:
                pass
            shutil.rmtree(temp_dir, ignore_errors=True)


async def sticker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = update.message.sticker
    await update.message.reply_text(
        f"file_id стикера:\n<code>{s.file_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Ловит все необработанные ошибки: пишет в лог и отвечает по-русски."""
    logger.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Что-то пошло не так 😔 Попробуй ещё раз чуть позже."
            )
        except Exception:
            pass


async def set_bot_commands(application: Application) -> None:
    """Русское меню команд и описание бота (вместо настройки в @BotFather)."""
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Запустить бота"),
                BotCommand("help", "Как пользоваться"),
                BotCommand("audio", "Скачать только звук по ссылке"),
            ]
        )
        await application.bot.set_my_short_description(
            "Скачиваю фото, видео и музыку по ссылке из Instagram, TikTok и др."
        )
        await application.bot.set_my_description(
            "Пришли ссылку на пост — я скачаю фото или видео и отправлю тебе.\n\n"
            "Могу достать музыку из видео в MP3.\n"
            "Работает с Instagram, TikTok, Twitter/X и другими сайтами."
        )
    except Exception:
        logger.exception("Не удалось настроить команды и описание бота")


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Sticker.ALL, sticker_id))
    application.add_handler(CommandHandler("audio", audio_command, block=False))
    application.add_handler(
        CallbackQueryHandler(audio_button, pattern=r"^a:", block=False)
    )
    # block=False: скачивание одного пользователя не задерживает остальных
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media, block=False)
    )
