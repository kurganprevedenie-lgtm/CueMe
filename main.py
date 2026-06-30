import asyncio
import hashlib
import logging
import re
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BusinessConnection,
    CallbackQuery, Document, Message,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import APP_NAME, BOT_TOKEN, REBUILD_THRESHOLD, SAMPLE_SIZE
from features import extract_features
from llm import (
    PROVIDER_NAMES,
    RateLimitError,
    adjust_message,
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
    compare_my_styles,
    get_forced_provider,
    make_features_summary,
    rewrite_message,
    rewrite_message_explained,
    sample_texts,
    set_forced_provider,
    suggest_reply,
)
from tg_parser import parse_chat
from storage import (
    count_biz_messages_for_contact,
    delete_all_user_data,
    delete_contact_data,
    delete_style_card,
    find_contact_by_original_id,
    get_all_per_contact_style_cards,
    get_any_user_samples,
    get_auto_mode,
    get_biz_messages_for_contact,
    get_business_connection,
    get_contact_by_id,
    get_interaction_card,
    get_imported_messages,
    get_message_samples,
    save_imported_messages,
    get_my_style_last_rebuild_count,
    get_my_style_per_contact,
    get_or_create_contact,
    get_style_card,
    init_db,
    list_contacts,
    save_business_message,
    save_interaction_card,
    save_message_samples,
    save_my_style_per_contact,
    save_style_card,
    set_auto_mode,
    update_contact_username,
    upsert_business_connection,
    upsert_chat_ref_mapping,
    upsert_user,
)

logging.basicConfig(level=logging.INFO)

dp = Dispatcher(storage=MemoryStorage())

BTN_REWRITE       = "📝 Переписать"
BTN_REPLY         = "💬 Ответить за меня"
BTN_ME            = "👤 Мой стиль"
BTN_CONTACT       = "🔍 Стиль собеседника"
BTN_MY_STYLE_FOR  = "🎯 Мой стиль с ним"
BTN_CONTACTS      = "📋 Контакты"
BTN_HELP          = "❓ Помощь"
_ALL_BTNS = {BTN_REWRITE, BTN_REPLY, BTN_ME, BTN_CONTACT, BTN_MY_STYLE_FOR, BTN_CONTACTS, BTN_HELP}

# Защита от параллельных пересборок одного контакта
_rebuilding: set[int] = set()

# Контекст последнего действия для кнопки «🔄 Ещё вариант» (по user_id)
_last_action: dict[int, dict] = {}

# После лимита Groq фоновые авто-пересборки молчат до этого момента (monotonic-время),
# чтобы не дёргать API обречёнными запросами на каждое сообщение.
_rebuild_cooldown_until: float = 0.0


def _contact_name(c) -> str:
    name     = c["display_name"] or ""
    username = c["username"] or "" if "username" in c.keys() else ""
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or c["contact_alias"]


def main_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_REWRITE), KeyboardButton(text=BTN_REPLY))
    b.row(KeyboardButton(text=BTN_ME), KeyboardButton(text=BTN_MY_STYLE_FOR))
    b.row(KeyboardButton(text=BTN_CONTACT), KeyboardButton(text=BTN_CONTACTS))
    b.row(KeyboardButton(text=BTN_HELP))
    return b.as_markup(resize_keyboard=True)


def result_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Ещё вариант", callback_data="again")
    b.button(text="✂️ Короче",     callback_data="adj:short")
    b.button(text="🔥 Теплее",     callback_data="adj:warm")
    b.button(text="👔 Формальнее", callback_data="adj:formal")
    b.adjust(1, 3)
    return b.as_markup()


async def _send_result(msg: Message, result: str, expl: str = "", rating: str = "") -> None:
    """Отправляет переписанный текст с кнопками, пояснение и оценку — отдельным сообщением."""
    await msg.answer(result, reply_markup=result_kb())
    tail = ""
    if expl:
        tail += f"💡 {expl}"
    if rating:
        tail += ("\n\n" if tail else "") + rating
    if tail:
        await msg.answer(tail)


def contacts_kb(contacts: list, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        name = _contact_name(c)
        b.button(text=name, callback_data=f"{prefix}:{c['id']}")
    b.adjust(1)
    return b.as_markup()


def demo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎬 Попробовать на примере", callback_data="demo")
    return b.as_markup()


_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA9F\U00002702-\U000027B0]+",
    re.UNICODE,
)


def _chat_ref(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def _msg_meta(text: str | None) -> dict:
    if not text:
        return {"length": 0, "has_emoji": False}
    return {"length": len(text), "has_emoji": bool(_EMOJI_RE.search(text))}


# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    waiting_for_json    = State()
    waiting_for_contact = State()

class Rewrite(StatesGroup):
    waiting_for_draft = State()

class ReplyHelp(StatesGroup):
    waiting_for_incoming = State()


# ── Выборка: biz-сообщения + fallback из JSON-семплов ─────────────────────────

def _get_rebuild_sample(
    owner_user_id: str, contact_id: int, direction: str, limit: int
) -> list[str]:
    msgs = get_biz_messages_for_contact(owner_user_id, contact_id, direction, limit)
    seen = set(msgs)
    for t in get_imported_messages(contact_id, direction):  # всё из JSON без лимита
        if t not in seen:
            msgs.append(t)
    return msgs


def _quick_stats(my_msgs: list[str], contact_msgs: list[str]) -> str:
    """Лёгкая статистика без LLM — для поля features_summary."""
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs) if my_msgs else 0
    in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs) if contact_msgs else 0
    return (
        f"Я: {len(my_msgs)} сообщ., средн. длина {out_avg:.0f} симв.\n"
        f"Собеседник: {len(contact_msgs)} сообщ., средн. длина {in_avg:.0f} симв."
    )


