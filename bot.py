"""Telegram bot for Закрытый клуб Павла Сидоренко."""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import (
    BOT_TOKEN,
    ALLOWED_USERS,
    DEFAULT_MODEL,
    DEFAULT_TONE,
    STATIC_MODEL_OPTIONS,
    TONE_PROFILES,
    TOPIC_CATEGORIES,
)
from generator import (
    generate_post,
    search_news,
    format_tg_post,
    card_to_bytes,
    load_archive,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

router = Router()

ADMIN_USER_ID = 278199173  # Фёдор

# ============================================================
# Per-user settings
# ============================================================
SETTINGS_FILE = Path(__file__).parent / "user_settings.json"


def _load_all_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {}


def _save_all_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_settings(user_id: int) -> dict:
    all_s = _load_all_settings()
    return all_s.get(str(user_id), {
        "tone": "Профи",
        "theme": "warm",
        "model": DEFAULT_MODEL,
    })


def set_settings(user_id: int, settings: dict):
    all_s = _load_all_settings()
    all_s[str(user_id)] = settings
    _save_all_settings(all_s)


# ============================================================
# Topics persistence
# ============================================================
TOPICS_FILE = Path(__file__).parent / "topics_history.json"


def _load_topics_history() -> list:
    if TOPICS_FILE.exists():
        try:
            return json.loads(TOPICS_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return []


def _save_topics_history(data: list):
    TOPICS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def save_topics(topics: list):
    """Append new topics batch to history."""
    history = _load_topics_history()
    batch = {
        "date": datetime.now().isoformat(),
        "topics": topics,
    }
    history.append(batch)
    # Keep last 50 batches
    if len(history) > 50:
        history = history[-50:]
    _save_topics_history(history)


def get_all_saved_topics() -> list:
    """Return flat list of all saved topics (newest first)."""
    history = _load_topics_history()
    result = []
    for batch in reversed(history):
        for t in batch.get("topics", []):
            if t not in result:
                result.append(t)
    return result


# ============================================================
# Access control
# ============================================================
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ============================================================
# FSM
# ============================================================
class Gen(StatesGroup):
    waiting_topic = State()
    edit_title = State()
    edit_rubric = State()
    edit_lead = State()


# ============================================================
# Keyboards
# ============================================================
def main_menu_kb(user_id: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔍 Найти темы", callback_data="categories"),
            InlineKeyboardButton(text="✏️ Своя тема", callback_data="custom_topic"),
        ],
        [
            InlineKeyboardButton(text="📋 Сохранённые темы", callback_data="saved_topics:0"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
        ],
    ]
    if user_id == ADMIN_USER_ID:
        rows.append([InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_kb() -> InlineKeyboardMarkup:
    buttons = []
    for key, cat in TOPIC_CATEGORIES.items():
        buttons.append([InlineKeyboardButton(
            text=cat["label"],
            callback_data=f"cat:{key}",
        )])
    buttons.append([InlineKeyboardButton(text="← Меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def settings_kb(s: dict) -> InlineKeyboardMarkup:
    tone = s.get("tone", "Клубный")
    theme = s.get("theme", "warm")
    model = s.get("model", DEFAULT_MODEL)
    theme_label = {"warm": "🟤 Тёплая", "dark": "⚫ Тёмная", "blue": "🔵 Блюзовая"}.get(theme, theme)
    model_short = model.replace("gemini-", "").replace("-preview", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Тон: {tone}", callback_data="set_tone")],
        [InlineKeyboardButton(text=f"Карточка: {theme_label}", callback_data="set_theme")],
        [InlineKeyboardButton(text=f"Модель: {model_short}", callback_data="set_model")],
        [InlineKeyboardButton(text="← Меню", callback_data="back_main")],
    ])


def tone_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"tone:{name}")]
        for name in TONE_PROFILES
    ]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def theme_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟤 Тёплая (винтаж)", callback_data="theme:warm")],
        [InlineKeyboardButton(text="⚫ Тёмная (клуб)", callback_data="theme:dark")],
        [InlineKeyboardButton(text="🔵 Блюзовая", callback_data="theme:blue")],
        [InlineKeyboardButton(text="← Назад", callback_data="settings")],
    ])


def model_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=m.replace("gemini-", "").replace("-preview", ""),
            callback_data=f"model:{m}",
        )]
        for m in STATIC_MODEL_OPTIONS[:5]
    ]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def news_topics_kb(topics: list, offset: int = 0, cat_key: str = "all") -> InlineKeyboardMarkup:
    buttons = []
    for i, t in enumerate(topics[:10]):
        topic_text = t.get("topic", "")[:45]
        rubric = t.get("rubric", "")
        buttons.append([InlineKeyboardButton(
            text=f"{rubric} · {topic_text}",
            callback_data=f"pick:{offset + i}",
        )])
    buttons.append([
        InlineKeyboardButton(text="🔄 Ещё темы", callback_data=f"cat:{cat_key}"),
        InlineKeyboardButton(text="📂 Категории", callback_data="categories"),
        InlineKeyboardButton(text="← Меню", callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def saved_topics_kb(topics: list, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 10
    start = page * per_page
    chunk = topics[start:start + per_page]
    buttons = []
    for i, t in enumerate(chunk):
        topic_text = t.get("topic", "")[:45]
        rubric = t.get("rubric", "")
        buttons.append([InlineKeyboardButton(
            text=f"{rubric} · {topic_text}",
            callback_data=f"saved_pick:{start + i}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"saved_topics:{page - 1}"))
    if start + per_page < len(topics):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"saved_topics:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← Меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ============================================================
# Handlers
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ ограничен.")
        return
    await message.answer(
        "🎸 <b>Закрытый клуб Павла Сидоренко</b>\n\n"
        "Генератор карточек и постов о гитаре, джазе, блюзе и оборудовании.\n\n"
        "Выбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await _send_help(message)


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "🎸 <b>Главное меню</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    if not is_allowed(message.from_user.id):
        return
    topic = message.text.replace("/generate", "").strip()
    if not topic:
        await message.answer(
            "Укажи тему: <code>/generate Wes Montgomery и октавы</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _do_generate(message, topic)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        await message.answer("⛔ Только для администратора.")
        return
    await _send_stats(message)


# ============================================================
# Callbacks
# ============================================================

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🎸 <b>Закрытый клуб Павла Сидоренко</b>\n\nВыбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(cb.from_user.id),
    )
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await _send_help(cb.message, edit=True)
    await cb.answer()


@router.callback_query(F.data == "custom_topic")
async def cb_custom_topic(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Gen.waiting_topic)
    await cb.message.edit_text(
        "✏️ Напиши тему для поста.\n\n"
        "Примеры:\n"
        "• <i>Какие струны использует John Mayer</i>\n"
        "• <i>Ibanez Tube Screamer — почему все его копируют</i>\n"
        "• <i>Celestion Greenback vs Vintage 30</i>\n\n"
        "Или отправь /menu чтобы вернуться.",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.message(Gen.waiting_topic)
async def on_topic_text(message: Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        return
    await state.clear()
    await _do_generate(message, message.text.strip())


# --- Categories ---

@router.callback_query(F.data == "categories")
async def cb_categories(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔ Нет доступа")
        return
    await cb.message.edit_text(
        "🔍 <b>Выбери тематику:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=categories_kb(),
    )
    await cb.answer()


# --- News search ---

@router.callback_query(F.data.startswith("cat:"))
async def cb_cat_search(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔ Нет доступа")
        return
    cat_key = cb.data.split(":", 1)[1]
    cat = TOPIC_CATEGORIES.get(cat_key, TOPIC_CATEGORIES["all"])
    cat_label = cat["label"]
    query = cat["query"]

    await cb.answer(f"🔍 Ищу: {cat_label}...")
    search_msg = await cb.message.answer(
        f"🔍 Ищу темы: <b>{cat_label}</b>...", parse_mode=ParseMode.HTML
    )

    try:
        s = get_settings(cb.from_user.id)
        topics = await asyncio.to_thread(search_news, query, s.get("model", DEFAULT_MODEL))
        if not topics:
            await search_msg.edit_text(
                "Не удалось найти темы. Попробуй ещё раз.",
                reply_markup=main_menu_kb(cb.from_user.id),
            )
            return

        # Persist topics
        save_topics(topics)
        _topic_cache[cb.from_user.id] = topics

        lines = []
        for i, t in enumerate(topics):
            hook = t.get("hook", "")
            lines.append(f"{i+1}. <b>{t.get('rubric', '')}</b> · {t.get('topic', '')}")
            if hook:
                lines.append(f"   <i>{hook}</i>")

        text = f"🔍 <b>{cat_label} — найденные темы:</b>\n\n" + "\n".join(lines) + "\n\nВыбери тему:"
        await search_msg.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=news_topics_kb(topics, cat_key=cat_key)
        )
    except Exception as e:
        log.exception("News search failed")
        await search_msg.edit_text(
            f"❌ Ошибка поиска: {e}", reply_markup=main_menu_kb(cb.from_user.id)
        )


_topic_cache: dict[int, list] = {}
_post_cache: dict[int, dict] = {}  # user_id -> last generated post


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick_topic(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔")
        return
    idx = int(cb.data.split(":")[1])
    topics = _topic_cache.get(cb.from_user.id, [])
    if idx >= len(topics):
        await cb.answer("Тема не найдена")
        return
    topic = topics[idx].get("topic", "")
    await cb.answer(f"Генерирую: {topic[:30]}...")
    status_msg = await cb.message.answer(
        f"⏳ Генерирую пост: <i>{topic}</i>...", parse_mode=ParseMode.HTML
    )
    await _do_generate_from_status(status_msg, cb.from_user.id, topic)


# --- Saved topics ---

@router.callback_query(F.data.startswith("saved_topics:"))
async def cb_saved_topics(cb: CallbackQuery):
    page = int(cb.data.split(":")[1])
    topics = get_all_saved_topics()
    if not topics:
        await cb.answer("Пока нет сохранённых тем")
        return

    total = len(topics)
    per_page = 10
    total_pages = (total + per_page - 1) // per_page

    text = f"📋 <b>Сохранённые темы</b> (стр. {page + 1}/{total_pages}, всего {total}):\n\nВыбери тему для генерации:"

    try:
        await cb.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=saved_topics_kb(topics, page)
        )
    except Exception:
        await cb.message.answer(
            text, parse_mode=ParseMode.HTML, reply_markup=saved_topics_kb(topics, page)
        )
    await cb.answer()


@router.callback_query(F.data.startswith("saved_pick:"))
async def cb_saved_pick(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔")
        return
    idx = int(cb.data.split(":")[1])
    topics = get_all_saved_topics()
    if idx >= len(topics):
        await cb.answer("Тема не найдена")
        return
    topic = topics[idx].get("topic", "")
    await cb.answer(f"Генерирую: {topic[:30]}...")
    status_msg = await cb.message.answer(
        f"⏳ Генерирую пост: <i>{topic}</i>...", parse_mode=ParseMode.HTML
    )
    await _do_generate_from_status(status_msg, cb.from_user.id, topic)


# --- Settings ---

@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery):
    s = get_settings(cb.from_user.id)
    await cb.message.edit_text(
        "⚙️ <b>Настройки</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_kb(s),
    )
    await cb.answer()


@router.callback_query(F.data == "set_tone")
async def cb_set_tone(cb: CallbackQuery):
    await cb.message.edit_text("Выбери тон:", reply_markup=tone_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tone:"))
async def cb_tone_picked(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    s = get_settings(cb.from_user.id)
    s["tone"] = name
    set_settings(cb.from_user.id, s)
    await cb.message.edit_text(
        f"✅ Тон: <b>{name}</b>", parse_mode=ParseMode.HTML, reply_markup=settings_kb(s)
    )
    await cb.answer()


@router.callback_query(F.data == "set_theme")
async def cb_set_theme(cb: CallbackQuery):
    await cb.message.edit_text("Выбери тему карточки:", reply_markup=theme_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("theme:"))
async def cb_theme_picked(cb: CallbackQuery):
    theme = cb.data.split(":", 1)[1]
    s = get_settings(cb.from_user.id)
    s["theme"] = theme
    set_settings(cb.from_user.id, s)
    label = {"warm": "🟤 Тёплая", "dark": "⚫ Тёмная", "blue": "🔵 Блюзовая"}.get(theme, theme)
    await cb.message.edit_text(
        f"✅ Карточка: <b>{label}</b>", parse_mode=ParseMode.HTML, reply_markup=settings_kb(s)
    )
    await cb.answer()


@router.callback_query(F.data == "set_model")
async def cb_set_model(cb: CallbackQuery):
    await cb.message.edit_text("Выбери модель:", reply_markup=model_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("model:"))
async def cb_model_picked(cb: CallbackQuery):
    model = cb.data.split(":", 1)[1]
    s = get_settings(cb.from_user.id)
    s["model"] = model
    set_settings(cb.from_user.id, s)
    short = model.replace("gemini-", "").replace("-preview", "")
    await cb.message.edit_text(
        f"✅ Модель: <b>{short}</b>", parse_mode=ParseMode.HTML, reply_markup=settings_kb(s)
    )
    await cb.answer()


# --- Edit card ---

def edit_card_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Заголовок", callback_data="edit:title"),
            InlineKeyboardButton(text="🏷 Рубрика", callback_data="edit:rubric"),
        ],
        [
            InlineKeyboardButton(text="💬 Лид-текст", callback_data="edit:lead"),
        ],
        [
            InlineKeyboardButton(text="🟤 Тёплая", callback_data="card_theme:warm"),
            InlineKeyboardButton(text="⚫ Тёмная", callback_data="card_theme:dark"),
            InlineKeyboardButton(text="🔵 Блюзовая", callback_data="card_theme:blue"),
        ],
        [InlineKeyboardButton(text="← Меню", callback_data="back_main")],
    ])


@router.callback_query(F.data == "edit_card")
async def cb_edit_card(cb: CallbackQuery):
    post = _post_cache.get(cb.from_user.id)
    if not post:
        await cb.answer("Нет карточки для редактирования")
        return
    text = (
        "✏️ <b>Редактирование карточки</b>\n\n"
        f"<b>Заголовок:</b> {post.get('title', '—')}\n"
        f"<b>Рубрика:</b> {post.get('rubric', '—')}\n"
        f"<b>Лид:</b> {post.get('essence', '—')}\n\n"
        "Что изменить?"
    )
    await cb.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=edit_card_kb())
    await cb.answer()


@router.callback_query(F.data == "edit:title")
async def cb_edit_title(cb: CallbackQuery, state: FSMContext):
    post = _post_cache.get(cb.from_user.id)
    if not post:
        await cb.answer("Нет карточки")
        return
    await state.set_state(Gen.edit_title)
    await cb.message.edit_text(
        f"Текущий заголовок: <b>{post.get('title', '—')}</b>\n\nОтправь новый заголовок:",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "edit:rubric")
async def cb_edit_rubric(cb: CallbackQuery, state: FSMContext):
    post = _post_cache.get(cb.from_user.id)
    if not post:
        await cb.answer("Нет карточки")
        return
    await state.set_state(Gen.edit_rubric)
    await cb.message.edit_text(
        f"Текущая рубрика: <b>{post.get('rubric', '—')}</b>\n\nОтправь новую рубрику:",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "edit:lead")
async def cb_edit_lead(cb: CallbackQuery, state: FSMContext):
    post = _post_cache.get(cb.from_user.id)
    if not post:
        await cb.answer("Нет карточки")
        return
    await state.set_state(Gen.edit_lead)
    await cb.message.edit_text(
        f"Текущий лид: <b>{post.get('essence', '—')}</b>\n\nОтправь новый текст:",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.message(Gen.edit_title)
async def on_edit_title(message: Message, state: FSMContext):
    await state.clear()
    await _apply_card_edit(message, "title", message.text.strip())


@router.message(Gen.edit_rubric)
async def on_edit_rubric(message: Message, state: FSMContext):
    await state.clear()
    await _apply_card_edit(message, "rubric", message.text.strip())


@router.message(Gen.edit_lead)
async def on_edit_lead(message: Message, state: FSMContext):
    await state.clear()
    await _apply_card_edit(message, "essence", message.text.strip())


async def _apply_card_edit(message: Message, field: str, value: str):
    post = _post_cache.get(message.from_user.id)
    if not post:
        await message.answer("Нет карточки для редактирования. Сгенерируй новую.")
        return

    post[field] = value

    # Regenerate card image with updated text
    from image import render_card_image
    s = get_settings(message.from_user.id)
    theme = s.get("theme", "warm")
    post["card_image"] = render_card_image(
        post.get("title", ""),
        post.get("rubric", ""),
        post.get("essence", ""),
        None,
        theme=theme,
    )
    _post_cache[message.from_user.id] = post

    # Send updated card
    card_bytes = card_to_bytes(post["card_image"])
    photo = BufferedInputFile(card_bytes, filename="card.png")
    await message.answer_photo(photo=photo)

    field_names = {"title": "Заголовок", "rubric": "Рубрика", "essence": "Лид"}
    await message.answer(
        f"✅ {field_names.get(field, field)} обновлён.\n\nПродолжить редактирование?",
        parse_mode=ParseMode.HTML,
        reply_markup=edit_card_kb(),
    )


@router.callback_query(F.data.startswith("card_theme:"))
async def cb_card_theme(cb: CallbackQuery):
    post = _post_cache.get(cb.from_user.id)
    if not post:
        await cb.answer("Нет карточки")
        return
    theme = cb.data.split(":", 1)[1]

    # Update user settings too
    s = get_settings(cb.from_user.id)
    s["theme"] = theme
    set_settings(cb.from_user.id, s)

    # Regenerate card
    from image import render_card_image
    post["card_image"] = render_card_image(
        post.get("title", ""),
        post.get("rubric", ""),
        post.get("essence", ""),
        None,
        theme=theme,
    )
    _post_cache[cb.from_user.id] = post

    card_bytes = card_to_bytes(post["card_image"])
    photo = BufferedInputFile(card_bytes, filename="card.png")
    await cb.message.answer_photo(photo=photo)

    label = {"warm": "🟤 Тёплая", "dark": "⚫ Тёмная", "blue": "🔵 Блюзовая"}.get(theme, theme)
    await cb.message.answer(
        f"✅ Тема карточки: <b>{label}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=edit_card_kb(),
    )
    await cb.answer()


# --- Admin stats ---

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_USER_ID:
        await cb.answer("⛔ Только для администратора")
        return
    await _send_stats(cb.message, edit=True)
    await cb.answer()


# ============================================================
# Helpers
# ============================================================

HELP_TEXT = """🎸 <b>Закрытый клуб Павла Сидоренко — Помощь</b>

<b>Команды:</b>
/start — главное меню
/menu — показать меню
/help — эта справка
/generate &lt;тема&gt; — сгенерировать пост по теме

<b>Как пользоваться:</b>
1. <b>🔍 Найти темы</b> — выбери тематику (педали, гитары, музыканты и т.д.), бот найдёт 5-7 тем через Google.
2. <b>✏️ Своя тема</b> — напиши свою тему текстом.
3. <b>📋 Сохранённые темы</b> — все ранее найденные темы. Можно вернуться и сгенерировать.
4. <b>⚙️ Настройки</b> — тон текста, тема карточки, модель AI.

<b>Тематики:</b>
🎛 Педали и эффекты · 🎸 Гитары · 🎤 Музыканты · 🔊 Усилители · ✋ Техника · 🔧 Оборудование · 💿 Релизы · 📜 История

<b>Результат:</b>
Карточка 1080×1080 + информационный пост для Telegram."""


async def _send_help(target: Message, edit: bool = False):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Меню", callback_data="back_main")]
    ])
    if edit:
        await target.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await target.answer(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb)


async def _send_stats(target: Message, edit: bool = False):
    archive = load_archive()
    total = len(archive)

    if total == 0:
        text = "📊 <b>Статистика</b>\n\nПока нет сгенерированных карточек."
    else:
        # By rubric
        rubrics = Counter(p.get("rubric", "—").upper() for p in archive)
        rubric_lines = "\n".join(
            f"  {r}: {c}" for r, c in rubrics.most_common(15)
        )

        # By date
        dates = Counter(p.get("date", "")[:10] for p in archive if p.get("date"))
        recent_dates = sorted(dates.items(), reverse=True)[:7]
        date_lines = "\n".join(f"  {d}: {c} шт." for d, c in recent_dates)

        # Saved topics
        saved = get_all_saved_topics()

        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"<b>Всего карточек:</b> {total}\n"
            f"<b>Сохранённых тем:</b> {len(saved)}\n\n"
            f"<b>По рубрикам:</b>\n{rubric_lines}\n\n"
            f"<b>По дням (последние 7):</b>\n{date_lines}"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Меню", callback_data="back_main")]
    ])
    if edit:
        await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await target.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def _do_generate(message: Message, topic: str):
    s = get_settings(message.from_user.id)
    tone_name = s.get("tone", "Клубный")
    tone = TONE_PROFILES.get(tone_name, DEFAULT_TONE)
    model = s.get("model", DEFAULT_MODEL)
    theme = s.get("theme", "warm")

    status_msg = await message.answer(
        f"⏳ Генерирую: <i>{topic[:60]}</i>...", parse_mode=ParseMode.HTML
    )

    try:
        post = await asyncio.to_thread(generate_post, topic, tone, model, theme)
        await _send_post(message, post, user_id=message.from_user.id)
    except Exception as e:
        log.exception("Generation failed")
        await message.answer(f"❌ Ошибка генерации: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def _do_generate_from_status(status_msg: Message, user_id: int, topic: str):
    s = get_settings(user_id)
    tone_name = s.get("tone", "Клубный")
    tone = TONE_PROFILES.get(tone_name, DEFAULT_TONE)
    model = s.get("model", DEFAULT_MODEL)
    theme = s.get("theme", "warm")

    try:
        post = await asyncio.to_thread(generate_post, topic, tone, model, theme)
        await _send_post(status_msg, post, user_id=user_id)
    except Exception as e:
        log.exception("Generation failed")
        await status_msg.answer(f"❌ Ошибка генерации: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def _send_post(target: Message, post: dict, user_id: int = 0):
    # Cache post for editing
    if user_id:
        _post_cache[user_id] = post

    card_bytes = card_to_bytes(post["card_image"])
    text = format_tg_post(post)

    photo = BufferedInputFile(card_bytes, filename="card.png")
    await target.answer_photo(photo=photo)

    await target.answer(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Редактировать карточку", callback_data="edit_card"),
            ],
            [
                InlineKeyboardButton(text="🔍 Ещё темы", callback_data="categories"),
                InlineKeyboardButton(text="← Меню", callback_data="back_main"),
            ],
        ]),
    )


# ============================================================
# Bot commands menu (persistent button)
# ============================================================
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="generate", description="Сгенерировать пост по теме"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="stats", description="Статистика (админ)"),
    ]
    await bot.set_my_commands(commands)


# ============================================================
# Main
# ============================================================
async def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не задан в .env")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await set_bot_commands(bot)
    log.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
