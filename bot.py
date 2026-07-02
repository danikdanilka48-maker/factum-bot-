import os
import re
import requests
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

WAIT_IMPORTANCE = 0
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


def build_post(raw_ai_text: str, emoji: str) -> str:
    text = raw_ai_text.strip()
    text = re.sub(r'^(⚡️)+', '', text).strip()
    text = text.replace('**', '').replace('*', '').strip()

    m = re.search(r'(.+?[.!?])(\s|\n|$)', text, re.DOTALL)
    if m:
        first = m.group(1).strip()
        rest = text[len(m.group(0)):].strip()
    else:
        parts = text.split('\n', 1)
        first = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""

    post = f"{emoji}*{first}*"
    if rest:
        post += "\n\n" + rest
    return post


def ask_groq(text, importance):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"

    prompt = f"""Ти — досвідчений редактор українського новинного Telegram-каналу.

Перепиши новину українською мовою. СУВОРІ ПРАВИЛА:

1. МОВА: Бездоганна літературна українська мова. Жодних граматичних, орфографічних або пунктуаційних помилок. Жодного калькування з російської (наприклад, НЕ "відмічено" а "зафіксовано", НЕ "приймати участь" а "брати участь").

2. СТИЛЬ: Стисло, чітко, по суті. Оптимальна довжина — 2-4 речення залежно від важливості події. Не повторюй одну думку різними словами. Не додавай власних висновків і припущень.

3. ФАКТИ: Лише те, що є в оригіналі. Нічого від себе.

4. ФОРМАТ: Виведи лише чистий текст без зірочок, емодзі, посилань, хештегів і підписів — це буде додано окремо.

Оригінальний текст:
{text}

Виведи лише перефразований текст."""

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.15
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    data = r.json()
    if "choices" not in data:
        raise Exception(str(data))
    raw = data["choices"][0]["message"]["content"].strip()
    return build_post(raw, emoji)


async def check_access(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != ALLOWED_USER_ID:
        if update.message:
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
        await msg.reply_text("Не знайшов тексту. Перешліть текст або фото/відео з підписом.")
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

    importance = "важлива" if "Важлива" in update.message.text else "звичайна"
    context.user_data["importance"] = importance
    stored = user_data_store.get(update.effective_user.id, {})
    text = stored.get("text", "")
    media_type = stored.get("media_type")
    media_file_id = stored.get("media_file_id")

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        result = ask_groq(text, importance) + CHANNEL_FOOTER
        context.user_data["last_result"] = result
        context.user_data["last_media_type"] = media_type
        context.user_data["last_media_file_id"] = media_file_id

        retry_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Переробити", callback_data="retry")]])

        if media_type == "photo":
            await update.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        elif media_type == "video":
            await update.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        elif media_type == "animation":
            await update.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        else:
            await update.message.reply_text(result, parse_mode="Markdown", reply_markup=retry_btn)
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")
    return ConversationHandler.END


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_access(update):
        return

    stored = user_data_store.get(update.effective_user.id, {})
    text = stored.get("text", "")
    importance = context.user_data.get("importance", "звичайна")
    media_type = context.user_data.get("last_media_type")
    media_file_id = context.user_data.get("last_media_file_id")

    await query.message.reply_text("⏳ Переробляю...")
    try:
        result = ask_groq(text, importance) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        retry_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Переробити", callback_data="retry")]])

        if media_type == "photo":
            await query.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        elif media_type == "video":
            await query.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        elif media_type == "animation":
            await query.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=retry_btn)
        else:
            await query.message.reply_text(result, parse_mode="Markdown", reply_markup=retry_btn)
    except Exception as e:
        await query.message.reply_text(f"Помилка: {e}")


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
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    print("Бот запущено")
    app.run_polling()
