import os
import re
import requests
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

WAIT_IMPORTANCE = 0
user_data_store = {}
media_group_buffer = {}
media_group_text = {}


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


def ask_groq(text, importance, temperature=0.15):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
    prompt = f"""Ти — досвідчений редактор українського новинного Telegram-каналу.

Перепиши новину українською мовою. СУВОРІ ПРАВИЛА:

1. МОВА — бездоганна українська:
   - Ніяких граматичних, орфографічних або пунктуаційних помилок
   - НЕ калькуй з російської: "зафіксовано" (не "відмічено"), "брати участь" (не "приймати участь")
   - Правильні відмінки, узгодження, розділові знаки

2. БЕЗ ПОВТОРІВ:
   - Кожне речення містить НОВУ інформацію
   - Заборонено переказувати ту саму думку іншими словами
   - Краще одне чітке речення ніж три з однаковим змістом

3. СТИЛЬ:
   - 1-3 речення залежно від кількості фактів
   - Лише факти з оригіналу, нічого від себе
   - Без висновків і припущень

4. Виведи лише чистий текст без зірочок, емодзі, посилань, хештегів

Оригінальний текст:
{text}

Виведи лише перефразований текст."""

    body = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "temperature": temperature}
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


async def send_result(bot, chat_id, result, stored, kb):
    media_list = stored.get("media_list", [])
    media_type = stored.get("media_type")
    media_file_id = stored.get("media_file_id")

    if media_list and len(media_list) > 1:
        input_media = []
        for i, (ftype, fid) in enumerate(media_list):
            caption = result if i == 0 else None
            parse_mode = "Markdown" if i == 0 else None
            if ftype == "photo":
                input_media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=parse_mode))
            elif ftype == "video":
                input_media.append(InputMediaVideo(media=fid, caption=caption, parse_mode=parse_mode))
        await bot.send_media_group(chat_id=chat_id, media=input_media)
        await bot.send_message(chat_id=chat_id, text="👆 Готово!", reply_markup=kb)
    elif media_type == "photo":
        await bot.send_photo(chat_id=chat_id, photo=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
    elif media_type == "video":
        await bot.send_video(chat_id=chat_id, video=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
    elif media_type == "animation":
        await bot.send_animation(chat_id=chat_id, animation=media_file_id, caption=result, parse_mode="Markdown", reply_markup=kb)
    else:
        await bot.send_message(chat_id=chat_id, text=result, parse_mode="Markdown", reply_markup=kb)


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

    # текст із повідомлення або підпис під медіа
    raw_text = msg.text or msg.caption or ""
    cleaned = clean_text(raw_text)

    # визначаємо медіа
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

    # якщо це частина медіагрупи
    if msg.media_group_id and media_type in ("photo", "video"):
        group_id = msg.media_group_id
        if group_id not in media_group_buffer:
            media_group_buffer[group_id] = []
            media_group_text[group_id] = ""
        media_group_buffer[group_id].append((media_type, media_file_id))
        if cleaned:
            media_group_text[group_id] = cleaned

        # чекаємо поки всі фото прийдуть (через 1.5с після останнього)
        async def finalize_group():
            await asyncio.sleep(1.5)
            if group_id not in media_group_buffer:
                return
            files = media_group_buffer.pop(group_id, [])
            text = media_group_text.pop(group_id, "")

            if not text:
                await context.bot.send_message(chat_id=msg.chat_id, text="Не знайшов тексту. Перешліть фото/відео з підписом.")
                return

            user_data_store[update.effective_user.id] = {
                "text": text,
                "media_list": files,
                "media_type": files[0][0] if files else None,
                "media_file_id": files[0][1] if files else None,
            }
            keyboard = [["⚡️ Звичайна", "⚡️⚡️⚡️ Важлива"]]
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=f"Зібрав {len(files)} файлів. Яка важливість новини?",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            )

        asyncio.ensure_future(finalize_group())
        return WAIT_IMPORTANCE

    # звичайне одне повідомлення
    if not cleaned:
        await msg.reply_text("Не знайшов тексту. Перешліть текст або фото/відео з підписом.")
        return ConversationHandler.END

    user_data_store[update.effective_user.id] = {
        "text": cleaned,
        "media_list": [(media_type, media_file_id)] if media_type else [],
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

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        result = ask_groq(text, importance) + CHANNEL_FOOTER
        context.user_data["last_result"] = result
        context.user_data["last_stored"] = stored
        await send_result(context.bot, update.message.chat_id, result, stored, get_keyboard())
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
    await query.message.reply_text("⏳ Переробляю...")
    try:
        result = ask_groq(text, importance, temperature=0.7) + CHANNEL_FOOTER
        context.user_data["last_result"] = result
        await send_result(context.bot, query.message.chat_id, result, stored, get_keyboard())
    except Exception as e:
        await query.message.reply_text(f"Помилка: {e}")


async def handle_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return
    stored = user_data_store.get(update.effective_user.id, {})
    importance = context.user_data.get("importance", "звичайна")
    last_result = context.user_data.get("last_result", "")
    clean_result = last_result.replace(CHANNEL_FOOTER, "").replace("*", "").strip()

    await query.message.reply_text("✏️ Виправляю помилки...")
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        fix_prompt = f"""Ти — коректор українського тексту. Виправ ВСІ помилки (граматичні, орфографічні, пунктуаційні, кальки з російської, повтори думок). Поверни лише виправлений текст без пояснень, зірочок, емодзі.

Текст:
{clean_result}"""
        body = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": fix_prompt}], "temperature": 0.1}
        r = requests.post(url, headers=headers, json=body, timeout=30)
        data = r.json()
        if "choices" not in data:
            raise Exception(str(data))
        emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
        raw = data["choices"][0]["message"]["content"].strip()
        result = build_post(raw, emoji) + CHANNEL_FOOTER
        context.user_data["last_result"] = result
        await send_result(context.bot, query.message.chat_id, result, stored, get_keyboard())
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