def _refresh_samples(owner_user_id: str, contact_id: int) -> None:
    """Освежает message_samples из текущих business + imported данных. Без LLM."""
    my_full = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    ct_full = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE)
    c     = get_contact_by_id(contact_id)
    label = _contact_name(c) if c else ""
    save_message_samples(
        contact_id, my_full[:100], ct_full[:50], _quick_stats(my_full, ct_full), contact_label=label
    )


# ── Ядро пересборки одного контакта ──────────────────────────────────────────

async def _rebuild_contact(owner_user_id: str, contact_id: int) -> bool:
    """Пересобирает my_style_per_contact и interaction_card. True если успешно."""
    my_msgs      = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    contact_msgs = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE // 2)

    if not my_msgs:
        return False

    total   = count_biz_messages_for_contact(owner_user_id, contact_id)
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs)
    in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs) if contact_msgs else 0
    stats = (
        f"Я: {total} сообщений всего, {len(my_msgs)} в выборке, средн. {out_avg:.0f} симв.\n"
        f"Собеседник: {len(contact_msgs)} в выборке, средн. {in_avg:.0f} симв."
    )

    # Обновляем message_samples объединёнными данными (JSON + business)
    contact_row = get_contact_by_id(contact_id)
    label = (contact_row["display_name"] or contact_row["contact_alias"]) if contact_row else ""
    save_message_samples(
        contact_id,
        my_msgs[:100],
        contact_msgs[:50],
        stats,
        contact_label=label,
    )

    my_style = await build_my_style_for_contact(my_msgs, stats)
    save_my_style_per_contact(contact_id, my_style, total)

    if contact_msgs:
        interaction = await build_interaction_card(my_msgs, contact_msgs, stats)
        save_interaction_card(contact_id, interaction)

    return True


# ── Авто-пересборка (fire-and-forget) ─────────────────────────────────────────

async def _maybe_rebuild(owner_user_id: str, contact_id: int) -> None:
    global _rebuild_cooldown_until
    if contact_id in _rebuilding:
        return
    if time.monotonic() < _rebuild_cooldown_until:
        return  # лимит Groq недавно исчерпан — не дёргаем API на каждое сообщение

    last = get_my_style_last_rebuild_count(contact_id)
    total = count_biz_messages_for_contact(owner_user_id, contact_id)
    if total - last < REBUILD_THRESHOLD:
        return

    _rebuilding.add(contact_id)
    try:
        logging.info("auto-rebuild start: contact_id=%s (new=%s)", contact_id, total - last)
        ok = await _rebuild_contact(owner_user_id, contact_id)
        if ok:
            per_contact = get_all_per_contact_style_cards(owner_user_id)
            if per_contact:
                overall = await build_overall_style(per_contact)
                save_style_card(owner_user_id, overall)
        logging.info("auto-rebuild done: contact_id=%s ok=%s", contact_id, ok)
    except RateLimitError:
        # Дневной лимит исчерпан — молчим 30 мин, пересоберём позже. Без трейсбека.
        _rebuild_cooldown_until = time.monotonic() + 1800
        logging.warning("auto-rebuild отложена на 30 мин (лимит Groq): contact_id=%s", contact_id)
    except Exception:
        logging.exception("auto-rebuild failed: contact_id=%s", contact_id)
    finally:
        _rebuilding.discard(contact_id)


# ── Ленивая генерация карточек ────────────────────────────────────────────────

async def _gen_style_card(telegram_id: str) -> str | None:
    """Общий агрегатный портрет. Приоритет: per-contact cards > JSON-семплы."""
    card = get_style_card(telegram_id)
    if card:
        return card

    per_contact = get_all_per_contact_style_cards(telegram_id)
    if per_contact:
        card = await build_overall_style(per_contact)
        save_style_card(telegram_id, card)
        return card

    # Fallback: старый подход через JSON-семплы
    samples = get_any_user_samples(telegram_id)
    if not samples:
        return None
    card = await build_style_card(samples["my_sample"], samples["features_summary"])
    save_style_card(telegram_id, card)
    return card


async def _gen_interaction_card(contact_id: int, owner_user_id: str = "") -> str | None:
    card = get_interaction_card(contact_id)
    if card:
        return card

    samples = get_message_samples(contact_id)
    if samples:
        my_msgs      = samples["my_sample"]
        contact_msgs = samples["contact_sample"]
        stats        = samples["features_summary"]
    elif owner_user_id:
        my_msgs      = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
        contact_msgs = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE // 2)
        if not contact_msgs:
            return None
        out_avg = sum(len(t) for t in my_msgs) / len(my_msgs) if my_msgs else 0
        in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs)
        stats   = f"Мои: {len(my_msgs)} сообщ., средн. {out_avg:.0f} симв. | Собеседника: {len(contact_msgs)} сообщ., средн. {in_avg:.0f} симв."
    else:
        return None

    card = await build_interaction_card(my_msgs, contact_msgs, stats)
    save_interaction_card(contact_id, card)
    return card


async def _gen_my_style_per_contact(contact_id: int, owner_user_id: str) -> str | None:
    card = get_my_style_per_contact(contact_id)
    if card:
        return card
    my_msgs = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    if not my_msgs:
        return None
    total   = count_biz_messages_for_contact(owner_user_id, contact_id)
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs)
    stats   = f"Я: {total} сообщений, {len(my_msgs)} в выборке, средн. {out_avg:.0f} симв."
    card    = await build_my_style_for_contact(my_msgs, stats)
    save_my_style_per_contact(contact_id, card, total)
    return card


# ── Business API ──────────────────────────────────────────────────────────────

