"""Telegram bot for Закрытый клуб Павла Сидоренко."""

import asyncio
import json
import logging
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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
)
from generator import (
    generate_post,
    search_news,
    format_tg_post,
    card_to_bytes,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

router = Router()

# --- Per-user settings (in-memory + file persistence) ---
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
        "tone": "Клубный",
        "theme": "warm",
        "model": DEFAULT_MODEL,
    })


def set_settings(user_id: int, settings: dict):
    all_s = _load_all_settings()
    all_s[str(user_id)] = settings
    _save_all_settings(all_s)


# --- Access control ---
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # no whitelist = open
    return user_id in ALLOWED_USERS


# --- FSM States ---
class Gen(StatesGroup):
    waiting_topic = State()


# --- Keyboards ---
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Найти темы", callback_data="news"),
            InlineKeyboardButton(text="✏️ Своя тема", callback_data="custom_topic"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ],
    ])


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
        [InlineKeyboardButton(text="← Назад", callback_data="back_main")],
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


def news_topics_kb(topics: list) -> InlineKeyboardMarkup:
    buttons = []
    for i, t in enumerate(topics[:7]):
        topic_text = t.get("topic", "")[:45]
        rubric = t.get("rubric", "")
        buttons.append([InlineKeyboardButton(
            text=f"{rubric} · {topic_text}",
            callback_data=f"pick:{i}",
        )])
    buttons.append([InlineKeyboardButton(text="🔄 Ещё темы", callback_data="news")])
    buttons.append([InlineKeyboardButton(text="← Меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Handlers ---

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
        reply_markup=main_menu_kb(),
    )


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    if not is_allowed(message.from_user.id):
        return
    topic = message.text.replace("/generate", "").strip()
    if not topic:
        await message.answer("Укажи тему: <code>/generate Wes Montgomery и октавы</code>", parse_mode=ParseMode.HTML)
        return
    await _do_generate(message, topic)


# --- Callbacks ---

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🎸 <b>Закрытый клуб Павла Сидоренко</b>\n\nВыбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "custom_topic")
async def cb_custom_topic(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Gen.waiting_topic)
    await cb.message.edit_text(
        "✏️ Напиши тему для поста.\n\n"
        "Примеры:\n"
        "• <i>Какие струны использует John Mayer</i>\n"
        "• <i>Ibanez Tube Screamer — почему все его копируют</i>\n"
        "• <i>Celestion Greenback vs Vintage 30</i>",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.message(Gen.waiting_topic)
async def on_topic_text(message: Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        return
    await state.clear()
    await _do_generate(message, message.text.strip())


@router.callback_query(F.data == "news")
async def cb_news(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔ Нет доступа")
        return
    await cb.message.edit_text("🔍 Ищу интересные темы...", parse_mode=ParseMode.HTML)
    await cb.answer()

    try:
        s = get_settings(cb.from_user.id)
        topics = await asyncio.to_thread(search_news, "", s.get("model", DEFAULT_MODEL))
        if not topics:
            await cb.message.edit_text("Не удалось найти темы. Попробуй ещё раз.",
                                       reply_markup=main_menu_kb())
            return

        # Store topics for picking
        _topic_cache[cb.from_user.id] = topics

        lines = []
        for i, t in enumerate(topics):
            hook = t.get("hook", "")
            lines.append(f"{i+1}. <b>{t.get('rubric', '')}</b> · {t.get('topic', '')}")
            if hook:
                lines.append(f"   <i>{hook}</i>")

        text = "🔍 <b>Найденные темы:</b>\n\n" + "\n".join(lines) + "\n\nВыбери тему:"
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=news_topics_kb(topics))
    except Exception as e:
        log.exception("News search failed")
        await cb.message.edit_text(f"❌ Ошибка поиска: {e}", reply_markup=main_menu_kb())


# Topic cache (user_id -> list of topics)
_topic_cache: dict[int, list] = {}


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
    await cb.message.edit_text(f"⏳ Генерирую пост: <i>{topic}</i>...", parse_mode=ParseMode.HTML)
    await _do_generate_from_cb(cb, topic)


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
    await cb.message.edit_text(f"✅ Тон: <b>{name}</b>", parse_mode=ParseMode.HTML,
                                reply_markup=settings_kb(s))
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
    await cb.message.edit_text(f"✅ Карточка: <b>{label}</b>", parse_mode=ParseMode.HTML,
                                reply_markup=settings_kb(s))
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
    await cb.message.edit_text(f"✅ Модель: <b>{short}</b>", parse_mode=ParseMode.HTML,
                                reply_markup=settings_kb(s))
    await cb.answer()


# --- Generation helpers ---

async def _do_generate(message: Message, topic: str):
    """Generate and send post from a regular message."""
    s = get_settings(message.from_user.id)
    tone_name = s.get("tone", "Клубный")
    tone = TONE_PROFILES.get(tone_name, DEFAULT_TONE)
    model = s.get("model", DEFAULT_MODEL)
    theme = s.get("theme", "warm")

    status_msg = await message.answer(f"⏳ Генерирую: <i>{topic[:60]}</i>...", parse_mode=ParseMode.HTML)

    try:
        post = await asyncio.to_thread(generate_post, topic, tone, model, theme)
        await _send_post(message, post)
    except Exception as e:
        log.exception("Generation failed")
        await message.answer(f"❌ Ошибка генерации: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def _do_generate_from_cb(cb: CallbackQuery, topic: str):
    """Generate and send post from a callback query."""
    s = get_settings(cb.from_user.id)
    tone_name = s.get("tone", "Клубный")
    tone = TONE_PROFILES.get(tone_name, DEFAULT_TONE)
    model = s.get("model", DEFAULT_MODEL)
    theme = s.get("theme", "warm")

    try:
        post = await asyncio.to_thread(generate_post, topic, tone, model, theme)
        # Delete the "generating..." message
        try:
            await cb.message.delete()
        except Exception:
            pass
        await _send_post(cb.message, post)
    except Exception as e:
        log.exception("Generation failed")
        await cb.message.edit_text(f"❌ Ошибка: {e}", reply_markup=main_menu_kb())


async def _send_post(target: Message, post: dict):
    """Send card image + formatted post text."""
    card_bytes = card_to_bytes(post["card_image"])
    text = format_tg_post(post)

    # Send card image
    photo = BufferedInputFile(card_bytes, filename="card.png")
    await target.answer_photo(photo=photo)

    # Send formatted text
    await target.answer(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Ещё темы", callback_data="news"),
                InlineKeyboardButton(text="← Меню", callback_data="back_main"),
            ]
        ]),
    )


# --- Main ---
async def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не задан в .env")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
