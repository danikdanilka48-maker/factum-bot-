import os
import re
import html
import shutil
import logging
import tempfile
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
    InputMediaDocument,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

try:  # Telethon потрібен лише для публікації від імені твого акаунта
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import DocumentAttributeAnimated
except ImportError:
    TelegramClient = None

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("factum_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])


def _normalize_channel_id(raw):
    """Привести CHANNEL_ID до формату, який розуміє Telegram:
    @username для публічного каналу, -100XXXXXXXXXX для закритого.
    Прибирає пробіли й лапки, розуміє посилання t.me/..., додає @ і -100."""
    v = (raw or "").strip().strip("\"'").strip()
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(.+)$", v, re.I)
    if m:
        tail = m.group(1).split("?")[0].strip("/")
        if tail.startswith("+") or tail.startswith("joinchat"):
            return v  # запрошувальне посилання як адресу використати не можна
        if tail.startswith("c/"):  # https://t.me/c/1234567890/5
            return "-100" + tail[2:].split("/")[0]
        v = "@" + tail.split("/")[0]
    if re.fullmatch(r"\d+", v):  # «голий» id каналу без -100
        return "-100" + v
    if re.fullmatch(r"-\d+", v):
        return v
    if not v.startswith("@"):
        v = "@" + v
    return v


# ID або @username каналу для публікації.
CHANNEL_ID = _normalize_channel_id(os.environ["CHANNEL_ID"])
print(f"CHANNEL_ID = {CHANNEL_ID!r}")
CHANNEL_FOOTER = "\n\n[Фактум Новини | Підписатись](https://t.me/factum_ua)"

# --- Публікація від імені ТВОГО акаунта (userbot) ---
# Якщо бота не можна додати адміном у канал, у канал публікує твій акаунт
# (Telethon). Бот залишається інтерфейсом: приймає новину, робить пост, показує
# кнопки. Вмикається автоматично, коли задані API_ID, API_HASH і STRING_SESSION
# (їх дає gen_session.py). Твій акаунт має бути адміном каналу з правом публікації.
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
STRING_SESSION = os.environ.get("STRING_SESSION", "").strip()
USE_USERBOT = bool(API_ID and API_HASH and STRING_SESSION)
userbot = None          # заповнюється в _post_init
userbot_error = None    # причина, якщо акаунт не вдалося підключити
channel_entity = None   # канал, знайдений акаунтом

user_data_store = {}

BTN_NORMAL = "⚡️ Звичайна"
BTN_IMPORTANT = "⚡️⚡️⚡️ Важлива"

# Ліміт підпису до медіа в Telegram. Довший текст надсилається окремим
# повідомленням ПІСЛЯ медіа — медіа не губиться і публікація не падає.
CAPTION_LIMIT = 1024

# Збір альбому: ждемо, поки повідомлення групи ПЕРЕСТАНУТЬ надходити.
# Кожне нове фото/відео з альбому перезапускає відлік. ВАЖЛИВО: обробник
# при цьому НЕ блокується (PTB за замовчуванням обробляє оновлення по одному,
# тому sleep усередині обробника не давав решті фото альбому дійти до буфера).
ALBUM_WAIT_SECONDS = 2.0
media_group_buffers = {}  # media_group_id -> {"messages": [Message, ...], "task": Task}
_background_tasks = set()  # щоб фонові задачі не збирав garbage collector

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
    # Екрануємо символи, які ламають Markdown Telegram (_ ` [) — інакше пост
    # з "_" у тексті не відправляється взагалі ("can't parse entities").
    text = re.sub(r'([_`\[])', r'\\\1', text)
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


def _call_groq_sync(messages, temperature=0.15, timeout=30):
    """Синхронний виклик Groq (звичайний requests.post). НЕ викликати напряму
    з async-коду — він блокує процес; для цього є call_groq нижче."""
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


async def call_groq(messages, temperature=0.15, timeout=30):
    """Async-обгортка над Groq. Бот однопроцесний і обробляє одне оновлення за
    раз: звичайний requests.post усередині async-обробника заморожує ВЕСЬ
    бот (у тому числі натискання кнопок) на час запиту. asyncio.to_thread
    виносить запит у окремий потік, щоб решта бота лишалась чутливою."""
    return await asyncio.to_thread(_call_groq_sync, messages, temperature, timeout)


async def ask_groq(text, importance, temperature=0.15):
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

    raw = await call_groq([{"role": "user", "content": prompt}], temperature=temperature)
    return build_post(raw, emoji)


async def _make_post(text, importance, temperature=0.15):
    """Готовий пост. Якщо тексту немає (тільки медіа) — Groq не викликаємо,
    щоб модель нічого не вигадала: піде лише підпис каналу."""
    if not text:
        return CHANNEL_FOOTER.strip()
    return await ask_groq(text, importance, temperature) + CHANNEL_FOOTER


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


# ============ МЕДІА: витягнення і відправка ============
def _extract_media_item(msg):
    """Визначити тип і file_id медіа одного повідомлення
    (photo / video / animation / document)."""
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.animation:  # гіфка має і animation, і document — перевіряємо її раніше
        return {"type": "animation", "file_id": msg.animation.file_id}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id,
                "name": msg.document.file_name}
    return None


