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
    BusinessConnection,
    CallbackQuery, Document, Message,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import APP_NAME, BOT_TOKEN
from features import extract_features
from llm import (
    build_interaction_card,
    build_style_card,
    make_features_summary,
    make_user_features_summary,
    rewrite_message,
    sample_texts,
)
from tg_parser import parse_chat
from storage import (
    delete_style_card,
    get_any_user_samples,
    get_auto_mode,
    get_business_connection,
    get_contact_by_id,
    get_interaction_card,
    get_message_samples,
    get_or_create_contact,
    get_style_card,
    init_db,
    list_contacts,
    save_business_message,
    save_interaction_card,
    save_message_samples,
    save_style_card,
    set_auto_mode,
    upsert_business_connection,
    upsert_user,
)

logging.basicConfig(level=logging.INFO)

dp = Dispatcher(storage=MemoryStorage())

BTN_REWRITE   = "📝 Переписать"
BTN_ME        = "👤 Мой стиль"
BTN_CONTACT   = "🔍 Стиль собеседника"
BTN_AUTO      = "🔄 Авто-режим"
BTN_CONTACTS  = "📋 Контакты"
_ALL_BTNS     = {BTN_REWRITE, BTN_ME, BTN_CONTACT, BTN_AUTO, BTN_CONTACTS}


def main_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_REWRITE), KeyboardButton(text=BTN_ME))
    b.row(KeyboardButton(text=BTN_CONTACT), KeyboardButton(text=BTN_AUTO))
    b.row(KeyboardButton(text=BTN_CONTACTS))
    return b.as_markup(resize_keyboard=True)


def contacts_kb(contacts: list, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        name = c["display_name"] or c["contact_alias"]
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


# ── Ленивая генерация карточек ────────────────────────────────────────────────

async def _gen_style_card(telegram_id: str) -> str | None:
    card = get_style_card(telegram_id)
    if card:
        return card
    samples = get_any_user_samples(telegram_id)
    if not samples:
        return None
    card = await build_style_card(samples["my_sample"], samples["user_features_summary"])
    save_style_card(telegram_id, card)
    return card


async def _gen_interaction_card(contact_id: int) -> str | None:
    card = get_interaction_card(contact_id)
    if card:
        return card
    samples = get_message_samples(contact_id)
    if not samples:
        return None
    card = await build_interaction_card(
        samples["my_sample"], samples["contact_sample"], samples["features_summary"]
    )
    save_interaction_card(contact_id, card)
    return card


# ── Business API ─────────────────────────────────────────────────────────────

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
    elif message.text == BTN_CONTACTS:
        await _show_contacts(message)
    elif message.text == BTN_AUTO:
        await _toggle_auto(message)


# ── Загрузка JSON-файла (без LLM) ────────────────────────────────────────────

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

    # Сохраняем выборку — LLM вызовем позже, по требованию
    my_s       = sample_texts(chat.my_messages, 100)
    contact_s  = sample_texts(chat.contact_messages, 50)
    feat_full  = make_features_summary(features)
    feat_user  = make_user_features_summary(features)
    save_message_samples(contact_id, my_s, contact_s, feat_full, feat_user)

    # При новом чате — сбрасываем style_card чтобы пересчитать из актуальных данных
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
            style_card    = await _gen_style_card(telegram_id)
            interaction_card = await _gen_interaction_card(contact_id)
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
                f"✓ Файл загружен.\n\n"
                "◉ Шаг 2 из 2 — с кем хочешь работать?",
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
    contact_id = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name = contact["display_name"] or contact["contact_alias"]
    await call.message.edit_text(f"Выбран — {name}. Генерирую анализ...")

    style_card       = await _gen_style_card(telegram_id)
    interaction_card = await _gen_interaction_card(contact_id)

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
        "   (можно: все, кроме контактов, только новые и т.д.)\n"
        "4. Подтверди подключение\n\n"
        "После этого бот начнёт получать сообщения из выбранных чатов "
        "и накапливать данные о твоём стиле общения.\n\n"
        "Данные хранятся только на нашем сервере. "
        "Имена и контакты собеседников не сохраняются — только анонимизированные паттерны."
    )


# ── /me ───────────────────────────────────────────────────────────────────────

@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    await _show_style(message)


# ── Мой стиль ─────────────────────────────────────────────────────────────────

