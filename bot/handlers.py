import os
import logging
import asyncio
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import yt_dlp

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8904813799:AAFupFe_z8_rHpiR8IYJHAEVzskKJFrIblU"
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 49 * 1024 * 1024  # \~49 МБ
# ==============================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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
        "Карусели Instagram тоже поддерживаются."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


def is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()


def create_caption(url: str, title: str | None = None) -> str:
    """Создаёт красивую подпись к медиа"""
    caption = f"<a href='{url}'>🔗 Оригинальный пост</a>"
    
    if title and title.strip() and title.lower() not in ["", "na", "none"]:
        # Ограничиваем длину названия
        short_title = title.strip()
        if len(short_title) > 120:
            short_title = short_title[:117] + "..."
        caption = f"<b>{short_title}</b>\n\n" + caption
    
    return caption


async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("Пришли, пожалуйста, нормальную ссылку.")
        return

    status_msg = await update.message.reply_text("Скачиваю... ⏳")

    temp_dir = DOWNLOAD_DIR / f"{update.effective_user.id}_{update.message.message_id}"
    temp_dir.mkdir(exist_ok=True)

    ydl_opts = {
        "outtmpl": str(temp_dir / "%(id)s_%(playlist_index)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "max_filesize": MAX_FILE_SIZE,
        "retries": 3,
        "fragment_retries": 3,
    }

    if is_instagram(url):
        ydl_opts.update({
            "format": "best",
            "extractor_args": {
                "instagram": {
                    "webpage_download": True
                }
            }
        })
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"

    try:
        loop = asyncio.get_event_loop()

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await loop.run_in_executor(None, download)

        # Получаем название (если есть)
        title = None
        if isinstance(info, dict):
            title = info.get("title") or info.get("description")
            # Для плейлистов (каруселей) берём название первого элемента
            if not title and info.get("entries"):
                first = info["entries"][0]
                if first:
                    title = first.get("title") or first.get("description")

        files = sorted(temp_dir.glob("*"))
        files = [f for f in files if f.is_file() and f.stat().st_size > 0]

        if not files:
            raise Exception("Не удалось скачать ни одного файла")

        await status_msg.edit_text(f"Скачано файлов: {len(files)}. Отправляю...")

        caption = create_caption(url, title)
        sent_count = 0

        for index, file_path in enumerate(files):
            file_size = file_path.stat().st_size

            if file_size > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"Файл слишком большой ({file_size // 1024 // 1024} МБ), пропускаю."
                )
                continue

            ext = file_path.suffix.lower()
            # Подпись отправляем только к первому файлу
            current_caption = caption if index == 0 else None

            try:
                with open(file_path, "rb") as f:
                    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                        await update.message.reply_photo(
                            photo=f,
                            caption=current_caption,
                            parse_mode=ParseMode.HTML
                        )
                    elif ext in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                        await update.message.reply_video(
                            video=f,
                            caption=current_caption,
                            parse_mode=ParseMode.HTML,
                            supports_streaming=True
                        )
                    else:
                        await update.message.reply_document(
                            document=f,
                            caption=current_caption,
                            parse_mode=ParseMode.HTML
                        )
                sent_count += 1
            except Exception as send_error:
                logger.error(f"Ошибка отправки файла {file_path}: {send_error}")
                await update.message.reply_text("Не удалось отправить один из файлов.")

        if sent_count > 0:
            await status_msg.delete()
        else:
            await status_msg.edit_text("Не удалось отправить ни одного файла.")

    except Exception as e:
        error_text = str(e).lower()
        logger.error(f"Ошибка скачивания: {e}")

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
            msg = f"Не удалось скачать.\nОшибка: {str(e)[:180]}"

        await status_msg.edit_text(msg)

    finally:
        for f in temp_dir.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        try:
            temp_dir.rmdir()
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_media))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

    