async def _md_fallback(send):
    """Викликати send("Markdown"); якщо Telegram не зміг розібрати розмітку —
    повторити без parse_mode. При такій помилці нічого не відправляється,
    тому повтор безпечний (дубля не буде)."""
    try:
        return await send("Markdown")
    except BadRequest as e:
        if "parse entities" in str(e).lower():
            return await send(None)
        raise


def _build_units(media_list):
    """Розкласти медіа на «одиниці» відправки. Альбом у Telegram — від 2 до 10
    елементів, фото з відео можна змішувати, а файли (document) — тільки між
    собою; гіфки не входять в альбоми. Довші набори ріжемо по 10, щоб жоден
    файл не загубився через ліміт."""
    units = []
    for kinds in (("photo", "video"), ("document",)):
        items = [m for m in media_list if m["type"] in kinds]
        for i in range(0, len(items), 10):
            chunk = items[i:i + 10]
            units.append(("group", chunk) if len(chunk) > 1 else ("single", chunk[0]))
    for m in media_list:
        if m["type"] == "animation":
            units.append(("single", m))
    return units


async def _send_group(bot, chat_id, items, caption):
    def build(parse_mode):
        classes = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument}
        group = []
        for i, it in enumerate(items):
            cap = caption if i == 0 else None
            group.append(classes[it["type"]](it["file_id"], caption=cap, parse_mode=parse_mode if cap else None))
        return group

    async def send(parse_mode):
        return await bot.send_media_group(chat_id=chat_id, media=build(parse_mode))

    return await _md_fallback(send)


async def _send_single(bot, chat_id, item, caption, kb):
    methods = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "animation": bot.send_animation,
        "document": bot.send_document,
    }
    method = methods[item["type"]]

    async def send(parse_mode):
        return await method(
            chat_id=chat_id,
            caption=caption,
            parse_mode=parse_mode if caption else None,
            reply_markup=kb,
            **{item["type"]: item["file_id"]},
        )

    return await _md_fallback(send)


async def _send_text(bot, chat_id, text, kb):
    async def send(parse_mode):
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=kb)

    return await _md_fallback(send)