@dp.business_connection()
async def handle_business_connection(event: BusinessConnection) -> None:
    upsert_business_connection(
        connection_id=event.id,
        owner_user_id=str(event.user.id),
        can_reply=event.can_reply,
        is_enabled=event.is_enabled,
    )
    status = "подключён" if event.is_enabled else "отключён"
    logging.info("business_connection %s: owner=%s %s", event.id, event.user.id, status)


@dp.business_message()
async def handle_business_message(event: Message) -> None:
    conn_id = event.business_connection_id
    if not conn_id:
        return

    conn_row = get_business_connection(conn_id)
    if not conn_row:
        logging.warning("business_message: unknown connection %s", conn_id)
        return

    sender_id = str(event.from_user.id) if event.from_user else None
    if not sender_id:
        return

    owner_id  = conn_row["owner_user_id"]
    direction = "out" if sender_id == owner_id else "in"
    chat_ref  = _chat_ref(event.chat.id)
    text      = event.text or event.caption or None
    date      = event.date.isoformat()

    save_business_message(
        connection_id=conn_id,
        owner_user_id=owner_id,
        chat_ref=chat_ref,
        direction=direction,
        text=text,
        date=date,
        tg_message_id=event.message_id,
        raw_meta=_msg_meta(text),
    )
    logging.info(
        "business_message saved: conn=%s chat_ref=%s direction=%s",
        conn_id, chat_ref, direction,
    )

    upsert_user(owner_id, f"user{owner_id}")

    # Для приватного чата event.chat.id всегда равен ID собеседника
    contact_tg_id = str(event.chat.id)
    if contact_tg_id == owner_id:
        # edge-case: избегаем создания контакта «сам с собой»
        return
    original_id = f"user{contact_tg_id}"

    contact_row = find_contact_by_original_id(owner_id, original_id)
    if not contact_row:
        # Контакт ещё не создан — создаём автоматически из данных чата
        parts = [event.chat.first_name or "", event.chat.last_name or ""]
        display_name = " ".join(p for p in parts if p).strip()
        new_cid = get_or_create_contact(owner_id, original_id, display_name)
        if getattr(event.chat, "username", None):
            update_contact_username(new_cid, event.chat.username)
        upsert_chat_ref_mapping(owner_id, chat_ref, new_cid)
        contact_id_for_rebuild = new_cid
        logging.info("auto-created contact: id=%s name=%s", new_cid, display_name)
    else:
        upsert_chat_ref_mapping(owner_id, chat_ref, contact_row["id"])
        contact_id_for_rebuild = contact_row["id"]
        if direction == "in" and event.from_user and event.from_user.username:
            update_contact_username(contact_row["id"], event.from_user.username)

    if contact_id_for_rebuild:
        # Освежаем message_samples при каждом сообщении (без LLM, дёшево)
        _refresh_samples(owner_id, contact_id_for_rebuild)
        asyncio.create_task(_maybe_rebuild(owner_id, contact_id_for_rebuild))


# ── /start ────────────────────────────────────────────────────────────────────

def _capabilities_text() -> str:
    return (
        "Вот что я умею:\n\n"
        "📝 Переписать — твой черновик → готовое под собеседника\n"
        "💬 Ответить за меня — подскажу ответ на его сообщение\n"
        "👤 Мой стиль · 🎯 Мой стиль с ним — как пишешь ты\n"
        "🔍 Стиль собеседника — как писать ему\n"
        "📋 Контакты · /stats — портрет в цифрах · /compare — сравнить стили\n\n"
        "Полный список команд — /help"
    )


# ── Демо-режим: готовые примеры-собеседники без загрузки данных ───────────────

_DEMO_STYLE = (
    "🎙️ Голос и тон\n"
    "• пишешь дружелюбно и по делу, без официоза\n\n"
    "✍️ Как ты строишь сообщения\n"
    "• законченные мысли средней длины\n\n"
    "🔤 Регистр и инициатива\n"
    "• с маленькой буквы, на «ты», эмодзи почти не используешь"
)

_DEMO_BOSS = (
    "🎯 Как писать этому человеку\n"
    "• коротко и по делу, без воды\n"
    "• на «Вы», вежливо и формально\n"
    "• конкретика: сроки, цифры, факты\n"
    "• без сленга и эмодзи\n\n"
    "🔤 Регистр и язык\n"
    "• Вы, с большой буквы, деловой тон"
)

_DEMO_FRIEND = (
    "🎯 Как писать этому человеку\n"
    "• неформально, на «ты», тепло\n"
    "• можно с лёгким юмором, коротко\n"
    "• сленг ок, эмодзи изредка\n\n"
    "🔤 Регистр и язык\n"
    "• ты, с маленькой буквы, расслабленно"
)


def _setup_demo(telegram_id: str) -> None:
    """Создаёт двух примеров-собеседников с готовыми карточками. Без LLM."""
    upsert_user(telegram_id, f"user{telegram_id}")
    save_style_card(telegram_id, _DEMO_STYLE)
    for orig, name, card in [
        ("demo_boss",   "Босс (демо)", _DEMO_BOSS),
        ("demo_friend", "Друг (демо)", _DEMO_FRIEND),
    ]:
        cid = get_or_create_contact(telegram_id, orig, name)
        save_interaction_card(cid, card)
        save_my_style_per_contact(cid, _DEMO_STYLE, 0)


