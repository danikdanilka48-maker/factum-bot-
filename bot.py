import os
import re
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL_FOOTER = "\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

def clean_text(text):
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()

def ask_gemini(text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""Ти — редактор українського новинного Telegram-каналу.
Твоє завдання:
1. Перефразуй новину українською мовою — стисло, чітко, журналістським стилем
2. Якщо новина звичайна — постав на початку ⚡️
3. Якщо новина важлива (бойові дії, прориви, загрози, офіційні заяви) — постав ⚡️⚡️⚡️
4. НЕ додавай жодних посилань, підписів, хештегів
5. Поверни ТІЛЬКИ готовий текст посту, без пояснень

Текст новини:
{text}"""
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(url, json=body, timeout=30)
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text
    cleaned = clean_text(raw)
    await update.message.reply_text("⏳ Форматую...")
    try:
        result = ask_gemini(cleaned) + CHANNEL_FOOTER
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

if __name__ == "__main__":
    import asyncio
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущено")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()