async def _deliver(bot, chat_id, media_list, text, kb=None):
    """Єдина відправка поста: і адміну (з кнопками kb), і в канал (kb=None).

    Усі медіа йдуть разом; підпис — до першого медіа, якщо влізає в 1024
    символи, інакше текст іде окремим повідомленням після медіа. Кнопки до
    альбому прикріпити не можна, тому для нього шлемо окремий рядок «Готово»."""
    units = _build_units(media_list)
    caption_ok = bool(units) and bool(text) and len(text) <= CAPTION_LIMIT
    kb_used = False

    for idx, (kind, payload) in enumerate(units):
        caption = text if (idx == 0 and caption_ok) else None
        if kind == "group":
            await _send_group(bot, chat_id, payload, caption)
        else:
            use_kb = kb if (len(units) == 1 and caption_ok) else None
            await _send_single(bot, chat_id, payload, caption, use_kb)
            if use_kb is not None:
                kb_used = True

    if text and not caption_ok:
        await _send_text(bot, chat_id, text, kb)
        if kb is not None:
            kb_used = True

    if kb is not None and not kb_used:
        await bot.send_message(chat_id=chat_id, text="Готово 👇", reply_markup=kb)


# ============ ВІДПРАВКА В КАНАЛ ВІД ІМЕНІ АКАУНТА ============
def _tg_target(value):
    """Для Telethon числовий id має бути int, а @username — рядком."""
    return int(value) if re.fullmatch(r"-?\d+", str(value)) else value


def _post_to_html(post):
    """Markdown Bot API (*жирний*, [текст](url), \\_ екранування) → HTML для Telethon."""
    pattern = re.compile(
        r"(?<!\\)\[(.+?)\]\((https?://[^)\s]+)\)|(?<!\\)\*(.+?)\*", re.DOTALL
    )

    def unesc(s):
        return re.sub(r"\\([_`\[])", r"\1", s)

    def esc(s):
        return html.escape(unesc(s), quote=False)

    out, pos = [], 0
    for m in pattern.finditer(post):
        out.append(esc(post[pos:m.start()]))
        if m.group(1) is not None:
            url = html.escape(m.group(2), quote=True)
            out.append(f'<a href="{url}">{esc(m.group(1))}</a>')
        else:
            out.append(f"<b>{esc(m.group(3))}</b>")
        pos = m.end()
    out.append(esc(post[pos:]))
    return "".join(out)


# Скільки чекати одну мережеву операцію публікації від акаунта, перш ніж
# здатись і показати помилку замість вічного «завантаження» кнопки.
USERBOT_STEP_TIMEOUT = 120


async def _download_item(bot, item, tmpdir, index):
    """Скачати медіа, яке ти надіслав боту, у тимчасову папку."""
    try:
        tg_file = await asyncio.wait_for(bot.get_file(item["file_id"]), USERBOT_STEP_TIMEOUT)
    except BadRequest as e:
        if "too big" in str(e).lower():
            raise RuntimeError("файл більший за 20 МБ — Bot API не дозволяє його завантажити") from e
        raise
    except asyncio.TimeoutError as e:
        raise RuntimeError("Telegram не відповів на запит файлу (тайм-аут)") from e
    if item["type"] == "document":
        name = os.path.basename(item.get("name") or "file")
    else:
        name = "media" + {"photo": ".jpg", "video": ".mp4", "animation": ".mp4"}[item["type"]]
    folder = os.path.join(tmpdir, str(index))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    try:
        await asyncio.wait_for(tg_file.download_to_drive(custom_path=path), USERBOT_STEP_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"Скачування файлу зависло довше {USERBOT_STEP_TIMEOUT} с") from e
    return path


