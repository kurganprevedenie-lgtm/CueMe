"""/start, онбординг, кнопки главного меню, загрузка JSON, /connect.

Вынесено из main.py без изменения логики (строки 6315–6930 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import html
import logging
import tempfile
from pathlib import Path
from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Document,
    FSInputFile,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import (
    ADMIN_GROUP_CHAT_ID,
    ONBOARDING_PHOTO_FILE_ID,
    ONBOARDING_PHOTO_PATH,
    OPENERS_FOR_HER,
    OPENERS_FOR_HIM,
    TEST_ACCOUNT_USERNAMES,
)
from features import extract_features
from llm import make_features_summary, sample_texts
from tg_parser import parse_chat
from storage import (
    delete_style_card,
    get_latest_business_connection,
    get_contact_by_id,
    get_referrer_by_code,
    get_user,
    mark_bot_unblocked,
    mark_legacy_kb_cleared,
    save_imported_messages,
    get_or_create_contact,
    list_contacts,
    save_message_samples,
    save_referral_pending,
    record_event,
    set_gender,
    upsert_user,
)
from app_state import (
    BTN_DATE,
    BTN_DEEP,
    BTN_HELP,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_UNIFIED,
    _ALL_BTNS,
    _contact_name,
    _is_admin,
    dp,
)
from handlers.subscription import _require_premium
from handlers.referrals_gate import _credit_referral_if_pending
from handlers.stars_tribute import _show_invite
from handlers.redeem_menu import _send_main_menu, _with_back_to_menu
from handlers.profile import (
    _GENDER_LABELS,
    _maybe_prompt_gender,
    contacts_kb,
    gender_kb,
)
from handlers.cards import Setup, _gen_interaction_card, _gen_style_card
from handlers.analysis import _show_deep_analysis, _show_ideal_date
import handlers  # ссылки по модулю: имена ниже по исходному main.py / общее состояние
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── /start ────────────────────────────────────────────────────────────────────

def _quickstart_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Да, есть", callback_data="qs:yes"),
        InlineKeyboardButton(text="🤷 Пока никого", callback_data="qs:no"),
    ]])


def _no_contacts_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # callback_data="qs:yes" — переиспользует существующий cb_quickstart_yes
    # (получает state через DI aiogram сам), не нужно тащить state через
    # все функции, которые показывают эту подсказку.
    b.button(text="💬 Ответ с CueMe", callback_data="qs:yes")
    return b.as_markup()


async def _send_no_contacts_hint(message: Message) -> None:
    """Единая замена тупикового "Сначала загрузи JSON-файл чата." — во всех
    местах, требующих хотя бы один контакт. JSON остаётся рабочим опциональным
    путём (кто с компьютера — дойдёт сам), но дефолтная подсказка ведёт в уже
    существующий единый флоу, который как раз и создаёт контакт на лету."""
    await message.answer(
        "Пока нет ни одного диалога для этого — начни с «💬 Ответ с CueMe», "
        "перешли туда любое сообщение, и я заведу первый контакт.",
        reply_markup=_no_contacts_kb(),
    )


# _send_no_dialogs_hint — заменена на _finish_onboarding_chain (edit-in-place
# всей Business-цепочки онбординга, задача «объединить цепочку сообщений»).
# Раньше слала ДВА новых сообщения: сначала "·"-заглушку (единственная цель —
# доставить reply-клавиатуру main_kb, которую нельзя повесить на сообщение с
# inline-кнопками вопроса) + сам вопрос отдельным сообщением. main_kb теперь
# уезжает юзеру раньше, вместе с первым сообщением о подключении
# (handle_business_connection), так что заглушка не нужна — оставлено здесь
# закомментированным на случай отката.
# async def _send_no_dialogs_hint(message: Message) -> None:
#     try:
#         await message.answer("·", reply_markup=main_kb())
#     except Exception:
#         logging.exception("_send_no_dialogs_hint: не удалось отправить main_kb")
#     await message.answer(
#         "Готово, бот подключён! Есть кто-то конкретный, с кем сейчас переписываешься?",
#         reply_markup=_quickstart_kb(),
#     )


_QUICKSTART_PROMPT_TEXT = "Готово, бот подключён! Есть кто-то конкретный, с кем сейчас переписываешься?"


async def _finish_onboarding_chain(bot: Bot, chat_id: int, message_id: int) -> None:
    """Финальный шаг Business-цепочки онбординга (источник → пол → это) —
    редактирует то же самое сообщение вместо отправки нового."""
    try:
        await bot.edit_message_text(
            _QUICKSTART_PROMPT_TEXT,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=_quickstart_kb(),
        )
    except Exception:
        logging.warning(
            "_finish_onboarding_chain: не удалось отредактировать chat_id=%s message_id=%s",
            chat_id, message_id,
        )


@dp.callback_query(F.data == "qs:yes")
async def cb_quickstart_yes(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await handlers.reply._start_unified_reply(call.message, state)


def dating_apps_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💘 Дайвинчик", url="https://t.me/leomatchbot")
    b.button(text="⚡ FastLove", url="https://t.me/fastlovetg_bot")
    b.adjust(1)
    return b.as_markup()


def _quickstart_gender_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👩 Ей", callback_data="qsphr:her")
    b.button(text="👨 Ему", callback_data="qsphr:him")
    b.adjust(2)
    return b.as_markup()


@dp.callback_query(F.data == "qs:no")
async def cb_quickstart_no(call: CallbackQuery) -> None:
    await call.answer()
    # edit, не answer — остаёмся тем же сообщением цепочки; новое сообщение
    # начинается только когда бот реально присылает готовую фразу
    # (cb_quickstart_gender ниже).
    await call.message.edit_text("Кому бы написал(-а)?", reply_markup=_quickstart_gender_kb())


def _quickstart_phrase_next_kb(target: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data=f"qsphr_next:{target}")
    b.button(text="💘 Дайвинчик", url="https://t.me/leomatchbot")
    b.adjust(1)
    return _with_back_to_menu(b.as_markup())


async def _quickstart_phrase_content(state: FSMContext, target: str) -> tuple[str, InlineKeyboardMarkup]:
    """Своя, изолированная от phrases:*, ветка — переиспользует только
    ДАННЫЕ (OPENERS_FOR_HER/HIM), не общий код _send_opener/cb_phrases_gender,
    чтобы не задеть существующий путь «🎲 Готовые фразы для начала».
    Возвращает текст+клавиатуру: первый показ шлёт новое сообщение
    (_send_quickstart_phrases), «Другой вариант» редактирует то же."""
    items = OPENERS_FOR_HER if target == "her" else OPENERS_FOR_HIM
    phrase = await handlers.reply._pick_no_repeat(state, f"qs_opener_shown_{target}", items)
    intro = "Вот пара фраз для начала — сохрани, пригодятся:"
    if "[" in phrase:
        intro += " замени [то, что в скобках] на реальную деталь из анкеты."
    text = f"{intro}\n\n<code>{html.escape(phrase)}</code>\n\nПосле того как найдешь себе партнера возвращайся и пиши в 💬 Ответ с CueMe"
    return text, _quickstart_phrase_next_kb(target)


async def _send_quickstart_phrases(msg: Message, state: FSMContext, target: str) -> None:
    text, kb = await _quickstart_phrase_content(state, target)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("qsphr:"))
async def cb_quickstart_gender(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]  # her | him
    await call.answer()
    await _send_quickstart_phrases(call.message, state, target)


@dp.callback_query(F.data.startswith("qsphr_next:"))
async def cb_quickstart_phrase_next(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]
    await call.answer("Другой вариант")
    text, kb = await _quickstart_phrase_content(state, target)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        # При исчерпании списка _pick_no_repeat начинает заново и может
        # выпасть та же фраза, что уже на экране — Telegram отвечает
        # «message is not modified»; это не ошибка, просто глотаем.
        if "not modified" not in str(e):
            raise


# Выбор устройства (iPhone/Android/десктоп) убран — шаги подключения теперь
# одной универсальной формулировкой, без platform-specific веток.


async def _business_connect_text(bot: Bot) -> str:
    me = await bot.get_me()
    return (
        "Подключи бота к своим чатам — он будет учиться твоему стилю прямо "
        "по живой переписке, ничего загружать не нужно:\n\n"
        "1️⃣ Открой Telegram → Настройки (в приложении — вкладка ⚙️ внизу "
        "экрана или значок ☰/твой аватар в углу; на компьютере — значок ☰ "
        "в левом верхнем углу или свой аватар в левой панели)\n"
        "2️⃣ Нажми «Изменить» рядом со своим профилем/фото\n"
        "3️⃣ Выбери «Автоматизация чатов»\n"
        f"4️⃣ В поле впиши @{me.username} и выбери меня\n"
        "5️⃣ Включи переключатель «Ответы на сообщения»\n"
        "6️⃣ Выбери чаты, к которым дать доступ (можно один)\n\n"
        "Не нашёл пункт «Автоматизация чатов»? В поиске по настройкам введи "
        "«автоматизация» или «automation» — так быстрее всего."
    )


def business_connect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подключить", url="tg://settings/edit")],
        [InlineKeyboardButton(text="👀 Видео-инструкция", url="https://t.me/CueMee")],
        # "👑 Подписка" убрана с этого экрана (задача: стартовый экран не
        # должен вести на подписку) — доступ к ней остаётся через кнопку
        # "👑 Подписка" в главном меню (main_menu_kb) и команду /premium.
        [InlineKeyboardButton(text="✨ Возможности бота", url="https://t.me/CueMee")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/CueMeSupport")],
    ])


# demo_teaser_kb — клавиатура под убранным демо-тизером, см. комментарий у
# DEMO_TEASER_TEXT ниже. Не удалена физически.
# def demo_teaser_kb() -> InlineKeyboardMarkup:
#     return InlineKeyboardMarkup(inline_keyboard=[
#         [InlineKeyboardButton(text="Использовать бота", callback_data="onb:show_connect")],
#     ])


async def _send_business_connect_prompt_to(
    bot: Bot, chat_id: int, text: str, parse_mode: str | None = None,
) -> None:
    """Инструкция по подключению Автоматизации чатов — со скриншотом, куда
    именно нажимать в настройках Telegram, если задан ONBOARDING_PHOTO_PATH/
    ONBOARDING_PHOTO_FILE_ID (.env), иначе просто текст. Приоритет
    файл-на-диске → file_id → голый текст. Версия по chat_id — для фоновых
    напоминаний (_send_funnel_reminder), где входящего Message нет;
    _send_business_connect_prompt ниже — обёртка для хендлеров."""
    photo_path = Path(ONBOARDING_PHOTO_PATH) if ONBOARDING_PHOTO_PATH else None
    if photo_path and photo_path.is_file():
        await bot.send_photo(
            chat_id,
            photo=FSInputFile(photo_path),
            caption=text,
            parse_mode=parse_mode,
            reply_markup=business_connect_kb(),
        )
    elif ONBOARDING_PHOTO_FILE_ID:
        await bot.send_photo(
            chat_id,
            photo=ONBOARDING_PHOTO_FILE_ID,
            caption=text,
            parse_mode=parse_mode,
            reply_markup=business_connect_kb(),
        )
    else:
        await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=business_connect_kb())


async def _send_business_connect_prompt(
    target: Message, text: str, parse_mode: str | None = None,
) -> None:
    """Обёртка над _send_business_connect_prompt_to для хендлеров —
    cb_onboarding_business, cmd_connect и cb_onboarding_show_connect
    (последнему нужен parse_mode="HTML" — в его тексте <blockquote>)."""
    await _send_business_connect_prompt_to(target.bot, target.chat.id, text, parse_mode)


async def _connect_steps_blockquote(bot: Bot) -> str:
    """4 шага подключения (HTML <blockquote>) — общий блок для экрана после
    демо (cb_onboarding_show_connect) и funnel-напоминания стадии 'connect'."""
    me = await bot.get_me()
    return (
        "<blockquote>❓ Подключить бота:\n"
        "1. Настройки → «Изменить» рядом с профилем\n"
        "2. Автоматизация чатов\n"
        f"3. Впиши @{me.username} и выбери меня\n"
        "4. Включи «Ответы на сообщения» и выбери чаты</blockquote>"
    )


# ── Захват file_id фото-инструкции (только для админа) ───────────────────────
# Разработчик присылает фото боту напрямую (просто как сообщение) — бот в
# ответ шлёт его file_id, который нужно прописать в ONBOARDING_PHOTO_FILE_ID
# (.env на сервере). Фото хранится на серверах Telegram, не в репозитории.

@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    file_id = message.photo[-1].file_id  # последний элемент — самое большое разрешение
    await message.answer(
        f"file_id:\n\n"
        f"<code>{html.escape(file_id)}</code>\n\n"
        "Пропиши его в .env на сервере как ONBOARDING_PHOTO_FILE_ID и "
        "перезапусти бота.",
        parse_mode="HTML",
    )


# DEMO_TEASER_TEXT/demo_teaser_kb/cb_onboarding_show_connect — убраны по
# прямому запросу: первый экран для нового юзера был демо-примером ответа
# с кнопкой «Использовать бота», сначала показывал ЗАЧЕМ подключать и
# только по тапу вёл к инструкции подключения. Теперь _send_start_menu
# сразу ведёт на инструкцию подключения (тот же текст/фото, что раньше
# показывались после тапа) — лишний экран убран. Не удалено физически.
# DEMO_TEASER_TEXT = (
#     f"👋 Привет! Я <b>{APP_NAME}</b> — подскажу, что ответить в переписке.\n\n"
#     "Тебе пишут:\n"
#     "<blockquote>«ты сегодня какой-то подозрительно долго не писал, случайно "
#     "не заскучал по мне? 😏»</blockquote>\n\n"
#     f"{APP_NAME} за секунды предложит ответ в твоём стиле:\n"
#     "<blockquote>«заскучал, да. а что, хочешь это исправить?)))»</blockquote>"
# )


async def _send_start_menu(message: Message, telegram_id: str) -> None:
    if list_contacts(telegram_id):
        await _send_main_menu(message)
        return

    # Демо-тизер убран — сразу показываем инструкцию по подключению
    # (раньше была за тапом «Использовать бота», см. комментарий у
    # DEMO_TEASER_TEXT выше).
    welcome_text = await _connect_steps_blockquote(message.bot)
    await _send_business_connect_prompt(message, welcome_text, parse_mode="HTML")


# @dp.callback_query(F.data == "onb:show_connect")
# async def cb_onboarding_show_connect(call: CallbackQuery, bot: Bot) -> None:
#     await call.answer()
#     try:
#         await call.message.delete()
#     except Exception:
#         pass
#     welcome_text = await _connect_steps_blockquote(bot)
#     # Фото-инструкция крепится caption'ом, приоритет файл на диске
#     # (ONBOARDING_PHOTO_PATH) → file_id → голый текст — см.
#     # _send_business_connect_prompt. Больше НИЧЕГО следом не шлём —
#     # намеренно, чтобы не отвлекать от единственного действия (подключить).
#     await _send_business_connect_prompt(call.message, welcome_text, parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    telegram_id = str(message.from_user.id)
    is_new = get_user(telegram_id) is None
    # Строка в users нужна СРАЗУ на первом /start, а не только когда юзер
    # дойдёт до конца онбординга (Business/JSON/ответ на вопросы) — иначе
    # уведомление "🆕 Новый пользователь" в админ-группе улетало, а сам юзер
    # оставался невидим в /users, если дальше ничего не сделал.
    upsert_user(telegram_id, f"user{telegram_id}")
    mark_bot_unblocked(telegram_id)  # живой /start — юзер точно не заблокировал бота
    if is_new:
        mark_legacy_kb_cleared(telegram_id)  # новый юзер старую reply-клавиатуру никогда не видел
    try:
        record_event(telegram_id, "start")  # для "последнее действие" в /users
    except Exception:
        logging.exception("cmd_start: не удалось записать событие")

    # Реферальная ссылка (/start ref<CODE>) — засчитываем и начисляем СРАЗУ,
    # только для реально НОВОГО юзера (is_new — до этого момента его не было
    # в users вообще), без требования Premium или подключения Автоматизации
    # чатов, см. коммент у _credit_referral_if_pending. Повторный /start (уже
    # не is_new) с тем же параметром — no-op, защита от повторного начисления.
    if is_new:
        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) == 2 else ""
        if payload.startswith("ref") and len(payload) > 3:
            code = payload[3:].strip().upper()
            referrer_id = get_referrer_by_code(code)
            if referrer_id and referrer_id != telegram_id:
                save_referral_pending(referrer_id, telegram_id)
                await _credit_referral_if_pending(bot, telegram_id)

    await _send_start_menu(message, telegram_id)
    username = message.from_user.username
    is_test_account = bool(username) and username.lower() in TEST_ACCOUNT_USERNAMES
    if is_new and ADMIN_GROUP_CHAT_ID and not is_test_account:
        who = f"@{username}" if username else f"id{telegram_id} (без username)"
        asyncio.create_task(_notify_admin_new_user(bot, telegram_id, who))


# Было: «🆕 Новый пользователь» уходил в админ-группу сразу на /start — без
# информации, дошёл ли юзер до главного шага. Теперь ждём
# _NEW_USER_NOTIFY_DELAY и пишем заодно, подключил ли он Автоматизацию
# чатов за это время. Задача живёт в памяти: если бот перезапустится в эти
# минуты, уведомление по этому юзеру не уйдёт.
_NEW_USER_NOTIFY_DELAY = 5 * 60


async def _notify_admin_new_user(bot: Bot, telegram_id: str, who: str) -> None:
    await asyncio.sleep(_NEW_USER_NOTIFY_DELAY)
    try:
        conn = await asyncio.to_thread(get_latest_business_connection, telegram_id)
        connected = bool(conn and conn["is_enabled"])
        status = "✅ Автоматизация подключена" if connected else "❌ Автоматизация выключена"
        await bot.send_message(int(ADMIN_GROUP_CHAT_ID), f"🆕 Новый пользователь: {who}\n{status}")
    except Exception:
        logging.warning("admin-group new-user notify failed: %s", who)


@dp.callback_query(F.data.in_({"gender:male", "gender:female"}))
async def cb_gender_select(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    gender = call.data.split(":", 1)[1]
    telegram_id = str(call.from_user.id)
    set_gender(telegram_id, gender)
    # Подтверждение — всплывающим тостом, не отдельным сообщением в чате.
    await call.answer(f"Обращаюсь как к «{_GENDER_LABELS[gender]}»")
    await state.clear()

    if list_contacts(telegram_id):
        # Уже есть хотя бы один контакт (демо/JSON/Business) — вопрос про пол
        # всегда задаётся СРАЗУ после того, как онбординг только что показал
        # меню и инструкции; повторно слать "С возвращением!" тут не нужно —
        # само сообщение с вопросом про пол больше не нужно, убираем его.
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        # Пол спросили сразу после подключения Автоматизации чатов, ещё до
        # первого реального сообщения — контакта пока нет. Полный экран
        # приветствия тут ни к чему, это уже пройденный шаг — редактируем ЭТО
        # ЖЕ сообщение на финальный вопрос цепочки (было: delete + 2 новых
        # сообщения через _send_no_dialogs_hint).
        await _finish_onboarding_chain(bot, call.message.chat.id, call.message.message_id)


@dp.message(Command("gender"))
async def cmd_gender(message: Message) -> None:
    await message.answer("Как теперь к тебе обращаться?", reply_markup=gender_kb())


@dp.callback_query(F.data == "onb:business")
async def cb_onboarding_business(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await call.answer()
    telegram_id = str(call.from_user.id)
    upsert_user(telegram_id, f"user{call.from_user.id}")
    await _send_business_connect_prompt(call.message, await _business_connect_text(bot))


@dp.callback_query(F.data == "onb:json")
async def cb_onboarding_json(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    telegram_id = str(call.from_user.id)
    if not await _require_premium(bot, call.message, telegram_id):
        return
    await state.set_state(Setup.waiting_for_json)
    await call.message.answer(
        "Загрузи переписку: Telegram Desktop → открой чат → ⋮ → "
        "Экспорт истории чата → формат JSON (без медиа) → пришли файл result.json сюда."
    )


def support_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🆘 Написать в поддержку", url="https://t.me/CueMeSupport")
    return b.as_markup()


def help_kb() -> InlineKeyboardMarkup:
    """Как support_kb(), плюс «⬅️ Назад» — для _show_help(edit=True) из
    главного меню (callback_data="sub:to_menu" — тот же таргет, что и
    «Назад» из «Подписка», см. cb_sub_to_main_menu, работает для любого
    сообщения, не только для экрана Подписки)."""
    b = InlineKeyboardBuilder()
    b.button(text="🆘 Написать в поддержку", url="https://t.me/CueMeSupport")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


# _show_support — раньше показывала только ссылку на @CueMeSupport по тапу
# «🆘 Помощь» в главном меню. По запросу «в кнопке помощь должен быть список
# команд» — BTN_SUPPORT теперь ведёт на _show_help (тот же список, что и
# BTN_HELP//help), с кнопкой на поддержку внизу (support_kb в _show_help).
# Не удалена физически — на случай отката.
# async def _show_support(message: Message) -> None:
#     await message.answer(
#         "Если что-то не работает или есть вопрос — пиши сюда:",
#         reply_markup=support_kb(),
#     )


# ── Кнопки главного меню ──────────────────────────────────────────────────────

@dp.message(F.text.in_(_ALL_BTNS))
async def handle_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    # if message.text == BTN_SCREENSHOT:
    #     await _start_screenshot(message, state)
    # elif message.text == BTN_REPLY:
    #     await _start_reply(message, state)
    # elif message.text == BTN_LIVE:
    #     await _show_live_start(message)
    if message.text == BTN_UNIFIED:
        await handlers.reply._start_unified_reply(message, state)
    elif message.text == BTN_DEEP:
        await _show_deep_analysis(message, bot)
    elif message.text == BTN_DATE:
        await _show_ideal_date(message, bot)
    elif message.text == BTN_SUBSCRIPTION:
        await main._show_premium_screen(message, bot, str(message.from_user.id))
    elif message.text == BTN_HELP:
        await main._show_help(message)
    elif message.text == BTN_SUPPORT:
        await main._show_help(message)


@dp.callback_query(F.data.startswith("menu:"))
async def cb_submenu(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    action = call.data.split(":", 1)[1]
    telegram_id = str(call.from_user.id)
    await call.answer()
    if action == "date":
        await _show_ideal_date(call.message, bot, telegram_id)
    # elif action == "revive":  # «Скрипты общения» убраны совсем — см. BTN_REVIVE
    #     await _show_revive(call.message, state)
    elif action == "invite":
        await _show_invite(call.message, bot, telegram_id)


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

    if not await _require_premium(bot, message, telegram_id):
        return

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

    await _credit_referral_if_pending(bot, telegram_id)
    await _maybe_prompt_gender(bot, telegram_id)

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
                await message.answer(
                    f"Готово! Данные по {name} загружены.\n"
                    "Открой /menu — там «💬 Ответ с CueMe» подскажет что ответить.",
                    reply_markup=ReplyKeyboardRemove(),
                )
            else:
                await message.answer(
                    "Файл загружен. Открой /menu для работы.",
                    reply_markup=ReplyKeyboardRemove(),
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
            "Открой /menu → «🔬 Анализ собеседника» для разбора.",
            reply_markup=ReplyKeyboardRemove(),
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
        await state.clear()
        await call.message.edit_text(
            f"Готово! Данные по {name} загружены.\n"
            "Открой /menu — там «💬 Ответ с CueMe» подскажет что ответить."
        )
    else:
        await state.clear()
        await call.message.edit_text("Файл загружен. Открой /menu для работы.")


# ── /connect ─────────────────────────────────────────────────────────────────

@dp.message(Command("connect"))
async def cmd_connect(message: Message, bot: Bot) -> None:
    await _send_business_connect_prompt(message, await _business_connect_text(bot))
