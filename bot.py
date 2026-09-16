import os
import re
import asyncio
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
# ID або @username каналу для публікації. Бот повинен бути адміном
# цього каналу з правом надсилати повідомлення.
CHANNEL_ID = os.environ["CHANNEL_ID"]
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

WAIT_IMPORTANCE = 0
user_data_store = {}

# Час очікування решти повідомлень одного альбому (medіа-групи).
# Поки чекаємо — усі фото/відео з альбому встигають прийти і зібратись разом.
ALBUM_WAIT_SECONDS = 1.5
media_group_buffers = {}  # media_group_id -> [Message, ...]

# Список моделей Groq у порядку пріоритету. Якщо на поточній моделі
# закінчився денний ліміт токенів (rate_limit_exceeded) або сталася
# інша помилка — код автоматично пробує наступну модель зі списку.
# За потреби можна змінити порядок або додати/прибрати моделі.
GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]


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


def call_groq(messages, temperature=0.15, timeout=30):
    """Викликати Groq chat completion, перебираючи моделі зі списку
    GROQ_MODELS по черзі. Якщо модель впирається в ліміт токенів
    (rate_limit_exceeded) чи повертає іншу помилку — пробуємо наступну
    модель зі списку. Якщо жодна модель не спрацювала — кидаємо
    останню отриману помилку."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    last_error = None
    for model in GROQ_MODELS:
        body = {"model": model, "messages": messages, "temperature": temperature}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            data = r.json()
        except Exception as e:
            last_error = e
            continue

        if "choices" in data:
            return data["choices"][0]["message"]["content"].strip()

        # Помилка від Groq (ліміт токенів, недоступна модель тощо) —
        # запам'ятовуємо і переходимо до наступної моделі зі списку.
        last_error = Exception(f"{model}: {data}")
        continue

    raise last_error if last_error else Exception("Усі моделі Groq недоступні")


def ask_groq(text, importance, temperature=0.15):
    emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"

    prompt = f"""Ти — редактор українського новинного Telegram-каналу.

Твоє завдання:
1. Перефразуй новину українською мовою — стисло, чітко, журналістським стилем. ОБОВ'ЯЗКОВО зроби перший рядок жирним: Перший рядок тут. Далі з нового рядка — основний текст.
2. Якщо новина звичайна — постав на початку ⚡️
3. Якщо новина важлива (бойові дії, прориви, загрози, офіційні заяви) — постав ⚡️⚡️⚡️
4. НЕ додавай жодних посилань, підписів, хештегів
5. НІКОЛИ не пиши рядок "Джерело:" або будь-яке інше посилання на джерело інформації — це заборонено повністю, навіть якщо в оригінальному тексті була назва каналу.
6. НІКОЛИ не згадуй назви телеграм-каналів, груп, батальйонів чи інших джерел.
7. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати будь-які дані, яких немає в оригінальному тексті: імена, по батькові, прізвища, посади, дати, цифри, локації. Якщо в тексті є тільки прізвище — залиш тільки прізвище, НЕ додавай ім'я від себе. Якщо якоїсь інформації бракує — просто не згадуй її, не заповнюй прогалини вигаданими фактами.
8. Не додавай жодних власних висновків, оцінок чи деталей, яких не було в оригінальному тексті.
9. Ігноруй будь-які підписи, назви каналів, рекламні мітки та службові написи, які могли потрапити в текст випадково — вони НЕ є частиною новини.
10. КАТЕГОРИЧНО ЗАБОРОНЕНО повторювати в основному тексті ту саму інформацію, яка вже сказана в жирному першому рядку (заголовку) — навіть іншими словами. Основний текст має додавати НОВІ деталі з оригіналу, яких немає в заголовку. Якщо в оригінальному тексті немає жодної додаткової інформації понад заголовок — просто НЕ пиши другий абзац, обмежся одним жирним рядком.
11. Довжина посту має відповідати кількості реальної інформації в оригіналі: не розтягуй коротку новину на кілька речень штучно.
12. Поверни ТІЛЬКИ готовий текст посту, без пояснень

