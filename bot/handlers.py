"""Обработчики Telegram-бота: скачивание медиа по ссылкам."""

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import yt_dlp
from telegram import BotCommand, InputMediaPhoto, InputMediaVideo, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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

MAX_FILE_SIZE = 49 * 1024 * 1024  # лимит Telegram для ботов ~50 МБ
MAX_ITEMS = 10                    # максимум файлов за одну ссылку
MAX_PARALLEL = 3                  # сколько скачиваний одновременно

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
URL_RE = re.compile(r"https?://\S+")

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто пришли мне ссылку на пост — я скачаю медиа и отправлю тебе.\n\n"
        "Используй /help, чтобы посмотреть все возможности."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Доступные команды:</b>\n\n"
        "/start — перезапустить бота\n"
        "/help — показать это сообщение\n\n"
        "<b>Как пользоваться:</b>\n"
        "Просто отправь ссылку на пост из:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• YouTube\n"
        "• Twitter / X\n"
        "• и многих других\n\n"
        "Бот скачает фото или видео и пришлёт их тебе.\n"
        "Карусели тоже поддерживаются (до 10 файлов)."
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


def run_download(url: str, temp_dir: Path):
    with yt_dlp.YoutubeDL(build_ydl_opts(temp_dir)) as ydl:
        return ydl.extract_info(url, download=True)


def extract_title(info) -> str | None:
    if not isinstance(info, dict):
        return None
    title = info.get("title") or info.get("description")
    if not title and info.get("entries"):
        first = next((e for e in info["entries"] if e), None)
        if first:
            title = first.get("title") or first.get("description")
    return title


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
                    cls(f, caption=cap if n == 0 else None, parse_mode=ParseMode.HTML)
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


async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = URL_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Пришли, пожалуйста, ссылку на пост.")
        return
    url = match.group(0)

    status_msg = await update.message.reply_text("Скачиваю... ⏳")
    temp_dir = DOWNLOAD_DIR / f"{update.effective_user.id}_{update.message.message_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with download_slots:
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

        if sent:
            await status_msg.delete()
        else:
            await status_msg.edit_text("Не удалось отправить ни одного файла.")

    except Exception as e:
        error_text = str(e).lower()
        logger.exception("Ошибка при обработке %s", url)

        if "private" in error_text or "login" in error_text or "403" in error_text:
            msg = (
                "Контент приватный или требует авторизации.\n"
                "Instagram часто блокирует скачивание без входа в аккаунт."
            )
        elif "unsupported" in error_text or "no video" in error_text:
            msg = "Этот тип ссылки пока не поддерживается или контент недоступен."
        elif "instagram" in error_text:
            msg = (
                "Instagram временно заблокировал скачивание.\n"
                "Попробуй позже или другую ссылку."
            )
        else:
            msg = (
                "Не удалось скачать это медиа 😔\n"
                "Проверь ссылку или попробуй другую."
            )

        try:
            await status_msg.edit_text(msg)
        except Exception:
            await update.message.reply_text(msg)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
            ]
        )
        await application.bot.set_my_short_description(
            "Скачиваю фото и видео по ссылке из Instagram, TikTok, YouTube и др."
        )
        await application.bot.set_my_description(
            "Пришли ссылку на пост — я скачаю фото или видео и отправлю тебе.\n\n"
            "Работает с Instagram, TikTok, YouTube, Twitter/X и многими другими сайтами."
        )
    except Exception:
        logger.exception("Не удалось настроить команды и описание бота")


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # block=False: скачивание одного пользователя не задерживает остальных
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media, block=False)
)
