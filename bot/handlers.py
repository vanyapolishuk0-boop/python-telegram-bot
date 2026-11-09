"""Обработчики Telegram-бота: скачивание медиа по ссылкам."""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import yt_dlp
from pathlib import Path
from urllib.parse import urlparse
import subprocess
import tempfile
from pathlib import Path
from pydub import AudioSegment
from shazamio import Shazam
shazam = Shazam()

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

# Cookies текстом из переменной Railway COOKIES_TEXT
COOKIES_TEXT = os.getenv("COOKIES_TEXT", "").strip()
if COOKIES_TEXT and not COOKIES_FILE:
    COOKIES_FILE = str(Path(tempfile.gettempdir()) / "cookies.txt")
    Path(COOKIES_FILE).write_text(COOKIES_TEXT + "\n", encoding="utf-8")

MAX_FILE_SIZE = 49 * 1024 * 1024  # лимит Telegram для ботов ~50 МБ
MAX_DOWNLOAD_SIZE = 300 * 1024 * 1024  # больше этого не качаем даже для сжатия
MAX_ITEMS = 10                    # максимум файлов за одну ссылку
MAX_PARALLEL = 3                  # сколько скачиваний одновременно

# Стикер, который показывается, пока бот скачивает
STICKER_LOADING = "CAACAgIAAxkBAAP5at4iU67sExMlWjX2xVoefOOdxBQAAhABAAJSiZEjxEjx-swcBvM9BA"

# Название бота (показывается в приветствии)
BOT_NAME = "Nafig_Link"
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
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
    name = html.escape(update.effective_user.first_name or "друг")
    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Я — <b>{html.escape(BOT_NAME)}</b>.\n"
        "Скачиваю фото и видео из <b>Instagram</b> и <b>TikTok</b>.\n\n"
        "📸 Фото и карусели\n"
        "🎬 Видео и reels\n"
        "🎵 Музыка из видео в MP3\n\n"
        "🎧 Узнаю песню по голосовому или видео\n"
        "Просто пришли мне ссылку на пост 👇"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📖 Как пользоваться", callback_data="help")],
            [InlineKeyboardButton("🎵 Как скачать аудио", callback_data="audio_help")],
        ]
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "<b>Как пользоваться:</b>\n\n"
        "1️⃣ Скопируй ссылку на пост из Instagram или TikTok\n"
        "2️⃣ Отправь её мне\n"
        "3️⃣ Получи фото или видео\n\n"
        "Карусели приходят альбомом (до 10 файлов).",
        parse_mode=ParseMode.HTML,
    )


async def audio_help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "<b>Как скачать аудио:</b>\n\n"
        "🎵 Под каждым видео появится кнопка «Скачать аудио». "
        "Нажми её, и я пришлю музыку в формате MP3.\n\n"
        "Или напиши /audio и ссылку на видео.",
        parse_mode=ParseMode.HTML,
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
    """Подпись к медиа: описание поста и ссылка. Всё экранируется для HTML."""
    link = f'<a href="{html.escape(url, quote=True)}">🔗 Оригинальный пост</a>'
    text = (title or "").strip()
    if text and text.lower() not in {"na", "none"}:
        if len(text) > 800:
            text = text[:797] + "..."
        return f"{html.escape(text)}\n\n{link}"
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
        "max_filesize": MAX_DOWNLOAD_SIZE,
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


def run_gallery_dl(url: str, temp_dir: Path) -> str | None:
    """Фото и карусели Instagram через gallery-dl. Возвращает описание поста."""
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "-D", str(temp_dir),
        "--no-mtime",
        "--write-metadata",
        "-o", "audio=false",
        "-o", "filename={num:>03}_{filename}.{extension}",
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gallery-dl: {result.stderr[-300:]}")
    return pop_gallery_description(temp_dir)


