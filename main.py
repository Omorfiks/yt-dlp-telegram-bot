import os
import re
import asyncio
import shutil
import tempfile
import logging
from contextlib import asynccontextmanager

import yt_dlp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from fastapi import FastAPI, Request

# Настройки логирования
logging.basicConfig(level=logging.INFO)

# Переменные окружения (задаются в настройках Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # Например: https://my-bot.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

if not BOT_TOKEN or not WEBHOOK_HOST:
    raise ValueError("Не заданы переменные окружения BOT_TOKEN или WEBHOOK_HOST")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ============================================================================
# НАСТРОЙКИ YT-DLP (Адаптированы из YouTubeDownloader.ps1 для Linux/Telegram)
# ============================================================================

def download_media(url, format_type, output_dir):
    """Скачивает медиа в указанную директорию с настройками из PS1 скрипта"""
    
    # Базовые настройки (аналог --newline, quiet)
    base_opts = {
        'outtmpl': os.path.join(output_dir, '%(title).100s.%(ext)s'), # Ограничиваем длину имени файла
        'quiet': True,
        'no_warnings': True,
        'noprogress': True, # Важно для сервера, чтобы не спамить в лог
        'no_mtime': True,   # Избегаем проблем с правами на изменение времени файла в Linux
    }

    if format_type == 'v':
        # Аналог: -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        # Добавлено ограничение height<=720, чтобы уложиться в лимит Telegram 50 МБ
        ydl_opts = {
            **base_opts,
            'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
            'merge_output_format': 'mp4',
        }
    elif format_type == 'a':
        # Аналог: -x --audio-format mp3 --audio-quality 0
        ydl_opts = {
            **base_opts,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192', # Хороший баланс качества и размера для Telegram
            }],
        }
    elif format_type == 's':
        # Аналог вкладки Subtitles из PS1 (ru, en приоритет)
        ydl_opts = {
            **base_opts,
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['ru', 'en', 'all'],
            'subtitlesformat': 'srt',
        }
    else:
        raise ValueError("Неизвестный тип формата")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # Корректировка имени файла для аудио (yt-dlp меняет расширение после постпроцессинга)
        if format_type == 'a':
            filename = os.path.splitext(filename)[0] + '.mp3'
        elif format_type == 's':
            # Для субтитров ищем файл .srt в директории, так как имя может отличаться
            srt_files = [f for f in os.listdir(output_dir) if f.endswith('.srt')]
            if srt_files:
                filename = os.path.join(output_dir, srt_files[0])
            else:
                raise Exception("Субтитры не найдены для этого видео.")
            
        return filename, info.get('title', 'media')

# ============================================================================
# ОБРАБОТЧИКИ TELEGRAM
# ============================================================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 **YouTube Downloader Bot**\n\n"
        "Используйте в любом чате через упоминание:\n"
        "`@ytdlp \"ссылка\" \"v\"` — Видео (MP4, до 720p)\n"
        "`@ytdlp \"ссылка\" \"a\"` — Аудио (MP3, 192kbps)\n"
        "`@ytdlp \"ссылка\" \"s\"` — Субтитры (SRT, Ru/En)\n\n"
        "*Пример:* `@ytdlp \"https://youtu.be/...\" \"a\"`",
        parse_mode="Markdown"
    )

@dp.message(F.text.contains("@ytdlp"))
async def handle_ytdlp(message: types.Message):
    text = message.text
    
    # Парсинг: @ytdlp "ссылка" "тип" или @ytdlp ссылка тип
    pattern = r'@ytdlp\s+["\']?(https?://[^\s"\']+)["\']?\s+["\']?([vas])["\']?'
    match = re.search(pattern, text, re.IGNORECASE)
    
    if not match:
        await message.answer(
            "❌ Неверный формат. Используйте:\n"
            "`@ytdlp \"ссылка\" \"v\"` (видео)\n"
            "`@ytdlp \"ссылка\" \"a\"` (аудио)\n"
            "`@ytdlp \"ссылка\" \"s\"` (субтитры)", 
            parse_mode="Markdown"
        )
        return

    url = match.group(1)
    format_type = match.group(2).lower()
    
    status_msg = await message.answer("⏳ Начинаю обработку...")
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Запускаем в отдельном потоке, чтобы не блокировать бота
        file_path, title = await asyncio.to_thread(download_media, url, format_type, temp_dir)
        
        await status_msg.edit_text("📤 Отправляю файл...")
        
        # Отправка в зависимости от типа
        with open(file_path, 'rb') as file:
            if format_type == 'v':
                await message.answer_video(video=file, caption=f"🎬 {title}", supports_streaming=True)
            elif format_type == 'a':
                await message.answer_audio(audio=file, caption=f"🎵 {title}", title=title)
            elif format_type == 's':
                await message.answer_document(document=file, caption=f"📝 Субтитры: {title}")
                
        await status_msg.delete()
        
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Private video" in error_msg or "Sign in" in error_msg:
            await status_msg.edit_text("❌ Ошибка: Видео приватное или требует входа. Бот не может использовать куки браузера на сервере.")
        elif "Subtitles" in error_msg:
            await status_msg.edit_text("❌ Ошибка: Для этого видео недоступны субтитры (Ru/En).")
        else:
            await status_msg.edit_text(f"❌ Ошибка загрузки. Проверьте ссылку.")
        logging.error(f"DownloadError: {e}")
    except Exception as e:
        await status_msg.edit_text("❌ Произошла непредвиденная ошибка.")
        logging.error(f"Unexpected error: {e}")
    finally:
        # Очистка временных файлов (важно для бесплатного хостинга!)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

# ============================================================================
# FASTAPI WEBHOOK (Для Render.com)
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        logging.info(f"Устанавливаю webhook: {WEBHOOK_URL}")
        await bot.set_webhook(url=WEBHOOK_URL)
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}

@app.get("/")
async def healthcheck():
    return {"status": "bot is running"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)