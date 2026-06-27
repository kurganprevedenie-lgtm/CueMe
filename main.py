import asyncio
import hashlib
import logging
import re
import tempfile
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
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
    make_features_summary,
    make_user_features_summary,
    rewrite_message,
    sample_texts,
)
from tg_parser import parse_chat
from storage import (
    count_biz_messages_for_contact,
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
BTN_ME            = "👤 Мой стиль"
BTN_CONTACT       = "🔍 Стиль собеседника"
BTN_AUTO          = "🔄 Авто-режим"
BTN_MY_STYLE_FOR  = "🎯 Мой стиль с ним"
BTN_CONTACTS      = "📋 Контакты"
_ALL_BTNS = {BTN_REWRITE, BTN_ME, BTN_CONTACT, BTN_AUTO, BTN_MY_STYLE_FOR, BTN_CONTACTS}

# Защита от параллельных пересборок одного контакта
_rebuilding: set[int] = set()


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
    b.row(KeyboardButton(text=BTN_REWRITE), KeyboardButton(text=BTN_ME))
    b.row(KeyboardButton(text=BTN_CONTACT), KeyboardButton(text=BTN_AUTO))
    b.row(KeyboardButton(text=BTN_MY_STYLE_FOR), KeyboardButton(text=BTN_CONTACTS))
    return b.as_markup(resize_keyboard=True)


def contacts_kb(contacts: list, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        name = _contact_name(c)
        b.button(text=name, callback_data=f"{prefix}:{c['id']}")
    b.adjust(1)
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


def _refresh_samples(owner_user_id: str, contact_id: int) -> None:
    """Освежает message_samples из текущих business + imported данных. Без LLM."""
    my_s  = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)[:100]
    ct_s  = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE)[:50]
    c     = get_contact_by_id(contact_id)
    label = _contact_name(c) if c else ""
    save_message_samples(contact_id, my_s, ct_s, "", "", contact_label=label)


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
    if contact_id in _rebuilding:
        return

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
    card = await build_style_card(samples["my_sample"], samples["user_features_summary"])
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

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if list_contacts(str(message.from_user.id)):
        await message.answer("С возвращением! Чем могу помочь?", reply_markup=main_kb())
        return

    await state.set_state(Setup.waiting_for_json)
    await message.answer(
        f"Привет! Я {APP_NAME}.\n\n"
        "Помогу писать сообщения в твоём стиле — под конкретного человека.\n\n"
        "◉ Шаг 1 из 2 — загрузи переписку\n\n"
        "Открой нужный чат в Telegram Desktop → ⋮ → Экспорт истории чата → "
        "формат JSON, без медиафайлов. Отправь файл сюда 👇",
    )


# ── Кнопки главного меню ──────────────────────────────────────────────────────

@dp.message(F.text.in_(_ALL_BTNS))
async def handle_menu_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text == BTN_ME:
        await _show_style(message)
    elif message.text == BTN_REWRITE:
        await _start_rewrite(message, state)
    elif message.text == BTN_CONTACT:
        await _show_contact_style(message)
    elif message.text == BTN_MY_STYLE_FOR:
        await _show_my_style_for(message)
    elif message.text == BTN_CONTACTS:
        await _show_contacts(message)
    elif message.text == BTN_AUTO:
        await _toggle_auto(message)


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
    feat_user = make_user_features_summary(features)
    my_s      = sample_texts(chat.my_messages, 100)
    contact_s = sample_texts(chat.contact_messages, 50)
    label = chat.meta.contact_name or chat.meta.contact_id
    save_message_samples(contact_id, my_s, contact_s, feat_full, feat_user, contact_label=label)

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
    auto_on, _ = get_auto_mode(telegram_id)
    status = "🟢 Авто-режим включён" if auto_on else "⚫ Авто-режим выключен"
    lines  = "\n".join(f"• {_contact_name(c)}" for c in contacts)
    await message.answer(f"{status}\n\nЗагруженные чаты:\n{lines}")


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
    await message.answer("Переписываю...")

    try:
        result = await rewrite_message(draft, data["style_card"], data["interaction_card"])
        await message.answer(result)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    finally:
        await state.clear()