Текст новини:
{text}"""

    raw = call_groq([{"role": "user", "content": prompt}], temperature=temperature)
    return build_post(raw, emoji)


def get_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Опублікувати", callback_data="publish")],
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


def _extract_media_item(msg):
    """Визначити тип і file_id медіа одного повідомлення (photo/video/animation)."""
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id}
    return None


def _build_media_group(media_list, caption):
    """Зібрати список InputMedia для альбому. Альбоми Bot API підтримують
    тільки photo/video (гіфки-animation в альбом не входять)."""
    group = []
    for i, item in enumerate(media_list):
        cap = caption if i == 0 else None
        parse_mode = "Markdown" if cap else None
        if item["type"] == "photo":
            group.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode=parse_mode))
        elif item["type"] == "video":
            group.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode=parse_mode))
    return group


async def _send_result(target_message, media_list, result, kb):
    """Надіслати готовий пост адміну: альбом (кілька фото/відео), одне медіа,
    або тільки текст. Жодне медіа з альбому не губиться — усі частини,
    зібрані в handle_message, надсилаються разом однією медіа-групою."""
    photos_videos = [m for m in media_list if m["type"] in ("photo", "video")]
    animations = [m for m in media_list if m["type"] == "animation"]

    if len(photos_videos) > 1:
        group = _build_media_group(photos_videos, result)
        await target_message.reply_media_group(media=group)
        # Telegram не дозволяє прикріпити inline-кнопки до медіа-групи —
        # шлемо їх окремим коротким повідомленням одразу після альбому.
        await target_message.reply_text("Готово 👇", reply_markup=kb)
        for item in animations:
            await target_message.reply_animation(animation=item["file_id"])
        return

    if len(media_list) == 1:
        item = media_list[0]
        if item["type"] == "photo":
            await target_message.reply_photo(photo=item["file_id"], caption=result, parse_mode="Markdown", reply_markup=kb)
        elif item["type"] == "video":
            await target_message.reply_video(video=item["file_id"], caption=result, parse_mode="Markdown", reply_markup=kb)
        elif item["type"] == "animation":
            await target_message.reply_animation(animation=item["file_id"], caption=result, parse_mode="Markdown", reply_markup=kb)
        return

    # Тільки текст (або кілька гіфок без фото/відео — альбому з них не буває)
    await target_message.reply_text(result, parse_mode="Markdown", reply_markup=kb)
    for item in animations:
        await target_message.reply_animation(animation=item["file_id"])


async def _publish_to_channel(bot, media_list, result):
    """Опублікувати готовий пост у CHANNEL_ID: альбом, одне медіа, або тільки текст."""
    photos_videos = [m for m in media_list if m["type"] in ("photo", "video")]
    animations = [m for m in media_list if m["type"] == "animation"]

    if len(photos_videos) > 1:
        group = _build_media_group(photos_videos, result)
        await bot.send_media_group(chat_id=CHANNEL_ID, media=group)
        for item in animations:
            await bot.send_animation(chat_id=CHANNEL_ID, animation=item["file_id"])
        return

    if len(media_list) == 1:
        item = media_list[0]
        if item["type"] == "photo":
            await bot.send_photo(chat_id=CHANNEL_ID, photo=item["file_id"], caption=result, parse_mode="Markdown")
        elif item["type"] == "video":
            await bot.send_video(chat_id=CHANNEL_ID, video=item["file_id"], caption=result, parse_mode="Markdown")
        elif item["type"] == "animation":
            await bot.send_animation(chat_id=CHANNEL_ID, animation=item["file_id"], caption=result, parse_mode="Markdown")
        return

    await bot.send_message(chat_id=CHANNEL_ID, text=result, parse_mode="Markdown")
    for item in animations:
        await bot.send_animation(chat_id=CHANNEL_ID, animation=item["file_id"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END

    msg = update.message
    group_id = msg.media_group_id

    if group_id:
        # Це частина альбому. Збираємо ВСІ повідомлення групи в буфер і
        # обробляємо їх разом лише один раз — коли перше повідомлення
        # альбому дочекається решти (ALBUM_WAIT_SECONDS).
        buffer = media_group_buffers.setdefault(group_id, [])
        buffer.append(msg)
        if len(buffer) > 1:
            # Не перше повідомлення альбому — вже обробляється викликом,
            # який зараз чекає (нижче). Більше нічого робити не треба.
            return
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        messages = media_group_buffers.pop(group_id, buffer)
    else:
        messages = [msg]

    # Підпис/текст зазвичай є лише в одному повідомленні альбому — беремо перший непорожній
    raw_text = ""
    for m in messages:
        candidate = m.text or m.caption or ""
        if candidate:
            raw_text = candidate
            break
    cleaned = clean_text(raw_text)

    media_list = []
    for m in messages:
        item = _extract_media_item(m)
        if item:
            media_list.append(item)

    if not cleaned and not media_list:
        await msg.reply_text("Не знайшов тексту. Перешліть текст або фото/відео з підписом.")
        return ConversationHandler.END

    user_data_store[update.effective_user.id] = {
        "text": cleaned,
        "media": media_list,
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
    media_list = stored.get("media", [])

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        result = ask_groq(text, importance) + CHANNEL_FOOTER
        context.user_data["last_result"] = result
        context.user_data["last_media"] = media_list

        kb = get_keyboard()
        await _send_result(update.message, media_list, result, kb)
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
    media_list = context.user_data.get("last_media", [])

    await query.message.reply_text("⏳ Переробляю...")
    try:
        result = ask_groq(text, importance, temperature=0.7) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        kb = get_keyboard()
        await _send_result(query.message, media_list, result, kb)
    except Exception as e:
        await query.message.reply_text(f"Помилка: {e}")


async def handle_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    importance = context.user_data.get("importance", "звичайна")
    media_list = context.user_data.get("last_media", [])
    last_result = context.user_data.get("last_result", "")
    clean_result = last_result.replace(CHANNEL_FOOTER, "").replace("*", "").strip()

    await query.message.reply_text("✏️ Виправляю помилки...")
    try:
        fix_prompt = f"""Ти — коректор українського тексту. Виправ ВСІ помилки:
- Граматичні, орфографічні, пунктуаційні помилки
- Кальки з російської мови
- Повтори думок

Поверни лише виправлений текст без пояснень, зірочок, емодзі.

Текст:
{clean_result}"""
        raw = call_groq([{"role": "user", "content": fix_prompt}], temperature=0.1)

        emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
        result = build_post(raw, emoji) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        kb = get_keyboard()
        await _send_result(query.message, media_list, result, kb)
    except Exception as e:
        await query.message.reply_text(f"Помилка: {e}")


async def handle_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    media_list = context.user_data.get("last_media", [])
    result = context.user_data.get("last_result", "")

    if not result:
        await query.message.reply_text("⚠️ Немає готового посту для публікації. Спочатку сформуйте новину.")
        return

    try:
        await _publish_to_channel(context.bot, media_list, result)
        await query.message.reply_text("✅ Опубліковано в канал!")
        # Прибираємо кнопки, щоб випадково не опублікувати вдруге
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        await query.message.reply_text(f"Помилка публікації: {e}")


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
    app.add_handler(CallbackQueryHandler(handle_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    app.add_handler(CallbackQueryHandler(handle_fix, pattern="^fix$"))
    print("Бот запущено")
    app.run_polling()
