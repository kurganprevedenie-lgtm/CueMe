"""Админские рассылки /broadcast_*.

Вынесено из main.py без изменения логики (строки 2531–2960 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import logging
from aiogram import Bot, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    LinkPreviewOptions,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from storage import (
    mark_bot_blocked,
    get_users_without_deleted_messages_broadcast,
    mark_deleted_messages_broadcast_sent,
    get_users_without_deleted_paywall_broadcast,
    mark_deleted_paywall_broadcast_sent,
    get_users_without_price_drop_broadcast,
    mark_price_drop_broadcast_sent,
    list_all_users,
)
from app_state import _is_admin, dp
from handlers.subscription import _is_premium
from handlers.stars_tribute import _invite_text, invite_kb


# ── /broadcast_invite — разовое напоминание про рефералку ВСЕМ (только админ) ─

@dp.message(Command("broadcast_invite"))
async def cmd_broadcast_invite(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    users = list_all_users()
    await message.answer(
        f"⚠️ Разослать напоминание про рефералку {len(users)} пользователям? "
        "Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="bcast:invite:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:invite:cancel"),
        ]]),
    )


async def _run_broadcast_invite(bot: Bot, requester_id: int) -> None:
    """Фон — не блокирует основной event loop. Задержка между отправками —
    под лимиты Telegram Bot API (~30 сообщений/сек)."""
    users = list_all_users()
    sent = failed = blocked = 0

    for u in users:
        telegram_id = u["telegram_id"]
        try:
            body, link = await _invite_text(bot, telegram_id)
            text = "💡 Кстати, забыл сказать —\n\n" + body
            await bot.send_message(
                int(telegram_id), text, parse_mode="HTML",
                reply_markup=invite_kb(link),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            sent += 1
        except TelegramForbiddenError:
            # Юзер заблокировал бота — ожидаемо на любой массовой рассылке,
            # не роняет процесс, просто считаем и идём дальше. Заодно
            # обновляем последний известный статус для отчёта /users.
            mark_bot_blocked(telegram_id)
            blocked += 1
        except Exception:
            logging.exception("broadcast_invite: сбой для %s", telegram_id)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            requester_id,
            f"✅ Рассылка про рефералку завершена.\n"
            f"Отправлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}",
        )
    except Exception:
        logging.exception("broadcast_invite: не удалось отчитаться перед %s", requester_id)


@dp.callback_query(F.data == "bcast:invite:confirm")
async def cb_broadcast_invite_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_invite(bot, call.from_user.id))


@dp.callback_query(F.data == "bcast:invite:cancel")
async def cb_broadcast_invite_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Отменено.")


# ── /broadcast_price_drop — разовое объявление о снижении цены Premium ───────
# (только админ, тот же паттерн, что /broadcast_invite выше). Кнопка ведёт
# прямо на экран «Подписка» (show_premium) — без промокода, цена уже
# снижена по умолчанию (см. config.py: STAR_PRICE_MONTH).

# Было (до 2026-10-09): «…теперь 199 ₽/мес для новых пользователей. Успей…»
# (а ещё раньше — 150 ₽/мес). Заменено: «для новых пользователей» убрано,
# вместо него — что именно входит в Premium. Оставлено для отката.
# _PRICE_DROP_TEXT = (
#     "Привет! Мы снизили цену Premium — было 300 ₽/мес, теперь 199 ₽/мес "
#     "для новых пользователей. Успей, пока действует новая цена 👇"
# )
_PRICE_DROP_TEXT = (
    "Привет! Мы снизили цену Premium — было 300 ₽/мес, теперь 199 ₽/мес. "
    "Без лимитов на ответы, анализ собеседника и идеальное свидание. "
    "Успей, пока действует новая цена 👇"
)


def _price_drop_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👑 Оформить Premium", callback_data="show_premium")
    return b.as_markup()


@dp.message(Command("broadcast_price_drop"))
async def cmd_broadcast_price_drop(message: Message) -> None:
    """Команду можно запускать повторно — каждый раз уходит только тем, кто
    ещё не получал именно эту рассылку (price_drop_broadcast_sent), т.е.
    новым юзерам с прошлого раза, а не всем заново."""
    if not _is_admin(message.from_user.id):
        return
    users = get_users_without_price_drop_broadcast()
    if not users:
        await message.answer("Новых получателей нет — все, кто есть в базе, уже получили эту рассылку.")
        return
    await message.answer(
        f"⚠️ Разослать объявление о снижении цены {len(users)} новым пользователям "
        "(тем, кто ещё не получал эту рассылку)? Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="bcast:price_drop:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:price_drop:cancel"),
        ]]),
    )


async def _run_broadcast_price_drop(bot: Bot, requester_id: int) -> None:
    """Фон — не блокирует основной event loop. Задержка между отправками —
    под лимиты Telegram Bot API (~30 сообщений/сек), тот же паттерн, что
    _run_broadcast_invite. Только юзеры без price_drop_broadcast_sent —
    успешная отправка и «заблокировал бота» помечаются сразу (второй раз
    всё равно не уйдёт), обычная ошибка НЕ помечается — попробуем ещё раз
    при следующем запуске команды."""
    users = get_users_without_price_drop_broadcast()
    sent = failed = blocked = 0

    for u in users:
        telegram_id = u["telegram_id"]
        try:
            await bot.send_message(int(telegram_id), _PRICE_DROP_TEXT, reply_markup=_price_drop_kb())
            sent += 1
            mark_price_drop_broadcast_sent(telegram_id)
        except TelegramForbiddenError:
            mark_bot_blocked(telegram_id)
            mark_price_drop_broadcast_sent(telegram_id)
            blocked += 1
        except Exception:
            logging.exception("broadcast_price_drop: сбой для %s", telegram_id)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            requester_id,
            f"✅ Рассылка про снижение цены завершена.\n"
            f"Отправлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
            + ("\n\nОшибки не помечены как отправленные — попадут в следующий запуск команды." if failed else ""),
        )
    except Exception:
        logging.exception("broadcast_price_drop: не удалось отчитаться перед %s", requester_id)


@dp.callback_query(F.data == "bcast:price_drop:confirm")
async def cb_broadcast_price_drop_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_price_drop(bot, call.from_user.id))


@dp.callback_query(F.data == "bcast:price_drop:cancel")
async def cb_broadcast_price_drop_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Отменено.")


# ── /broadcast_deleted_messages — разовое объявление о показе удалённых ──────
# сообщений (только админ, тот же паттерн, что /broadcast_price_drop выше).
# Без кнопки — в отличие от price_drop (там кнопка вела на экран «Подписка»),
# тут никуда конкретно не ведём.

_DELETED_MESSAGES_BROADCAST_TEXT = (
    "🗑 Теперь CueMe показывает удалённые сообщения\n\n"
    "Если собеседник напишет тебе что-то и потом удалит — ты всё равно "
    "увидишь, что это было (текст, фото, видео, голосовое). Работает "
    "сразу, ничего включать не надо.\n\n"
    "Бесплатно для всех ближайшие 3 дня, потом — только по Premium."
)


@dp.message(Command("broadcast_deleted_messages"))
async def cmd_broadcast_deleted_messages(message: Message) -> None:
    """Команду можно запускать повторно — каждый раз уходит только тем, кто
    ещё не получал именно эту рассылку (deleted_messages_broadcast_sent),
    т.е. новым юзерам с прошлого раза, а не всем заново."""
    if not _is_admin(message.from_user.id):
        return
    users = get_users_without_deleted_messages_broadcast()
    if not users:
        await message.answer("Новых получателей нет — все, кто есть в базе, уже получили эту рассылку.")
        return
    await message.answer(
        f"⚠️ Разослать объявление об удалённых сообщениях {len(users)} новым пользователям "
        "(тем, кто ещё не получал эту рассылку)? Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="bcast:deleted_msgs:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:deleted_msgs:cancel"),
        ]]),
    )


async def _run_broadcast_deleted_messages(bot: Bot, requester_id: int) -> None:
    """Фон — не блокирует основной event loop. Задержка между отправками —
    под лимиты Telegram Bot API (~30 сообщений/сек), тот же паттерн, что
    _run_broadcast_price_drop. Только юзеры без deleted_messages_broadcast_sent —
    успешная отправка и «заблокировал бота» помечаются сразу (второй раз
    всё равно не уйдёт), обычная ошибка НЕ помечается — попробуем ещё раз
    при следующем запуске команды."""
    users = get_users_without_deleted_messages_broadcast()
    sent = failed = blocked = 0

    for u in users:
        telegram_id = u["telegram_id"]
        try:
            await bot.send_message(int(telegram_id), _DELETED_MESSAGES_BROADCAST_TEXT)
            sent += 1
            mark_deleted_messages_broadcast_sent(telegram_id)
        except TelegramForbiddenError:
            mark_bot_blocked(telegram_id)
            mark_deleted_messages_broadcast_sent(telegram_id)
            blocked += 1
        except Exception:
            logging.exception("broadcast_deleted_messages: сбой для %s", telegram_id)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            requester_id,
            f"✅ Рассылка об удалённых сообщениях завершена.\n"
            f"Отправлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
            + ("\n\nОшибки не помечены как отправленные — попадут в следующий запуск команды." if failed else ""),
        )
    except Exception:
        logging.exception("broadcast_deleted_messages: не удалось отчитаться перед %s", requester_id)


@dp.callback_query(F.data == "bcast:deleted_msgs:confirm")
async def cb_broadcast_deleted_messages_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_deleted_messages(bot, call.from_user.id))


@dp.callback_query(F.data == "bcast:deleted_msgs:cancel")
async def cb_broadcast_deleted_messages_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Отменено.")


# ── /broadcast_deleted_paywall — «удалённые сообщения теперь только по ──────
# Premium» (только админ, тот же паттерн, что /broadcast_deleted_messages).
# Уходит только юзерам БЕЗ Premium (проверка _is_premium на отправке) —
# премиум-юзеров пропускаем и НЕ помечаем: если позже потеряют Premium,
# повторный запуск команды до них дойдёт. Сама функция для не-премиум
# отключена в handle_deleted_business_messages.

_DELETED_PAYWALL_BROADCAST_TEXT = (
    "🗑 Удалённые сообщения теперь только с Premium\n\n"
    "Бесплатный период закончился. Я по-прежнему сообщу, когда собеседник "
    "что-то удалит, но само содержимое (текст, фото, видео, голосовые) "
    "открывается только с Premium. Оформишь подписку — сможешь посмотреть "
    "и то, что удалили до этого 👇"
)


@dp.message(Command("broadcast_deleted_paywall"))
async def cmd_broadcast_deleted_paywall(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    users = get_users_without_deleted_paywall_broadcast()
    if not users:
        await message.answer("Новых получателей нет — все, кто есть в базе, уже получили эту рассылку.")
        return
    await message.answer(
        f"⚠️ Разослать уведомление «удалённые сообщения только по Premium»? "
        f"Кандидатов: {len(users)} — пользователи с Premium будут пропущены "
        "при отправке. Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="bcast:deleted_paywall:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:deleted_paywall:cancel"),
        ]]),
    )


async def _run_broadcast_deleted_paywall(bot: Bot, requester_id: int) -> None:
    """Фон, задержка между отправками и пометки — как в
    _run_broadcast_deleted_messages. Обычная ошибка не помечается —
    попадёт в следующий запуск команды."""
    users = get_users_without_deleted_paywall_broadcast()
    sent = failed = blocked = skipped_premium = 0

    for u in users:
        telegram_id = u["telegram_id"]
        if await _is_premium(bot, telegram_id):
            skipped_premium += 1
            continue
        try:
            await bot.send_message(int(telegram_id), _DELETED_PAYWALL_BROADCAST_TEXT, reply_markup=_price_drop_kb())
            sent += 1
            mark_deleted_paywall_broadcast_sent(telegram_id)
        except TelegramForbiddenError:
            mark_bot_blocked(telegram_id)
            mark_deleted_paywall_broadcast_sent(telegram_id)
            blocked += 1
        except Exception:
            logging.exception("broadcast_deleted_paywall: сбой для %s", telegram_id)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            requester_id,
            f"✅ Рассылка «удалённые сообщения только по Premium» завершена.\n"
            f"Отправлено: {sent}\nПропущено (есть Premium): {skipped_premium}\n"
            f"Заблокировали бота: {blocked}\nОшибок: {failed}"
            + ("\n\nОшибки не помечены как отправленные — попадут в следующий запуск команды." if failed else ""),
        )
    except Exception:
        logging.exception("broadcast_deleted_paywall: не удалось отчитаться перед %s", requester_id)


@dp.callback_query(F.data == "bcast:deleted_paywall:confirm")
async def cb_broadcast_deleted_paywall_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_deleted_paywall(bot, call.from_user.id))


@dp.callback_query(F.data == "bcast:deleted_paywall:cancel")
async def cb_broadcast_deleted_paywall_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Отменено.")


# ── /broadcast_admin — универсальная разовая рассылка ПРОИЗВОЛЬНОГО текста ───
# (только админ). В отличие от /broadcast_invite и /broadcast_price_drop —
# не привязана к конкретной фиче, текст берётся из самого сообщения команды
# (всё после "/broadcast_admin "), чтобы не заводить новую команду под
# каждое разовое объявление (например про временные перебои у LLM-провайдеров).
# Текст на подтверждении хранится в памяти по admin_id — короткоживущий,
# переживать перезапуск бота не должен (это разовое действие в рамках сессии).

_pending_admin_broadcasts: dict[int, str] = {}


@dp.message(Command("broadcast_admin"))
async def cmd_broadcast_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) == 2 else ""
    if not text:
        await message.answer("Использование: /broadcast_admin <текст сообщения для всех пользователей>")
        return

    users = list_all_users()
    _pending_admin_broadcasts[message.from_user.id] = text
    preview = text if len(text) <= 500 else text[:500] + "…"
    await message.answer(
        f"⚠️ Разослать это сообщение {len(users)} пользователям? Действие необратимо.\n\n"
        f"—— текст ——\n{preview}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="bcast:admin:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:admin:cancel"),
        ]]),
    )


async def _run_broadcast_admin(bot: Bot, requester_id: int, text: str) -> None:
    """Фон — не блокирует основной event loop. Задержка между отправками —
    под лимиты Telegram Bot API (~30 сообщений/сек), тот же паттерн, что
    _run_broadcast_invite/_run_broadcast_price_drop. Разовая — без трекинга
    "кому уже уходило" (в отличие от price_drop), повторный запуск команды
    с новым текстом снова уйдёт всем."""
    users = list_all_users()
    sent = failed = blocked = 0

    for u in users:
        telegram_id = u["telegram_id"]
        try:
            await bot.send_message(int(telegram_id), text)
            sent += 1
        except TelegramForbiddenError:
            mark_bot_blocked(telegram_id)
            blocked += 1
        except Exception:
            logging.exception("broadcast_admin: сбой для %s", telegram_id)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            requester_id,
            f"✅ Рассылка завершена.\nОтправлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}",
        )
    except Exception:
        logging.exception("broadcast_admin: не удалось отчитаться перед %s", requester_id)


@dp.callback_query(F.data == "bcast:admin:confirm")
async def cb_broadcast_admin_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    text = _pending_admin_broadcasts.pop(call.from_user.id, None)
    await call.answer()
    if not text:
        await call.message.edit_text("Текст не найден (истёк или бот перезапускался) — запусти /broadcast_admin заново.")
        return
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_admin(bot, call.from_user.id, text))


@dp.callback_query(F.data == "bcast:admin:cancel")
async def cb_broadcast_admin_cancel(call: CallbackQuery) -> None:
    _pending_admin_broadcasts.pop(call.from_user.id, None)
    await call.answer()
    await call.message.edit_text("Отменено.")
