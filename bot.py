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
    text = re.sub(r'\[.*?\]\(https?://\S+\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    # убираем строки-подписи каналов (короткие строки с эмодзи, обычно в начале/конце)
    lines = text.split('\n')
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # пропускаем короткие строки без точки на конце — обычно это подписи/названия каналов
        if len(stripped) < 40 and stripped and not stripped.endswith(('.', '!', '?', ':')):
            continue
        clean_lines.append(line)
    return '\n'.join(clean_lines).strip()


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


def ask_groq(text, importance, temperature=0.15):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"

          prompt = f"""Ти — редактор українського новинного Telegram-каналу.

Твоє завдання:
1. Перефразуй новину українською мовою — стисло, чітко, журналістським стилем. ОБОВ'ЯЗКОВО зроби перший рядок жирним: **Перший рядок тут**. Далі з нового рядка — основний текст.
2. Якщо новина звичайна — постав на початку ⚡️
3. Якщо новина важлива (бойові дії, прориви, загрози, офіційні заяви) — постав ⚡️⚡️⚡️
4. НЕ додавай жодних посилань, підписів, хештегів
5. НІКОЛИ не пиши рядок "Джерело:" або будь-яке інше посилання на джерело інформації — це заборонено повністю, навіть якщо в оригінальному тексті була назва каналу.
6. НІКОЛИ не згадуй назви телеграм-каналів, груп, батальйонів чи інших джерел.
7. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати будь-які дані, яких немає в оригінальному тексті: імена, по батькові, прізвища, посади, дати, цифри, локації. Якщо в тексті є тільки прізвище — залиш тільки прізвище, НЕ додавай ім'я від себе. Якщо якоїсь інформації бракує — просто не згадуй її, не заповнюй прогалини вигаданими фактами.
8. Не додавай жодних власних висновків, оцінок чи деталей, яких не було в оригінальному тексті.
9. Ігноруй будь-які підписи, назви каналів, рекламні мітки та службові написи, які могли потрапити в текст випадково — вони НЕ є частиною новини.
10. Поверни ТІЛЬКИ готовий текст посту, без пояснень

Текст новини:
{text}"""

    body = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    data = r.json()
    if "choices" not in data:
        raise Exception(str(data))
    raw = data["choices"][0]["message"]["content"].strip()
    return build_post(raw, emoji)


def get_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Переробити", callback_data="retry")],
        [InlineKeyboardButton("✏️ Є помилка — виправити", callback_data="fix")]
    ])


async def check_access(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != ALLOWED_USER_ID:
        if update.message:
            await update.message.reply_text("⛔ У вас немає доступу до цього бота.")
        return False
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        kb = get_keyboard()
        if media_type == "photo":
            await update.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "video":
            await update.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "animation":
            await update.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        else:
            await update.message.reply_text(result, parse_mode="Markdown", reply_markup=kb)
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
        result = ask_groq(text, importance, temperature=0.7) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        kb = get_keyboard()
        if media_type == "photo":
            await query.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "video":
            await query.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "animation":
            await query.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        else:
            await query.message.reply_text(result, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await query.message.reply_text(f"Помилка: {e}")


async def handle_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    importance = context.user_data.get("importance", "звичайна")
    media_type = context.user_data.get("last_media_type")
    media_file_id = context.user_data.get("last_media_file_id")
    last_result = context.user_data.get("last_result", "")
    clean_result = last_result.replace(CHANNEL_FOOTER, "").replace("*", "").strip()

    await query.message.reply_text("✏️ Виправляю помилки...")
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        fix_prompt = f"""Ти — коректор українського тексту. Виправ ВСІ помилки:
- Граматичні, орфографічні, пунктуаційні помилки
- Кальки з російської мови
- Повтори думок

Поверни лише виправлений текст без пояснень, зірочок, емодзі.

Текст:
{clean_result}"""
        body = {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": fix_prompt}], "temperature": 0.1}
        r = requests.post(url, headers=headers, json=body, timeout=30)
        data = r.json()
        if "choices" not in data:
            raise Exception(str(data))

        emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
        raw = data["choices"][0]["message"]["content"].strip()
        result = build_post(raw, emoji) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        kb = get_keyboard()
        if media_type == "photo":
            await query.message.reply_photo(photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "video":
            await query.message.reply_video(video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        elif media_type == "animation":
            await query.message.reply_animation(animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
        else:
            await query.message.reply_text(result, parse_mode="Markdown", reply_markup=kb)
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
        entry_points=[MessageHandler(entry_filter, handle_message)],
        states={
            WAIT_IMPORTANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_importance)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    app.add_handler(CallbackQueryHandler(handle_fix, pattern="^fix$"))
    print("Бот запущено")
    app.run_polling()
