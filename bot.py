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
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

WAIT_IMPORTANCE, WAIT_LENGTH = range(2)
user_texts = {}

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
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()

def ask_groq(text, importance, length):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
    size = "дуже коротко — 1-2 речення" if length == "коротко" else "стандартно — 3-4 речення"

    prompt = f"""Ти — досвідчений редактор українського новинного Telegram-каналу.

Перефразуй новину українською мовою. Суворі вимоги:
- Починай рядок з емодзі {emoji} БЕЗ пробілу після нього, одразу жирний заголовок
- Жирний текст роби через подвійні зірочки: **Заголовок**
- Після заголовку з нового рядка — текст новини ({size})
- Стиль: чіткий, журналістський, без води, правильна українська мова
- НЕ додавай посилань, хештегів, підписів, пояснень
- Поверни ТІЛЬКИ готовий пост, нічого більше

Приклад формату:
⚡️**Заголовок новини**
Текст новини тут.

Текст новини для переробки:
{text}"""

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}]
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    data = r.json()
    if "choices" not in data:
        raise Exception(str(data))
    return data["choices"][0]["message"]["content"].strip()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_texts[update.effective_user.id] = clean_text(update.message.text)
    keyboard = [["⚡️ Звичайна", "⚡️⚡️⚡️ Важлива"]]
    await update.message.reply_text(
        "Яка важливість новини?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return WAIT_IMPORTANCE

async def handle_importance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["importance"] = "важлива" if "Важлива" in update.message.text else "звичайна"
    keyboard = [["📝 Коротко", "📄 Стандартно"]]
    await update.message.reply_text(
        "Який розмір посту?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return WAIT_LENGTH

async def handle_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    length = "коротко" if "Коротко" in update.message.text else "стандартно"
    importance = context.user_data.get("importance", "звичайна")
    text = user_texts.get(update.effective_user.id, "")

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        result = ask_groq(text, importance, length) + CHANNEL_FOOTER
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

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
        states={
            WAIT_IMPORTANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_importance)],
            WAIT_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_length)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)
    print("Бот запущено")
    app.run_polling()