# ── Авто-режим ────────────────────────────────────────────────────────────────

async def _toggle_auto(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    auto_on, _ = get_auto_mode(telegram_id)
    if auto_on:
        set_auto_mode(telegram_id, False)
        await message.answer("⚫ Авто-режим выключен.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card       = get_style_card(telegram_id)
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ...")
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
            if not interaction_card:
                interaction_card = await _gen_interaction_card(c["id"], telegram_id)
        if not style_card or not interaction_card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        set_auto_mode(telegram_id, True, c["id"])
        name = _contact_name(c)
        await message.answer(
            f"🟢 Авто-режим включён — {name}.\n"
            "Каждое твоё сообщение будет автоматически переписываться."
        )
        return

    await message.answer("Для кого включить авто-режим?", reply_markup=contacts_kb(contacts, "auto"))


@dp.callback_query(F.data.startswith("auto:"))
async def cb_auto_contact(call: CallbackQuery) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    await call.message.edit_text("Генерирую анализ...")

    style_card       = get_style_card(telegram_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card:
        style_card = await _gen_style_card(telegram_id)
    if not interaction_card:
        interaction_card = await _gen_interaction_card(contact_id, telegram_id)

    if not style_card or not interaction_card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    set_auto_mode(telegram_id, True, contact_id)
    name = _contact_name(contact)
    await call.message.edit_text(
        f"🟢 Авто-режим включён — {name}.\n"
        "Каждое твоё сообщение будет автоматически переписываться."
    )


# ── /rebuild_all — принудительная пересборка всего (для теста) ───────────────

@dp.message(Command("rebuild_all"))
async def cmd_rebuild_all(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет контактов для пересборки.")
        return

    n = len(contacts)
    await message.answer(
        f"Запускаю пересборку для {n} контактов — ~{n * 20} секунд..."
    )

    rebuilt = 0
    for c in contacts:
        try:
            ok = await _rebuild_contact(telegram_id, c["id"])
            if ok:
                rebuilt += 1
        except Exception:
            logging.exception("rebuild_all failed for contact_id=%s", c["id"])

    per_contact = get_all_per_contact_style_cards(telegram_id)
    if per_contact:
        overall = await build_overall_style(per_contact)
        save_style_card(telegram_id, overall)
        await message.answer(
            f"Готово. Пересобрано: {rebuilt}/{n}. Общий портрет обновлён."
        )
    else:
        await message.answer(
            f"Пересобрано: {rebuilt}/{n}. "
            "Пока нет данных для общего портрета (нужны сообщения в обе стороны)."
        )


# ── /help ────────────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n\n"
        "/start — начало работы / онбординг\n"
        "/help — список команд\n"
        "/connect — как подключить Автоматизацию чатов\n"
        "/me — твой общий стиль общения\n"
        "/rewrite — переписать сообщение\n"
        "/contacts — список загруженных чатов\n"
        "/rebuild_all — принудительно пересобрать все карточки\n\n"
        "Кнопки в меню:\n"
        "📝 Переписать — черновик → готовое сообщение\n"
        "👤 Мой стиль — как ты общаешься в целом\n"
        "🎯 Мой стиль с ним — твой стиль с конкретным человеком\n"
        "🔍 Стиль собеседника — паттерны и советы по нему\n"
        "🔄 Авто-режим — каждое сообщение переписывается автоматически\n"
        "📋 Контакты — загруженные переписки"
    )


# ── Авто-переписка (catch-all, должен быть последним) ─────────────────────────

@dp.message(F.text & ~F.text.in_(_ALL_BTNS))
async def auto_rewrite_handler(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return

    telegram_id = str(message.from_user.id)
    auto_on, contact_id = get_auto_mode(telegram_id)
    if not auto_on or not contact_id:
        return

    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        return

    try:
        result = await rewrite_message(message.text.strip(), style_card, interaction_card)
        await message.answer(result)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ── запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start",       description="Начало работы"),
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="connect",     description="Подключить Автоматизацию чатов"),
        BotCommand(command="me",          description="Мой стиль общения"),
        BotCommand(command="rewrite",     description="Переписать сообщение"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
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
