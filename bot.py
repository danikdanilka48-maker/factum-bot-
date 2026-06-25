import os
import re
import asyncio
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL_FOOTER = "\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

def clean_text(text):
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text
    cleaned = clean_text(raw)

    prompt = f"""Ти — редактор українського новинного Telegram-каналу.

Твоє завдання:
1. Перефразуй новину українською мовою — стисло, чітко, журналістським стилем
2. Якщо новина звичайна — постав на початку ⚡️
3. Якщо новина важлива (бойові дії, прориви, загрози, офіційні заяви) — постав ⚡️⚡️⚡️
4. НЕ додавай жодних посилань, підписів, хештегів
5. Поверни ТІЛЬКИ готовий текст посту, без пояснень

Текст новини:
{cleaned}"""

    await update.message.reply_text("⏳ Форматую...")

    try:
        response = model.generate_content(prompt)
        result = response.text.strip() + CHANNEL_FOOTER
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Бот запущено")
app.run_polling()