def find_text(data: dict) -> str | None:
    """Ищет текст описания поста под разными именами полей."""
    for key in ("description", "caption", "title", "text"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def pop_gallery_description(temp_dir: Path) -> str | None:
    """Достаёт описание из .json-файлов gallery-dl и удаляет их."""
    description = None
    metas = sorted(temp_dir.glob("*.json"))
    if not metas:
        logger.warning("gallery-dl: файлов с метаданными не найдено")
    for meta in metas:
        if description is None:
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                logger.info("gallery-dl поля метаданных: %s", sorted(data.keys()))
                description = find_text(data)
            except Exception:
                logger.exception("Не удалось прочитать метаданные gallery-dl")
        meta.unlink(missing_ok=True)
    logger.info("Описание поста найдено: %s", bool(description))
    return description


def media_files(temp_dir: Path) -> list[Path]:
    """Скачанные файлы без служебных .json."""
    return sorted(
        f
        for f in temp_dir.glob("*")
        if f.is_file() and f.stat().st_size > 0 and f.suffix.lower() != ".json"
    )


def extract_title(info) -> str | None:
    if not isinstance(info, dict):
        return None
    title = info.get("description") or info.get("title")
    if not title and info.get("entries"):
        first = next((e for e in info["entries"] if e), None)
        if first:
            title = first.get("description") or first.get("title")
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

        title = None
        performer = None
        if isinstance(info, dict):
            title = info.get("track") or info.get("title")
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
async def notify_cookies_problem(context, error):
    text = str(error).lower()
    if not OWNER_ID or not ("login" in text or "cookie" in text):
        return
    now = time.time()
    if now - context.bot_data.get("cookies_alert", 0) < 6 * 3600:
        return
    context.bot_data["cookies_alert"] = now
    try:
        await context.bot.send_message(
            OWNER_ID,
            "⚠️ Cookies Instagram, похоже, протухли. Экспортируй новые и "
            "замени переменную COOKIES_TEXT на Railway.",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление владельцу")
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
            gallery_desc = None
            if "instagram.com/p/" in url:
                try:
                    gallery_desc = await asyncio.to_thread(
                        run_gallery_dl, url, temp_dir
                    )
                except Exception as e:
                    logger.exception("gallery-dl не справился, пробую yt-dlp")
                    await notify_cookies_problem(context, e)
            if not media_files(temp_dir):
                info = await asyncio.to_thread(run_download, url, temp_dir)
            if "tiktok.com" in url and not media_files(temp_dir):
                # Фото-посты TikTok yt-dlp не умеет, пробуем gallery-dl
                try:
                    gallery_desc = await asyncio.to_thread(
                        run_gallery_dl, url, temp_dir
                    )
                except Exception:
                    logger.exception("gallery-dl не справился с TikTok")

        title = extract_title(info) or gallery_desc
        all_files = media_files(temp_dir)
        if not all_files:
            raise RuntimeError("Не удалось скачать ни одного файла")

        files = []
        for f in all_files:
            if (
                f.stat().st_size > MAX_FILE_SIZE
                and f.suffix.lower() in VIDEO_EXT
                and HAS_FFMPEG
            ):
                try:
                    await status_msg.edit_text("Видео большое, сжимаю... 🗜")
                    f = await asyncio.to_thread(compress_video, f)
                except Exception:
                    logger.exception("Не удалось сжать видео")
            if f.stat().st_size <= MAX_FILE_SIZE:
                files.append(f)
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


def cut_audio_sample(src, dst):
    cmd = [FFMPEG_PATH, "-y", "-i", str(src), "-t", "25",
           "-vn", "-ac", "1", "-ar", "44100", str(dst)]
    subprocess.run(cmd, capture_output=True, check=True)


async def recognize_music(update, context):
    msg = update.message
    media = msg.voice or msg.audio or msg.video or msg.video_note
    if not media:
        return
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        await msg.reply_text("Файл больше 20 МБ, Telegram не даёт боту его скачать. Пришли фрагмент покороче.")
        return
    status = await msg.reply_text("🎧 Слушаю...")
    try:
        if HAS_FFMPEG:
            AudioSegment.converter = FFMPEG_PATH
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            wav = Path(tmp) / "sample.wav"
            tg_file = await media.get_file()
            await tg_file.download_to_drive(src)
            await asyncio.to_thread(cut_audio_sample, src, wav)
            recognize = getattr(shazam, "recognize", None) or shazam.recognize_song
            result = await recognize(str(wav))
        track = result.get("track")
        if not track:
            await status.edit_text("😕 Не смог узнать песню. Попробуй фрагмент, где музыка слышна лучше.")
            return
        text = f"🎶 {track.get('subtitle', '')} — {track.get('title', '')}"
        query = f"{track.get('subtitle', '')} {track.get('title', '')}".strip()
        context.bot_data[f"music_{status.message_id}"] = query
        rows = [[InlineKeyboardButton("⬇️ Скачать MP3", callback_data=f"m:{status.message_id}")]]
        url = track.get("url")
        if url:
            rows.append([InlineKeyboardButton("Открыть в Shazam", url=url)])
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        logger.exception("Ошибка распознавания музыки")
        await status.edit_text("Не получилось распознать. Попробуй ещё раз.")

def compress_video(src):
    dst = src.with_name(src.stem + "_small.mp4")
    for crf, width in ((30, 1280), (34, 854), (38, 640)):
        cmd = [
            FFMPEG_PATH, "-y", "-i", str(src),
            "-vf", f"scale='min({width},iw)':-2",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(dst),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg: " + r.stderr[-400:])
        if dst.stat().st_size <= MAX_FILE_SIZE:
            break
    return dst
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

def run_music_search(query, temp_dir):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(temp_dir) / "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "ignoreerrors": True,
        "max_downloads": 1,
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if HAS_FFMPEG:
        opts["ffmpeg_location"] = FFMPEG_PATH
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([f"scsearch5:{query}"])
        except yt_dlp.utils.MaxDownloadsReached:
            pass
    
    files = list(Path(temp_dir).glob("*.mp3"))
    return files[0] if files else None


async def send_music(message, query):
    status = await message.reply_text("🔎 Ищу трек...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            file = await asyncio.to_thread(run_music_search, query, tmp)
            if not file:
                await status.edit_text("😕 Не нашёл этот трек. Попробуй другое название.")
                return
            if file.stat().st_size > 49 * 1024 * 1024:
                await status.edit_text("Трек слишком большой для Telegram (лимит 50 МБ).")
                return
            await message.reply_audio(audio=file.read_bytes(), filename=file.name)
        await status.delete()
    except Exception:
        logger.exception("Ошибка поиска музыки")
        await status.edit_text("Не получилось скачать трек. Попробуй ещё раз.")


async def music_command(update, context):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Напиши название, например:\n/music Imagine Dragons Believer")
        return
    await send_music(update.message, query)


async def music_button(update, context):
    q = update.callback_query
    await q.answer()
    query = context.bot_data.get("music_" + q.data.split(":", 1)[1])
    if not query:
        await q.message.reply_text("Кнопка устарела. Используй /music название")
        return
    await send_music(q.message, query)
def register_handlers(application: Application) -> None:
    application.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE,
        recognize_music))
    application.add_handler(CommandHandler("music", music_command))
    application.add_handler(CallbackQueryHandler(music_button, pattern="^m:"))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Sticker.ALL, sticker_id))
    application.add_handler(CallbackQueryHandler(help_button, pattern=r"^help$"))
    application.add_handler(
        CallbackQueryHandler(audio_help_button, pattern=r"^audio_help$")
    )
    application.add_handler(CommandHandler("audio", audio_command, block=False))
    application.add_handler(
        CallbackQueryHandler(audio_button, pattern=r"^a:", block=False)
    )
    # block=False: скачивание одного пользователя не задерживает остальных
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media, block=False)
)
