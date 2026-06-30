import os
import re
import requests
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ConversationHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])  # твой Telegram ID
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

WAIT_IMPORTANCE, WAIT_LENGTH = range(2)
user_data_store = {}


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()


def ensure_bold_first_sentence(result: str) -> str:
    """Подстраховка: если ИИ забыл сделать жирным первое предложение — делаем сами."""
    result = result.strip()
    if result.startswith("**"):
        return result  # уже жирный

    # отделяем эмодзи в начале (могут быть несколько ⚡️ подряд без пробела)
    m = re.match(r'^((?:⚡️)+)', result)
    prefix = m.group(1) if m else ""
    rest = result[len(prefix):].strip()

    # находим конец первого предложения (после .!? с большой буквы дальше, либо первый перенос строки)
    split_match = re.search(r'(.+?[.!?])(\s|\n|$)', rest, re.DOTALL)
    if split_match:
        first_sentence = split_match.group(1).strip()
        remainder = rest[len(split_match.group(0)):].strip()
    else:
        # нет точки — берём весь текст до первого переноса строки
        parts = rest.split("\n", 1)
        first_sentence = parts[0].strip()
        remainder = parts[1].strip() if len(parts) > 1 else ""

    bolded = f"{prefix}**{first_sentence}**"
    if remainder:
        bolded += "\n\n" + remainder
    return bolded


def ask_groq(text, importance, length):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
    size = "2-3 речення, дуже стисло" if length == "коротко" else "4-6 речень"

    prompt = f"""Ти професійний редактор українського новинного Telegram-каналу.
Перепиши новину нижче українською мовою СУВОРО за цим шаблоном (не відхиляйся від нього):

{emoji}**Перше речення новини тут, жирним, в подвійних зірочках.**

Другий і наступні абзаци звичайним текстом без зірочок, {size}.

ОБОВ'ЯЗКОВІ ПРАВИЛА:
- Перше речення ЗАВЖДИ обгорни у ** з обох боків, без винятків
- Емодзі {emoji} стоїть впритул до жирного тексту, без пробілу
- Пиши грамотною літературною українською, без калькування з російської
- НЕ вигадуй фактів
- НЕ додавай посилання, хештеги, підписи каналу
- НЕ пиши жодних коментарів від себе — лише сам пост у заданому форматі

Оригінальний текст новини:
{text}

Виведи лише готовий пост у точно такому форматі, як у шаблоні вище."""

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    data = r.json()
    if "choices" not in data:
        raise Exception(str(data))
    result = data["choices"][0]["message"]["content"].strip()
    result = ensure_bold_first_sentence(result)
    return result


async def check_access(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ У вас немає доступу до цього бота.")
        return False
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END

    msg = update.message
    raw_text = msg.text or msg.caption or ""
    cleaned = clean_text(raw_text)

    if not cleaned:
        await msg.reply_text("Не знайшов тексту в повідомленні. Перешліть текст або фото/відео з підписом.")
        return ConversationHandler.END

    media_type = None
    media_file_id = None
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    elif msg.animation:
        media_type = "animation"
        media_file_id = msg.animation.file_id

    user_data_store[update.effective_user.id] = {
        "text": cleaned,
        "media_type": media_type,
        "media_file_id": media_file_id,
    }

    keyboard = [["⚡️ Звичайна", "⚡️⚡️⚡️ Важлива"]]
    await msg.reply_text(
        "Яка важливість новини?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return WAIT_IMPORTANCE


async def handle_importance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END

    context.user_data["importance"] = "важлива" if "Важлива" in update.message.text else "звичайна"
    keyboard = [["📝 Коротко", "📄 Стандартно"]]
    await update.message.reply_text(
        "Який розмір посту?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return WAIT_LENGTH


async def handle_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END

    length = "коротко" if "Коротко" in update.message.text else "стандартно"
    importance = context.user_data.get("importance", "звичайна")
    stored = user_data_store.get(update.effective_user.id, {})
    text = stored.get("text", "")
    media_type = stored.get("media_type")
    media_file_id = stored.get("media_file_id")

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        result = ask_groq(text, importance, length) + CHANNEL_FOOTER

        if media_type == "photo":
            await update.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown")
        elif media_type == "video":
            await update.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown")
        elif media_type == "animation":
            await update.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown")
        else:
            await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    entry_filter = (
        (filters.TEXT & ~filters.COMMAND)
        | filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
    )

    conv = ConversationHandler(
        entry_points=[MessageHandler(entry_filter, handle_text)],
        states={
            WAIT_IMPORTANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_importance)],
            WAIT_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_length)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)
    print("Бот запущено")
    app.run_polling()