async def _run_demo(telegram_id: str, target: Message) -> None:
    _setup_demo(telegram_id)
    await target.answer(
        "Готово! Создал двух примеров-собеседников:\n"
        "• Босс (демо) — формальный, на «Вы»\n"
        "• Друг (демо) — неформальный, на «ты»\n\n"
        "Нажми «📝 Переписать», выбери одного и напиши любой черновик "
        "(например: «напомнить про встречу в пятницу») — увидишь, как одно и то же "
        "сообщение меняется под каждого.\n\n"
        "ℹ️ В демо голос условный. На твоих данных бот будет писать твоим голосом — "
        "загрузи экспорт чата, когда захочешь.",
        reply_markup=main_kb(),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    telegram_id = str(message.from_user.id)
    caps = _capabilities_text()

    if list_contacts(telegram_id):
        await message.answer(f"С возвращением!\n\n{caps}", reply_markup=main_kb())
        return

    await state.set_state(Setup.waiting_for_json)
    await message.answer(
        f"Привет! Я {APP_NAME} — помогаю писать сообщения в твоём стиле, "
        "под конкретного человека.\n\n"
        f"{caps}\n\n"
        "Чтобы начать на своих данных — загрузи переписку: Telegram Desktop → ⋮ → "
        "Экспорт истории чата → формат JSON (без медиа), пришли файл сюда.\n\n"
        "Или попробуй прямо сейчас на примере 👇",
        reply_markup=demo_kb(),
    )


@dp.callback_query(F.data == "demo")
async def cb_demo(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _run_demo(str(call.from_user.id), call.message)


@dp.message(Command("demo"))
async def cmd_demo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _run_demo(str(message.from_user.id), message)


# ── Кнопки главного меню ──────────────────────────────────────────────────────

@dp.message(F.text.in_(_ALL_BTNS))
async def handle_menu_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text == BTN_ME:
        await _show_style(message)
    elif message.text == BTN_REWRITE:
        await _start_rewrite(message, state)
    elif message.text == BTN_REPLY:
        await _start_reply(message, state)
    elif message.text == BTN_CONTACT:
        await _show_contact_style(message)
    elif message.text == BTN_MY_STYLE_FOR:
        await _show_my_style_for(message)
    elif message.text == BTN_CONTACTS:
        await _show_contacts(message)
    elif message.text == BTN_HELP:
        await _show_help(message)


# ── Загрузка JSON-файла ───────────────────────────────────────────────────────

@dp.message(F.document)
async def handle_document(message: Message, bot: Bot, state: FSMContext) -> None:
    doc: Document = message.document
    if not doc.file_name.endswith(".json"):
        await message.answer("Нужен JSON-файл экспорта (result.json).")
        return

    telegram_id = str(message.from_user.id)
    my_id = f"user{telegram_id}"
    upsert_user(telegram_id, my_id)

    current_state = await state.get_state()
    is_setup = current_state == Setup.waiting_for_json

    await message.answer("Читаю файл...")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / doc.file_name
        await bot.download(doc, destination=path)
        try:
            chat = parse_chat(str(path), my_id)
        except Exception as e:
            await message.answer(f"Не удалось разобрать файл: {e}")
            return

    features = extract_features(chat)
    contact_id = get_or_create_contact(telegram_id, chat.meta.contact_id, chat.meta.contact_name)

    feat_full = make_features_summary(features)
    my_s      = sample_texts(chat.my_messages, 100)
    contact_s = sample_texts(chat.contact_messages, 50)
    label = chat.meta.contact_name or chat.meta.contact_id
    save_message_samples(contact_id, my_s, contact_s, feat_full, contact_label=label)

    all_imported = [
        {"direction": "out", "text": m.text, "date": m.date.isoformat()}
        for m in chat.my_messages if m.text
    ] + [
        {"direction": "in", "text": m.text, "date": m.date.isoformat()}
        for m in chat.contact_messages if m.text
    ]
    save_imported_messages(contact_id, all_imported)

    delete_style_card(telegram_id)

    name = chat.meta.contact_name

    if is_setup:
        contacts = list_contacts(telegram_id)
        if len(contacts) == 1:
            await state.clear()
            await message.answer(
                f"✓ Файл загружен — {name} ({chat.meta.total_messages} сообщений).\n\n"
                "◉ Шаг 2 из 2 — генерирую анализ, подожди ~20 секунд..."
            )
            style_card       = await _gen_style_card(telegram_id)
            interaction_card = await _gen_interaction_card(contact_id, telegram_id)
            if style_card and interaction_card:
                set_auto_mode(telegram_id, True, contact_id)
                await message.answer(
                    f"Готово! Авто-режим включён — {name}.\n"
                    "Просто пиши сообщения — буду переписывать под него.",
                    reply_markup=main_kb(),
                )
            else:
                await message.answer(
                    "Файл загружен. Используй кнопки меню для работы.",
                    reply_markup=main_kb(),
                )
        else:
            await state.set_state(Setup.waiting_for_contact)
            await message.answer(
                "✓ Файл загружен.\n\n◉ Шаг 2 из 2 — с кем хочешь работать?",
                reply_markup=contacts_kb(contacts, "setup"),
            )
    else:
        await message.answer(
            f"Загружено — {name} ({chat.meta.total_messages} сообщений).\n"
            "Нажми «🔍 Стиль собеседника» для анализа.",
            reply_markup=main_kb(),
        )


# ── Онбординг: выбор контакта (шаг 2) ────────────────────────────────────────

@dp.callback_query(F.data.startswith("setup:"))
async def cb_setup_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name = _contact_name(contact)
    await call.message.edit_text(f"Выбран — {name}. Генерирую анализ...")

    style_card       = await _gen_style_card(telegram_id)
    interaction_card = await _gen_interaction_card(contact_id, telegram_id)

    if style_card and interaction_card:
        set_auto_mode(telegram_id, True, contact_id)
        await state.clear()
        await call.message.edit_text(
            f"Готово! Авто-режим включён — {name}.\n"
            "Просто пиши сообщения — буду переписывать под него."
        )
        await call.message.answer("Готово к работе 👇", reply_markup=main_kb())
    else:
        await state.clear()
        await call.message.edit_text("Файл загружен. Используй кнопки меню.")
        await call.message.answer("Меню:", reply_markup=main_kb())


# ── /connect ─────────────────────────────────────────────────────────────────

@dp.message(Command("connect"))
async def cmd_connect(message: Message) -> None:
    await message.answer(
        "Как подключить бота к своим чатам:\n\n"
        "1. Открой Telegram → Настройки → Автоматизация чатов\n"
        "2. Найди этого бота в списке или введи его @username\n"
        "3. Выбери, к каким чатам дать доступ\n"
        "4. Подтверди подключение\n\n"
        "После этого бот начнёт получать сообщения из выбранных чатов "
        "и накапливать данные о твоём стиле общения.\n\n"
        "Имена и контакты собеседников не сохраняются — только анонимизированные паттерны."
    )


# ── /provider — переключить LLM-провайдера (для теста) ───────────────────────

@dp.message(Command("provider"))
async def cmd_provider(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    variants = " · ".join(p.lower() for p in PROVIDER_NAMES) + " · auto"
    if len(parts) < 2:
        await message.answer(
            f"Сейчас активен: {get_forced_provider()}\n\n"
            f"Переключить: /provider <{variants}>\n"
            "После выбора просто что-нибудь перепиши — в логах будет «LLM: ответил ...».\n"
            "/provider auto — вернуть обычный каскад."
        )
        return
    try:
        result = set_forced_provider(parts[1].strip())
    except ValueError as e:
        await message.answer(str(e))
        return
    if result == "auto":
        await message.answer("✅ Провайдер: авто-каскад (Groq → Gemini → OpenRouter).")
    else:
        await message.answer(
            f"✅ Принудительно выбран: {result}.\n"
            "Перепиши любое сообщение для проверки. /provider auto — вернуть каскад."
        )


# ── /me ───────────────────────────────────────────────────────────────────────

@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    await _show_style(message)


# ── Мой общий стиль (агрегат) ─────────────────────────────────────────────────

async def _show_style(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    style_card = get_style_card(telegram_id)
    if not style_card:
        await message.answer("Генерирую общий портрет — займёт ~20 секунд...")
        style_card = await _gen_style_card(telegram_id)

    if not style_card:
        await message.answer("Не удалось сгенерировать анализ.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        name = _contact_name(c)
        per_contact = get_my_style_per_contact(c["id"])
        extra = f"\n\n── Мой стиль с {name} ──\n\n{per_contact}" if per_contact else ""
        await message.answer(f"Твой общий портрет:\n\n{style_card}{extra}")
        return

    await message.answer(
        f"Твой общий портрет:\n\n{style_card}\n\n"
        "Стиль с конкретным собеседником — кнопка «🎯 Мой стиль с ним»."
    )


@dp.callback_query(F.data.startswith("style:"))
async def cb_style_contact(call: CallbackQuery) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    style_card = get_style_card(telegram_id)
    contact    = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name        = _contact_name(contact)
    interaction = get_interaction_card(contact_id) or "Нажми «🔍 Стиль собеседника» для анализа."
    await call.message.edit_text(
        f"Твой стиль:\n\n{style_card}\n\n── Как писать {name} ──\n\n{interaction}"
    )


# ── 🎯 Мой стиль с конкретным человеком ──────────────────────────────────────

async def _show_my_style_for(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        name = _contact_name(c)
        card = get_my_style_per_contact(c["id"])
        if not card:
            await message.answer(f"Генерирую мой стиль с {name} — займёт ~20 секунд...")
            card = await _gen_my_style_per_contact(c["id"], telegram_id)
        if not card:
            await message.answer(
                "Нет данных. Загрузи JSON-экспорт переписки или накопи сообщения "
                "через Автоматизацию чатов."
            )
            return
        await message.answer(f"Мой стиль с {name}:\n\n{card}")
        return

    await message.answer("С кем показать стиль?", reply_markup=contacts_kb(contacts, "mystyle"))


@dp.callback_query(F.data.startswith("mystyle:"))
async def cb_my_style_for_contact(call: CallbackQuery) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name = _contact_name(contact)

    card = get_my_style_per_contact(contact_id)
    if not card:
        await call.message.edit_text(f"Генерирую мой стиль с {name} — займёт ~20 секунд...")
        card = await _gen_my_style_per_contact(contact_id, telegram_id)

    if not card:
        await call.message.edit_text(
            "Нет данных. Загрузи JSON-экспорт переписки или накопи сообщения "
            "через Автоматизацию чатов."
        )
        return

    await call.message.edit_text(f"Мой стиль с {name}:\n\n{card}")


# ── Контакты ──────────────────────────────────────────────────────────────────

async def _show_contacts(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет загруженных чатов. Отправь JSON-файл.")
        return
    _, auto_cid = get_auto_mode(telegram_id)
    lines = []
    for c in contacts:
        mark = " 🟢" if c["id"] == auto_cid else ""
        lines.append(f"• {_contact_name(c)}{mark}")
    await message.answer(
        "Загруженные чаты (🟢 — активный для авто-переписки):\n" + "\n".join(lines)
    )


@dp.message(Command("contacts"))
async def cmd_contacts(message: Message) -> None:
    await _show_contacts(message)


# ── 🔍 Стиль собеседника ──────────────────────────────────────────────────────

async def _show_contact_style(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет загруженных чатов. Отправь JSON-файл.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        name = _contact_name(c)
        card = get_interaction_card(c["id"])
        if not card:
            await message.answer(f"Генерирую анализ {name} — займёт ~20 секунд...")
            card = await _gen_interaction_card(c["id"], telegram_id)
        if not card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        await message.answer(f"Как писать {name}:\n\n{card}")
        return

    await message.answer("Чей стиль показать?", reply_markup=contacts_kb(contacts, "cstyle"))


@dp.callback_query(F.data.startswith("cstyle:"))
async def cb_contact_style(call: CallbackQuery) -> None:
    contact_id = int(call.data.split(":")[1])
    contact    = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name = _contact_name(contact)

    card = get_interaction_card(contact_id)
    if not card:
        await call.message.edit_text(f"Генерирую анализ {name} — займёт ~20 секунд...")
        card = await _gen_interaction_card(contact_id, str(call.from_user.id))

    if not card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    await call.message.edit_text(f"Как писать {name}:\n\n{card}")


# ── Хелпер: стиль для перезаписи (per-contact → global fallback) ──────────────

async def _style_for_rewrite(telegram_id: str, contact_id: int) -> str | None:
    """Предпочитаем per-contact карточку чтобы не смешивать данные разных чатов."""
    card = get_my_style_per_contact(contact_id)
    if card:
        return card
    return await _gen_style_card(telegram_id)


# ── Переписать ────────────────────────────────────────────────────────────────

async def _start_rewrite(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card       = await _style_for_rewrite(telegram_id, c["id"])
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ — займёт ~20 секунд...")
            if not interaction_card:
                interaction_card = await _gen_interaction_card(c["id"], telegram_id)
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
        if not style_card or not interaction_card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        await state.update_data(style_card=style_card, interaction_card=interaction_card)
        await state.set_state(Rewrite.waiting_for_draft)
        name = _contact_name(c)
        await message.answer(f"Напиши черновик для {name}:")
        return

    await message.answer("Для кого пишешь?", reply_markup=contacts_kb(contacts, "rw"))


@dp.message(Command("rewrite"))
async def cmd_rewrite(message: Message, state: FSMContext) -> None:
    await _start_rewrite(message, state)


@dp.callback_query(F.data.startswith("rw:"))
async def cb_rewrite_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()

    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        await call.message.edit_text("Генерирую анализ — займёт ~20 секунд...")
        if not interaction_card:
            interaction_card = await _gen_interaction_card(contact_id, telegram_id)
        if not style_card:
            style_card = await _gen_style_card(telegram_id)

    if not style_card or not interaction_card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    await state.update_data(style_card=style_card, interaction_card=interaction_card)
    await state.set_state(Rewrite.waiting_for_draft)
    name = _contact_name(contact)
    await call.message.edit_text(f"Напиши черновик для {name}:")


@dp.message(Rewrite.waiting_for_draft)
async def handle_draft(message: Message, state: FSMContext) -> None:
    draft = message.text.strip() if message.text else ""
    if not draft:
        await message.answer("Пришли черновик сообщения.")
        return

    data = await state.get_data()
    await state.clear()
    await message.answer("Переписываю...")

    try:
        result, expl, rating = await rewrite_message_explained(
            draft, data["style_card"], data["interaction_card"]
        )
        _last_action[message.from_user.id] = {
            "kind": "rewrite", "text": draft, "result": result,
            "style_card": data["style_card"], "interaction_card": data["interaction_card"],
        }
        await _send_result(message, result, expl, rating)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ── 🔄 Ещё вариант / подстройка тона ──────────────────────────────────────────

@dp.callback_query(F.data == "again")
async def cb_again(call: CallbackQuery) -> None:
    ctx = _last_action.get(call.from_user.id)
    if not ctx:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer("Генерирую другой вариант...")
    try:
        if ctx["kind"] == "rewrite":
            result, expl, rating = await rewrite_message_explained(
                ctx["text"], ctx["style_card"], ctx["interaction_card"]
            )
        else:
            result, expl, rating = await suggest_reply(
                ctx["text"], ctx["style_card"], ctx["interaction_card"]
            )
        ctx["result"] = result
        await _send_result(call.message, result, expl, rating)
    except Exception as e:
        await call.message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("adj:"))
async def cb_adjust(call: CallbackQuery) -> None:
    ctx = _last_action.get(call.from_user.id)
    if not ctx or not ctx.get("result"):
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    mode = call.data.split(":")[1]
    await call.answer("Подстраиваю...")
    try:
        result, expl = await adjust_message(ctx["result"], ctx["style_card"], mode)
        ctx["result"] = result
        await _send_result(call.message, result, expl)
    except Exception as e:
        await call.message.answer(f"Ошибка: {e}")


# ── 💬 Ответить за меня ───────────────────────────────────────────────────────

async def _start_reply(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card       = await _style_for_rewrite(telegram_id, c["id"])
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ — займёт ~20 секунд...")
            if not interaction_card:
                interaction_card = await _gen_interaction_card(c["id"], telegram_id)
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
        if not style_card or not interaction_card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        await state.update_data(style_card=style_card, interaction_card=interaction_card)
        await state.set_state(ReplyHelp.waiting_for_incoming)
        name = _contact_name(c)
        await message.answer(
            f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
        )
        return

    await message.answer("Кому отвечаешь?", reply_markup=contacts_kb(contacts, "reply"))


@dp.callback_query(F.data.startswith("reply:"))
async def cb_reply_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()

    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        await call.message.edit_text("Генерирую анализ — займёт ~20 секунд...")
        if not interaction_card:
            interaction_card = await _gen_interaction_card(contact_id, telegram_id)
        if not style_card:
            style_card = await _gen_style_card(telegram_id)

    if not style_card or not interaction_card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    await state.update_data(style_card=style_card, interaction_card=interaction_card)
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(
        f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
    )


@dp.message(ReplyHelp.waiting_for_incoming)
async def handle_incoming(message: Message, state: FSMContext) -> None:
    incoming = message.text.strip() if message.text else ""
    if not incoming:
        await message.answer("Пришли текст сообщения собеседника.")
        return

    data = await state.get_data()
    await state.clear()
    await message.answer("Думаю как ответить...")

    try:
        result, expl, rating = await suggest_reply(
            incoming, data["style_card"], data["interaction_card"]
        )
        _last_action[message.from_user.id] = {
            "kind": "reply", "text": incoming, "result": result,
            "style_card": data["style_card"], "interaction_card": data["interaction_card"],
        }
        await _send_result(message, result, expl, rating)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("reply"))
async def cmd_reply(message: Message, state: FSMContext) -> None:
    await _start_reply(message, state)


# ── /rebuild_all — принудительная пересборка всего (для теста) ───────────────

@dp.message(Command("rebuild_all"))
async def cmd_rebuild_all(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет контактов для пересборки.")
        return

    n = len(contacts)
    names = [_contact_name(c) for c in contacts]

    def _progress(done: int, current: str = "", mark: str = "⏳") -> str:
        lines = [f"Пересборка {done}/{n}\n"]
        for i, nm in enumerate(names):
            if i < done:
                lines.append(f"✅ {nm}")
            elif nm == current:
                lines.append(f"{mark} {nm} — обрабатываю...")
            else:
                lines.append(f"⬜ {nm}")
        return "\n".join(lines)

    status = await message.answer(_progress(0, names[0]))

    rebuilt = 0
    for i, c in enumerate(contacts):
        try:
            await status.edit_text(_progress(rebuilt, names[i]))
            ok = await _rebuild_contact(telegram_id, c["id"])
            if ok:
                rebuilt += 1
        except RateLimitError as e:
            await status.edit_text(_progress(rebuilt) + f"\n\n⛔ Дальше упёрлись в лимит.\n{e}")
            return
        except Exception:
            logging.exception("rebuild_all failed for contact_id=%s", c["id"])

    await status.edit_text(_progress(rebuilt))

    per_contact = get_all_per_contact_style_cards(telegram_id)
    if not per_contact:
        await message.answer(
            f"Пересобрано: {rebuilt}/{n}. "
            "Пока нет данных для общего портрета (нужны сообщения в обе стороны)."
        )
        return

    try:
        await message.answer("Собираю общий портрет...")
        overall = await build_overall_style(per_contact)
        save_style_card(telegram_id, overall)
        await message.answer(f"✅ Готово. Пересобрано: {rebuilt}/{n}. Общий портрет обновлён.")
    except RateLimitError as e:
        await message.answer(
            f"Контакты пересобраны ({rebuilt}/{n}), но общий портрет не успел — {e}"
        )


# ── /help ────────────────────────────────────────────────────────────────────

async def _show_help(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n\n"
        "/start — начало работы / онбординг\n"
        "/help — список команд\n"
        "/demo — попробовать на готовом примере\n"
        "/connect — как подключить Автоматизацию чатов\n"
        "/me — твой общий стиль общения\n"
        "/stats — твой портрет в цифрах\n"
        "/compare — как ты пишешь разным людям (сравнение)\n"
        "/rewrite — переписать сообщение\n"
        "/reply — помочь ответить на сообщение собеседника\n"
        "/contacts — список загруженных чатов\n"
        "/delete — удалить свои данные\n"
        "/rebuild_all — принудительно пересобрать все карточки\n\n"
        "Кнопки в меню:\n"
        "📝 Переписать — черновик → готовое сообщение\n"
        "💬 Ответить за меня — подскажу ответ на сообщение собеседника\n"
        "👤 Мой стиль — как ты общаешься в целом\n"
        "🎯 Мой стиль с ним — твой стиль с конкретным человеком\n"
        "🔍 Стиль собеседника — паттерны и советы по нему\n"
        "📋 Контакты — загруженные переписки\n"
        "❓ Помощь — это сообщение\n\n"
        "Любое сообщение, которое ты просто напишешь боту, автоматически "
        "переписывается под активного собеседника (🟢 в /contacts)."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show_help(message)


# ── /stats — портрет в цифрах (без LLM) ──────────────────────────────────────

def _all_my_messages(telegram_id: str, contact_id: int) -> list[str]:
    biz      = get_biz_messages_for_contact(telegram_id, contact_id, "out", 100000)
    imported = get_imported_messages(contact_id, "out")
    seen, out = set(), []
    for t in biz + imported:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _compute_stats(telegram_id: str) -> str | None:
    contacts = list_contacts(telegram_id)
    if not contacts:
        return None

    per, all_my = [], []
    for c in contacts:
        my = _all_my_messages(telegram_id, c["id"])
        if not my:
            continue
        all_my += my
        per.append({
            "name": _contact_name(c),
            "n":    len(my),
            "avg":  sum(len(t) for t in my) / len(my),
            "q":    sum(1 for t in my if t.rstrip().endswith("?")) / len(my),
            "em":   sum(1 for t in my if _EMOJI_RE.search(t)) / len(my),
        })

    if not all_my:
        return None

    total = len(all_my)
    g_avg = sum(len(t) for t in all_my) / total
    g_q   = sum(1 for t in all_my if t.rstrip().endswith("?")) / total
    g_em  = sum(1 for t in all_my if _EMOJI_RE.search(t)) / total

    lines = [
        "📊 Твой портрет в цифрах\n",
        f"Всего твоих сообщений: {total}",
        f"Средняя длина: {g_avg:.0f} символов",
        f"Доля вопросов: {g_q:.0%}",
        f"Эмодзи в сообщениях: {g_em:.0%}",
    ]

    if len(per) > 1:
        longest  = max(per, key=lambda x: x["avg"])
        shortest = min(per, key=lambda x: x["avg"])
        most_em  = max(per, key=lambda x: x["em"])
        lines += [
            "",
            f"Длиннее всего пишешь — {longest['name']} ({longest['avg']:.0f} симв.)",
            f"Короче всего — {shortest['name']} ({shortest['avg']:.0f} симв.)",
            f"Больше всего эмодзи — {most_em['name']} ({most_em['em']:.0%})",
        ]

    lines.append("\nПо собеседникам:")
    for p in sorted(per, key=lambda x: -x["n"]):
        lines.append(
            f"• {p['name']}: {p['n']} сообщ., {p['avg']:.0f} симв., "
            f"вопросы {p['q']:.0%}, эмодзи {p['em']:.0%}"
        )

    return "\n".join(lines)


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    stats = _compute_stats(str(message.from_user.id))
    if not stats:
        await message.answer("Пока нет данных. Загрузи JSON-чат или накопи сообщения.")
        return
    await message.answer(stats)


# ── /compare — сравнение стиля с разными людьми ──────────────────────────────

@dp.message(Command("compare"))
async def cmd_compare(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    cards = get_all_per_contact_style_cards(telegram_id)
    if len(cards) < 2:
        await message.answer(
            "Нужно минимум 2 разобранных собеседника. "
            "Загрузи ещё чат или дай боту накопить сообщения."
        )
        return
    await message.answer("Сравниваю как ты пишешь разным людям — ~20 секунд...")
    try:
        result = await compare_my_styles(cards)
        await message.answer(result)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ── /delete — удалить данные (152-ФЗ) ────────────────────────────────────────

def _delete_kb(contacts: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        b.button(text=f"🗑 {_contact_name(c)}", callback_data=f"del:{c['id']}")
    b.button(text="‼️ Удалить ВСЕ данные", callback_data="delall")
    b.adjust(1)
    return b.as_markup()


@dp.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("У тебя нет сохранённых данных.")
        return
    await message.answer(
        "Что удалить? Действие необратимо.",
        reply_markup=_delete_kb(contacts),
    )


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_contact(call: CallbackQuery) -> None:
    contact_id = int(call.data.split(":")[1])
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return
    await call.answer()
    name = _contact_name(contact)
    b = InlineKeyboardBuilder()
    b.button(text=f"Да, удалить {name}", callback_data=f"delyes:{contact_id}")
    b.button(text="Отмена", callback_data="delno")
    b.adjust(1)
    await call.message.edit_text(
        f"Удалить все данные по «{name}»? Это необратимо.",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("delyes:"))
async def cb_delete_contact_confirm(call: CallbackQuery) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    contact = get_contact_by_id(contact_id)
    name = _contact_name(contact) if contact else "контакт"
    delete_contact_data(telegram_id, contact_id)
    await call.answer("Удалено")
    await call.message.edit_text(f"✓ Данные по «{name}» удалены.")


@dp.callback_query(F.data == "delall")
async def cb_delete_all(call: CallbackQuery) -> None:
    await call.answer()
    b = InlineKeyboardBuilder()
    b.button(text="Да, удалить ВСЁ", callback_data="delallyes")
    b.button(text="Отмена", callback_data="delno")
    b.adjust(1)
    await call.message.edit_text(
        "Удалить ВСЕ твои данные — все чаты, стили, переписки? Это необратимо.",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data == "delallyes")
async def cb_delete_all_confirm(call: CallbackQuery) -> None:
    delete_all_user_data(str(call.from_user.id))
    await call.answer("Удалено")
    await call.message.edit_text(
        "✓ Все твои данные удалены. Чтобы начать заново — /start."
    )


@dp.callback_query(F.data == "delno")
async def cb_delete_cancel(call: CallbackQuery) -> None:
    await call.answer("Отменено")
    await call.message.edit_text("Удаление отменено.")


# ── Авто-переписка (catch-all, должен быть последним) ─────────────────────────

@dp.message(F.text & ~F.text.in_(_ALL_BTNS))
async def auto_rewrite_handler(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return

    telegram_id = str(message.from_user.id)
    # Авто-режим всегда активен. Цель — выбранный контакт, иначе единственный.
    _, contact_id = get_auto_mode(telegram_id)
    if not contact_id:
        contacts = list_contacts(telegram_id)
        if len(contacts) == 1:
            contact_id = contacts[0]["id"]
        else:
            return  # несколько контактов и не выбран целевой — не угадываем

    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        return

    try:
        draft = message.text.strip()
        result = await rewrite_message(draft, style_card, interaction_card)
        _last_action[message.from_user.id] = {
            "kind": "rewrite", "text": draft, "result": result,
            "style_card": style_card, "interaction_card": interaction_card,
        }
        await message.answer(result, reply_markup=result_kb())
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ── запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start",       description="Начало работы"),
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="demo",        description="Попробовать на примере"),
        BotCommand(command="connect",     description="Подключить Автоматизацию чатов"),
        BotCommand(command="me",          description="Мой стиль общения"),
        BotCommand(command="stats",       description="Портрет в цифрах"),
        BotCommand(command="compare",     description="Сравнить стиль с разными людьми"),
        BotCommand(command="rewrite",     description="Переписать сообщение"),
        BotCommand(command="reply",       description="Помочь ответить собеседнику"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
        BotCommand(command="delete",      description="Удалить свои данные"),
        BotCommand(command="rebuild_all", description="Пересобрать все карточки"),
    ])
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