async def _show_style(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    style_card = get_style_card(telegram_id)
    if not style_card:
        await message.answer("Генерирую анализ твоего стиля — займёт ~20 секунд...")
        style_card = await _gen_style_card(telegram_id)

    if not style_card:
        await message.answer("Не удалось сгенерировать анализ.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        name = c["display_name"] or c["contact_alias"]
        interaction = get_interaction_card(c["id"]) or "Нажми «🔍 Стиль собеседника» для анализа."
        await message.answer(
            f"Твой стиль общения:\n\n{style_card}\n\n"
            f"── Как писать {name} ──\n\n{interaction}"
        )
        return

    await message.answer(
        f"Твой стиль общения:\n\n{style_card}\n\n"
        "Выбери контакт чтобы увидеть советы:",
        reply_markup=contacts_kb(contacts, "style"),
    )


@dp.callback_query(F.data.startswith("style:"))
async def cb_style_contact(call: CallbackQuery) -> None:
    contact_id = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    style_card = get_style_card(telegram_id)
    contact    = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name        = contact["display_name"] or contact["contact_alias"]
    interaction = get_interaction_card(contact_id) or "Нажми «🔍 Стиль собеседника» для анализа."
    await call.message.edit_text(
        f"Твой стиль общения:\n\n{style_card}\n\n"
        f"── Как писать {name} ──\n\n{interaction}"
    )


# ── Контакты ──────────────────────────────────────────────────────────────────

async def _show_contacts(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет загруженных чатов. Отправь JSON-файл.")
        return
    auto_on, _ = get_auto_mode(telegram_id)
    status = "🟢 Авто-режим включён" if auto_on else "⚫ Авто-режим выключен"
    lines = "\n".join(f"• {c['display_name'] or c['contact_alias']}" for c in contacts)
    await message.answer(f"{status}\n\nЗагруженные чаты:\n{lines}")


@dp.message(Command("contacts"))
async def cmd_contacts(message: Message) -> None:
    await _show_contacts(message)


# ── Стиль собеседника ──────────────────────────────────────────────────────────

async def _show_contact_style(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет загруженных чатов. Отправь JSON-файл.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        name = c["display_name"] or c["contact_alias"]
        card = get_interaction_card(c["id"])
        if not card:
            await message.answer(f"Генерирую анализ {name} — займёт ~20 секунд...")
            card = await _gen_interaction_card(c["id"])
        if not card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        await message.answer(f"Как писать {name}:\n\n{card}")
        return

    await message.answer("Чей стиль показать?", reply_markup=contacts_kb(contacts, "cstyle"))


@dp.callback_query(F.data.startswith("cstyle:"))
async def cb_contact_style(call: CallbackQuery) -> None:
    contact_id = int(call.data.split(":")[1])
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    name = contact["display_name"] or contact["contact_alias"]

    card = get_interaction_card(contact_id)
    if not card:
        await call.message.edit_text(f"Генерирую анализ {name} — займёт ~20 секунд...")
        card = await _gen_interaction_card(contact_id)

    if not card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    await call.message.edit_text(f"Как писать {name}:\n\n{card}")


# ── Переписать ────────────────────────────────────────────────────────────────

async def _start_rewrite(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)

    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card    = get_style_card(telegram_id)
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ — займёт ~20 секунд...")
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
            if not interaction_card:
                interaction_card = await _gen_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        await state.update_data(style_card=style_card, interaction_card=interaction_card)
        await state.set_state(Rewrite.waiting_for_draft)
        name = c["display_name"] or c["contact_alias"]
        await message.answer(f"Напиши черновик для {name}:")
        return

    await message.answer("Для кого пишешь?", reply_markup=contacts_kb(contacts, "rw"))


@dp.message(Command("rewrite"))
async def cmd_rewrite(message: Message, state: FSMContext) -> None:
    await _start_rewrite(message, state)


@dp.callback_query(F.data.startswith("rw:"))
async def cb_rewrite_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()

    style_card    = get_style_card(telegram_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        await call.message.edit_text("Генерирую анализ — займёт ~20 секунд...")
        if not style_card:
            style_card = await _gen_style_card(telegram_id)
        if not interaction_card:
            interaction_card = await _gen_interaction_card(contact_id)

    if not style_card or not interaction_card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    await state.update_data(style_card=style_card, interaction_card=interaction_card)
    await state.set_state(Rewrite.waiting_for_draft)
    name = contact["display_name"] or contact["contact_alias"]
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
        style_card    = get_style_card(telegram_id)
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ...")
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
            if not interaction_card:
                interaction_card = await _gen_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Не удалось сгенерировать анализ.")
            return
        set_auto_mode(telegram_id, True, c["id"])
        name = c["display_name"] or c["contact_alias"]
        await message.answer(
            f"🟢 Авто-режим включён — {name}.\n"
            "Каждое твоё сообщение будет автоматически переписываться."
        )
        return

    await message.answer("Для кого включить авто-режим?", reply_markup=contacts_kb(contacts, "auto"))


@dp.callback_query(F.data.startswith("auto:"))
async def cb_auto_contact(call: CallbackQuery) -> None:
    contact_id = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    await call.message.edit_text("Генерирую анализ...")

    style_card    = get_style_card(telegram_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card:
        style_card = await _gen_style_card(telegram_id)
    if not interaction_card:
        interaction_card = await _gen_interaction_card(contact_id)

    if not style_card or not interaction_card:
        await call.message.edit_text("Не удалось сгенерировать анализ.")
        return

    set_auto_mode(telegram_id, True, contact_id)
    name = contact["display_name"] or contact["contact_alias"]
    await call.message.edit_text(
        f"🟢 Авто-режим включён — {name}.\n"
        "Каждое твоё сообщение будет автоматически переписываться."
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

    style_card = get_style_card(telegram_id)
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