async def _deliver_as_user(bot, media_list, post_text):
    """Опублікувати пост у канал ВІД ІМЕНІ ТВОГО АКАУНТА.

    Спочатку скачуємо ВСІ файли, і лише потім відправляємо: якщо якийсь файл не
    скачався, у канал не піде нічого (без «половини альбому»)."""
    target = channel_entity or _tg_target(CHANNEL_ID)
    text = _post_to_html(post_text)
    visible_len = len(html.unescape(re.sub(r"<[^>]+>", "", text)))
    units = _build_units(media_list)
    caption_ok = bool(units) and visible_len <= CAPTION_LIMIT

    tmpdir = tempfile.mkdtemp(prefix="post_")
    try:
        prepared = []
        counter = 0
        for kind, payload in units:
            items = payload if kind == "group" else [payload]
            paths = []
            for it in items:
                paths.append(await _download_item(bot, it, tmpdir, counter))
                counter += 1
            prepared.append((items, paths))

        for idx, (items, paths) in enumerate(prepared):
            caption = text if (idx == 0 and caption_ok) else ""
            kinds = {it["type"] for it in items}
            extra = {}
            if kinds == {"document"}:
                extra["force_document"] = True  # щоб фото-файли не стискались у фото
            elif kinds == {"animation"}:
                extra["attributes"] = [DocumentAttributeAnimated()]  # лишається гіфкою
            try:
                await asyncio.wait_for(
                    userbot.send_file(
                        target,
                        file=paths if len(paths) > 1 else paths[0],
                        caption=caption,
                        parse_mode="html" if caption else None,
                        supports_streaming=True,
                        **extra,
                    ),
                    USERBOT_STEP_TIMEOUT,
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(f"Відправка в канал зависла довше {USERBOT_STEP_TIMEOUT} с") from e
        if text and not caption_ok:
            try:
                await asyncio.wait_for(
                    userbot.send_message(target, text, parse_mode="html"), USERBOT_STEP_TIMEOUT
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(f"Відправка тексту зависла довше {USERBOT_STEP_TIMEOUT} с") from e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _show_result(context, chat_id, text, media_list, importance, temperature=0.15):
    """Згенерувати пост, запам'ятати його разом з медіа і показати адміну."""
    result = await _make_post(text, importance, temperature)
    context.user_data["last_result"] = result
    context.user_data["last_media"] = media_list
    await _deliver(context.bot, chat_id, media_list, result, get_keyboard())


# ============ ПРИЙОМ ПОВІДОМЛЕНЬ ============
async def _start_news(messages, user_id, context):
    """Обробити зібране повідомлення (або весь альбом) як одну новину."""
    msg = messages[0]

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
        return

    user_data_store[user_id] = {"text": cleaned, "media": media_list}

    if not cleaned:
        # Тільки медіа: питати важливість нема сенсу — одразу готуємо пост
        context.user_data["importance"] = "звичайна"
        context.user_data.pop("awaiting_importance", None)
        await msg.reply_text("⏳ Готую...")
        await _show_result(context, msg.chat_id, "", media_list, "звичайна")
        return

    context.user_data["awaiting_importance"] = True
    keyboard = [[BTN_NORMAL, BTN_IMPORTANT]]
    await msg.reply_text(
        "Яка важливість новини?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )


async def _finish_album(group_id, user_id, context):
    """Фонова задача: дочекатись тиші в альбомі і обробити його ЦІЛКОМ."""
    try:
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
    except asyncio.CancelledError:
        return  # прийшло ще одне фото альбому — відлік почнеться заново
    entry = media_group_buffers.pop(group_id, None)
    if not entry:
        return
    messages = sorted(entry["messages"], key=lambda m: m.message_id)
    try:
        await _start_news(messages, user_id, context)
    except Exception as e:
        try:
            await messages[0].reply_text(f"Помилка: {e}")
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    msg = update.message
    user_id = update.effective_user.id

    # Відповідь на питання про важливість (кнопки-відповіді)
    if msg.text in (BTN_NORMAL, BTN_IMPORTANT) and context.user_data.get("awaiting_importance"):
        await handle_importance(update, context)
        return

    group_id = msg.media_group_id
    if group_id:
        # Частина альбому: кладемо в буфер і одразу повертаємось (не блокуємо
        # обробку решти фото). Обробку запускає фонова задача після «тиші».
        entry = media_group_buffers.setdefault(group_id, {"messages": [], "task": None})
        entry["messages"].append(msg)
        if entry["task"]:
            entry["task"].cancel()
        task = asyncio.create_task(_finish_album(group_id, user_id, context))
        entry["task"] = task
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return

    await _start_news([msg], user_id, context)


async def handle_importance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    importance = "важлива" if "Важлива" in update.message.text else "звичайна"
    context.user_data["importance"] = importance
    context.user_data.pop("awaiting_importance", None)
    stored = user_data_store.get(update.effective_user.id, {})
    text = stored.get("text", "")
    media_list = stored.get("media", [])

    await update.message.reply_text("⏳ Форматую...", reply_markup=ReplyKeyboardRemove())
    try:
        await _show_result(context, update.message.chat_id, text, media_list, importance)
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Усе, що не текст/фото/відео/гіфка/файл, — не ігноруємо мовчки."""
    if not await check_access(update):
        return
    await update.message.reply_text(
        "⚠️ Цей тип вкладення не підтримується. Працюю з текстом, фото, відео, гіфками та файлами."
    )


# ============ КНОПКИ ============
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
        await _show_result(context, query.message.chat_id, text, media_list, importance, temperature=0.7)
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
    clean_result = last_result.replace(CHANNEL_FOOTER, "").replace("*", "").replace("\\", "").strip()

    if not clean_result:
        await query.message.reply_text("⚠️ Немає тексту для виправлення. Спочатку сформуйте новину.")
        return

    await query.message.reply_text("✏️ Виправляю помилки...")
    try:
        fix_prompt = f"""Ти — коректор українського тексту. Виправ ВСІ помилки:
- Граматичні, орфографічні, пунктуаційні помилки
- Кальки з російської мови
- Повтори думок

Поверни лише виправлений текст без пояснень, зірочок, емодзі.

Текст:
{clean_result}"""
        raw = await call_groq([{"role": "user", "content": fix_prompt}], temperature=0.1)

        emoji = "⚡️⚡️⚡️" if importance == "важлива" else "⚡️"
        result = build_post(raw, emoji) + CHANNEL_FOOTER
        context.user_data["last_result"] = result

        await _deliver(context.bot, query.message.chat_id, media_list, result, get_keyboard())
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

    if USE_USERBOT and userbot is None:
        await query.message.reply_text(
            "⚠️ Акаунт для публікації не підключено"
            + (f": {userbot_error}" if userbot_error else ".")
            + "\nПеревір STRING_SESSION і перезапусти бота."
        )
        return

    try:
        if userbot is not None:
            await _deliver_as_user(context.bot, media_list, result)
        else:
            await _deliver(context.bot, CHANNEL_ID, media_list, result)
    except Exception as e:
        # last_result НЕ чистимо — можна натиснути «Опублікувати» ще раз
        hint = ""
        if userbot is None and "chat not found" in str(e).lower():
            hint = ("\n\nБот не бачить канал (він не адмін). Щоб публікувати від свого імені, "
                    "задай API_ID, API_HASH і STRING_SESSION (їх дає gen_session.py).")
        await query.message.reply_text(f"Помилка публікації: {e}{hint}")
        return

    # Пост опубліковано: чистимо, щоб старі кнопки не опублікували його вдруге
    context.user_data.pop("last_result", None)
    await query.message.reply_text("✅ Опубліковано в канал!")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def check_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/checkchannel — перевірка, чи бачить канал той, хто публікує."""
    if not await check_access(update):
        return

    lines = [f"CHANNEL_ID: {CHANNEL_ID!r}"]

    if USE_USERBOT:
        if userbot is None:
            lines.append(f"❌ Акаунт не підключено: {userbot_error or 'не запущено'}")
            await update.message.reply_text("\n".join(lines))
            return
        # Публікує твій акаунт — перевіряємо його права, а не бота
        try:
            me = await userbot.get_me()
            lines.append(f"Публікація від акаунта: {me.first_name} (id {me.id})")
            ent = channel_entity or await userbot.get_entity(_tg_target(CHANNEL_ID))
            lines.append(f"✅ Канал знайдено: {getattr(ent, 'title', ent)}")
            perms = await userbot.get_permissions(ent)
            can = bool(perms.is_creator or perms.post_messages)
            lines.append(
                "Право публікувати: "
                + ("✅ є" if can else "❌ немає — акаунт має бути адміном каналу з правом публікації")
            )
        except Exception as e:
            lines.append(f"❌ {e}")
        await update.message.reply_text("\n".join(lines))
        return

    lines.append("Режим: публікація через бота (бот має бути адміном каналу)")
    try:
        chat = await context.bot.get_chat(CHANNEL_ID)
        lines.append(f"✅ Канал знайдено: {chat.title}")
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        lines.append(f"Статус бота: {member.status}")
        if member.status == "creator":
            lines.append("Право публікувати: ✅ є")
        elif member.status == "administrator":
            can = getattr(member, "can_post_messages", None)
            lines.append("Право публікувати: " + ("✅ є" if can else "❌ немає — увімкни «Публікація повідомлень»"))
        else:
            lines.append("❌ Бот не адміністратор каналу")
    except Exception as e:
        lines.append(f"❌ {e}")
    await update.message.reply_text("\n".join(lines))


async def _post_init(application):
    """Запуск userbot усередині event loop бота (Telethon так вимагає)."""
    global userbot, userbot_error, channel_entity
    if not USE_USERBOT:
        print("Публікація: через бота (він має бути адміном каналу)")
        return
    if TelegramClient is None:
        userbot_error = "не встановлено telethon (pip install telethon hachoir)"
        print(f"⚠️ {userbot_error}")
        return

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            userbot_error = "STRING_SESSION недійсна — створи нову через gen_session.py"
            print(f"⚠️ {userbot_error}")
            return
        me = await client.get_me()
    except Exception as e:
        userbot_error = str(e)
        print(f"⚠️ Не вдалося підключити акаунт: {e}")
        return

    userbot = client
    target = _tg_target(CHANNEL_ID)
    try:
        try:
            channel_entity = await userbot.get_entity(target)
        except ValueError:
            # числовий id Telethon знає лише після завантаження діалогів
            await userbot.get_dialogs()
            channel_entity = await userbot.get_entity(target)
        print(f"Публікація: від акаунта {me.first_name} → {getattr(channel_entity, 'title', CHANNEL_ID)}")
    except Exception as e:
        channel_entity = None
        print(f"⚠️ Акаунт {me.first_name} не бачить канал {CHANNEL_ID!r}: {e}")


async def _post_shutdown(application):
    if userbot is not None:
        await userbot.disconnect()


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    context.user_data.pop("awaiting_importance", None)
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Будь-яка необроблена помилка тепер потрапляє в лог Render (а не
    губиться мовчки), і, якщо можливо, адміну прийде повідомлення замість
    вічної «загрузки» кнопки."""
    logger.exception("Необроблена помилка", exc_info=context.error)
    try:
        chat_id = None
        if isinstance(update, Update):
            if update.effective_chat:
                chat_id = update.effective_chat.id
        if chat_id:
            await context.bot.send_message(chat_id, f"⚠️ Внутрішня помилка: {context.error}")
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    entry_filter = filters.UpdateType.MESSAGE & (
        (filters.TEXT & ~filters.COMMAND)
        | filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
        | filters.Document.ALL
    )

    app.add_handler(MessageHandler(entry_filter, handle_message))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("checkchannel", check_channel))
    app.add_handler(CallbackQueryHandler(handle_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry$"))
    app.add_handler(CallbackQueryHandler(handle_fix, pattern="^fix$"))
    # Останнім: усе, що не підійшло вище (аудіо, голосові, стікери...)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_unsupported))
    app.add_error_handler(on_error)
    print("Бот запущено")
    app.run_polling()
