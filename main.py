import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Document, Message

from config import APP_NAME, BOT_TOKEN
from features import extract_features
from llm import build_cards, rewrite_message
from parser import parse_chat
from storage import (
    get_interaction_card,
    get_or_create_contact,
    get_style_card,
    get_user,
    init_db,
    list_contacts,
    save_interaction_card,
    save_style_card,
    upsert_user,
)

logging.basicConfig(level=logging.INFO)

dp = Dispatcher(storage=MemoryStorage())


# ── FSM-состояния ─────────────────────────────────────────────────────────────

class Registration(StatesGroup):
    waiting_for_my_id = State()


class Rewrite(StatesGroup):
    waiting_for_contact = State()
    waiting_for_draft = State()


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = get_user(str(message.from_user.id))
    if user:
        await message.answer(
            f"С возвращением! Отправь JSON-файл экспорта из Telegram Desktop, "
            f"чтобы загрузить новый чат.\n\n"
            f"Команды:\n"
            f"/rewrite — переписать сообщение\n"
            f"/me — мой стиль общения"
        )
        return

    await message.answer(
        f"Привет! Это {APP_NAME}.\n\n"
        f"Мне нужен твой Telegram ID из экспорта чата. "
        f"Открой любой экспорт (result.json) и найди своё имя — "
        f"рядом будет поле «from_id», например: user123456789.\n\n"
        f"Отправь это значение:"
    )
    await state.set_state(Registration.waiting_for_my_id)


@dp.message(Registration.waiting_for_my_id)
async def handle_my_id(message: Message, state: FSMContext) -> None:
    my_id = message.text.strip()
    if not my_id:
        await message.answer("Пришли from_id — строку вида user123456789.")
        return

    upsert_user(str(message.from_user.id), my_id)
    await state.clear()
    await message.answer(
        f"Готово! Теперь отправь JSON-файл экспорта чата из Telegram Desktop.\n\n"
        f"Как экспортировать: открой нужный чат → ⋮ → Экспорт истории чата → "
        f"формат JSON, без медиафайлов."
    )


# ── Загрузка JSON-файла ───────────────────────────────────────────────────────

@dp.message(F.document)
async def handle_document(message: Message, bot: Bot) -> None:
    user = get_user(str(message.from_user.id))
    if not user:
        await message.answer("Сначала зарегистрируйся — отправь /start.")
        return

    doc: Document = message.document
    if not doc.file_name.endswith(".json"):
        await message.answer("Нужен JSON-файл экспорта (result.json).")
        return

    await message.answer("Получил файл, анализирую...")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / doc.file_name
        await bot.download(doc, destination=path)

        try:
            chat = parse_chat(str(path), user["my_id"])
        except Exception as e:
            await message.answer(f"Не удалось разобрать файл: {e}")
            return

    features = extract_features(chat)

    await message.answer("Считаю признаки... запускаю анализ (займёт ~30 секунд).")

    try:
        cards = await build_cards(chat, features)
    except Exception as e:
        await message.answer(f"Ошибка LLM-анализа: {e}")
        return

    telegram_id = str(message.from_user.id)
    contact_id = get_or_create_contact(
        telegram_id,
        chat.meta.contact_id,
        chat.meta.contact_name,
    )
    save_style_card(telegram_id, cards["style_card"])
    save_interaction_card(contact_id, cards["interaction_card"])

    await message.answer(
        f"Готово! Проанализировал переписку с {chat.meta.contact_name} "
        f"({chat.meta.total_messages} сообщений).\n\n"
        f"Теперь можешь написать /rewrite — перепишу твоё сообщение под этого собеседника."
    )


# ── /me ───────────────────────────────────────────────────────────────────────

@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    card = get_style_card(str(message.from_user.id))
    if not card:
        await message.answer("Нет данных. Сначала загрузи JSON-файл чата.")
        return
    await message.answer(f"Твой стиль общения:\n\n{card}")


# ── /rewrite ──────────────────────────────────────────────────────────────────

@dp.message(Command("rewrite"))
async def cmd_rewrite(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)

    style_card = get_style_card(telegram_id)
    if not style_card:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Нет загруженных чатов. Отправь JSON-файл.")
        return

    if len(contacts) == 1:
        await state.update_data(
            contact_id=contacts[0]["id"],
            style_card=style_card,
            interaction_card=get_interaction_card(contacts[0]["id"]),
        )
        await state.set_state(Rewrite.waiting_for_draft)
        name = contacts[0]["display_name"] or contacts[0]["contact_alias"]
        await message.answer(f"Напиши черновик сообщения для {name}:")
        return

    lines = "\n".join(
        f"{i+1}. {c['display_name'] or c['contact_alias']}"
        for i, c in enumerate(contacts)
    )
    await state.update_data(contacts=[dict(c) for c in contacts], style_card=style_card)
    await state.set_state(Rewrite.waiting_for_contact)
    await message.answer(f"Для кого пишешь? Отправь номер:\n\n{lines}")


@dp.message(Rewrite.waiting_for_contact)
async def handle_contact_choice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    contacts = data["contacts"]

    try:
        idx = int(message.text.strip()) - 1
        assert 0 <= idx < len(contacts)
    except (ValueError, AssertionError):
        await message.answer(f"Отправь число от 1 до {len(contacts)}.")
        return

    contact = contacts[idx]
    await state.update_data(
        contact_id=contact["id"],
        interaction_card=get_interaction_card(contact["id"]),
    )
    await state.set_state(Rewrite.waiting_for_draft)
    name = contact["display_name"] or contact["contact_alias"]
    await message.answer(f"Напиши черновик сообщения для {name}:")


@dp.message(Rewrite.waiting_for_draft)
async def handle_draft(message: Message, state: FSMContext) -> None:
    draft = message.text.strip()
    if not draft:
        await message.answer("Пришли черновик сообщения.")
        return

    data = await state.get_data()
    await message.answer("Переписываю...")

    try:
        result = await rewrite_message(draft, data["style_card"], data["interaction_card"])
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        await state.clear()
        return

    await state.clear()
    await message.answer(result)


# ── запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
