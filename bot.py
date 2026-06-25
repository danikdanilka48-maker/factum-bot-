import os
import re
import requests
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
CHANNEL_FOOTER = "\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

def clean_text(text):
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()

def ask_groq(text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"""Ти — редактор українського новинного Telegram-каналу.
Твоє завдання:
1. Перефразуй новину українською мовою — стисло, чітко, журналістським стилем
2. Якщо новина звичайна — постав на початку ⚡️
3. Якщо новина важлива (бойові дії, прориви, загрози, офіційні заяви) — постав ⚡️⚡️⚡️
4. НЕ додавай жодних посилань, підписів, хештегів
5. Поверни ТІЛЬКИ готовий текст посту, без пояснень

Текст новини:
{text}"""
    body = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "user", "content": prompt}]
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    data = r.json()
    if "choices" not in data:
        raise Exception(str(data))
    return data["choices"][0]["message"]["content"].strip()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text
    cleaned = clean_text(raw)
    await update.message.reply_text("⏳ Форматую...")
    try:
        result = ask_groq(cleaned) + CHANNEL_FOOTER
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        import traceback
        await update.message.reply_text(f"Помилка: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущено")
    app.run_polling()
