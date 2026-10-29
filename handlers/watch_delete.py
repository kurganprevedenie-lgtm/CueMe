"""«🗑 Удалённые сообщения» — настройки, /delete.

Вынесено из main.py без изменения логики (строки 10367–10550 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from storage import (
    delete_all_user_data,
    delete_contact_data,
    get_contact_by_id,
    list_contacts,
    get_watch_setting,
    toggle_watch_setting,
    WATCH_SETTINGS,
)
from app_state import _contact_name, dp
from handlers.subscription import _is_premium
from handlers.redeem_menu import _menu_screen_target
from handlers.profile import paged_contacts_kb


# ── 🗑 Удалённые сообщения — настройки трёх Premium-функций слежки ──────────
# Все три по умолчанию включены. Без Premium экран виден, но переключатели
# заблокированы (тап — подсказка про Premium), а сами функции присылают
# только тизеры. Проверки настроек — в handle_deleted_business_messages,
# handle_edited_business_message и _save_ephemeral_reply_media.

_WATCH_ITEMS = (
    ("watch_deleted", "Удалённые сообщения"),
    ("watch_edited", "Изменённые сообщения"),
    ("watch_ephemeral", "Сохранение исчезающих (с таймером)"),
)

_WATCH_TEXT = (
    "🗑 Удалённые сообщения\n\n"
    "Что присылать тебе из переписок:\n"
    "• удалённые собеседником сообщения\n"
    "• изменённые — что было и что стало\n"
    "• исчезающие фото, видео, кружки и голосовые — ответь на такое "
    "сообщение, и я пришлю копию\n\n"
    "Нажми на пункт, чтобы включить или выключить."
)


def _watch_kb(telegram_id: str, is_premium: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label in _WATCH_ITEMS:
        if is_premium:
            mark = "✅" if get_watch_setting(telegram_id, key) else "❌"
            b.button(text=f"{mark} {label}", callback_data=f"watch:toggle:{key}")
        else:
            b.button(text=f"🔒 {label}", callback_data="watch:locked")
    if not is_premium:
        b.button(text="👑 Оформить Premium", callback_data="show_premium")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


async def _show_watch_screen(target: Message, bot: Bot, telegram_id: str) -> None:
    is_premium = await _is_premium(bot, telegram_id)
    text = _WATCH_TEXT if is_premium else f"{_WATCH_TEXT}\n\n🔒 Доступно с Premium."
    await target.edit_text(text, reply_markup=_watch_kb(telegram_id, is_premium))


@dp.callback_query(F.data == "watch:menu")
async def cb_watch_menu(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_watch_screen(await _menu_screen_target(call.message), bot, str(call.from_user.id))


@dp.callback_query(F.data == "watch:locked")
async def cb_watch_locked(call: CallbackQuery) -> None:
    await call.answer("Настройки доступны с Premium", show_alert=True)


@dp.callback_query(F.data.startswith("watch:toggle:"))
async def cb_watch_toggle(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    key = call.data.split(":", 2)[2]
    if key not in WATCH_SETTINGS or not await _is_premium(bot, telegram_id):
        await call.answer("Настройки доступны с Premium", show_alert=True)
        return
    enabled = await asyncio.to_thread(toggle_watch_setting, telegram_id, key)
    await call.answer("Включено" if enabled else "Выключено")
    await _show_watch_screen(call.message, bot, telegram_id)


# ── /delete — удалить данные (152-ФЗ) ────────────────────────────────────────

def _delete_kb(contacts: list, page: int = 0) -> InlineKeyboardMarkup:
    # Было: все контакты одной клавиатурой — теперь постранично (handlers/profile.py:
    # paged_contacts_kb), «‼️ Удалить ВСЕ данные» — на каждой странице.
    return paged_contacts_kb(
        contacts, "del", page,
        lambda c: (f"🗑 {_contact_name(c)}", f"del:{c['id']}"),
        extra_buttons=(("‼️ Удалить ВСЕ данные", "delall"),),
    )


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


async def _resolve_target_id(
    message: Message, bot: Bot, command: str, arg: str | None = None, usage: str | None = None,
) -> tuple[str, str] | None:
    """Разбирает "<telegram_id>" или "@username" из текста команды — общий
    парсинг для /wipe и /inspect. Возвращает (target_id, arg_display) или
    None (сообщение об ошибке уже отправлено). arg — когда у команды есть
    другие аргументы (/gift_premium), цель передаётся уже вырезанной."""
    if arg is None:
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip() if len(parts) == 2 else ""
    usage = usage or f"Использование: {command} <telegram_id> или {command} @username"
    if not arg:
        await message.answer(usage)
        return None

    if arg.isdigit():
        return arg, arg
    if arg.startswith("@"):
        # Резолвим @username → numeric id через Telegram API. Работает только
        # если бот уже когда-то получал апдейт от этого юзера — иначе getChat падает.
        try:
            chat = await bot.get_chat(arg)
        except TelegramBadRequest:
            await message.answer(
                f"Не удалось найти {arg} — бот должен был хотя бы раз получить "
                "от него сообщение, иначе Telegram не отдаёт chat по username."
            )
            return None
        target_id = str(chat.id)
        return target_id, f"{arg} ({target_id})"

    await message.answer(usage)
    return None
