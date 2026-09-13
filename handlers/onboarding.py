"""Онбординг: /start (в т.ч. реферальная ссылка), вопросы про источник и пол,
цепочка «бот подключён → как узнал → как обращаться → есть с кем переписка»,
инструкция по подключению Автоматизации чатов (/connect) и импорт
JSON-экспорта переписки.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
import html
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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
from handlers.common import (
    Setup,
    _GENDER_LABELS,
    _GENDER_PROMPT_TEXT,
    _contact_name,
    _require_premium,
    _send_main_menu,
    _with_back_to_menu,
    contacts_kb,
    gender_kb,
)
from handlers.referral import _credit_referral_if_pending
from handlers.reply_flow import _pick_no_repeat, _start_unified_reply
from llm import make_features_summary, sample_texts
from services.cards import _gen_interaction_card, _gen_style_card
from storage import (
    delete_style_card,
    get_acquisition_source,
    get_contact_by_id,
    get_gender,
    get_or_create_contact,
    get_referrer_by_code,
    get_user,
    list_contacts,
    mark_bot_blocked,
    mark_bot_unblocked,
    record_event,
    save_imported_messages,
    save_message_samples,
    save_referral_pending,
    set_acquisition_source,
    set_gender,
    upsert_user,
)
from tg_parser import parse_chat

router = Router(name="onboarding")


async def _maybe_prompt_gender(
    bot: Bot,
    telegram_id: str,
    *,
    edit_chat_id: int | None = None,
    edit_message_id: int | None = None,
) -> bool:
    """Спрашивает пол один раз, сразу после реального завершения онбординга
    (первый контакт создан — демо/JSON/Business). Идемпотентно — no-op, если
    уже спрашивали/выбрали. Если передан edit_chat_id/edit_message_id
    (Business-цепочка — см. cb_source_select) — редактирует ЭТО сообщение
    вместо отправки нового, чтобы вся цепочка онбординг-вопросов оставалась
    одним сообщением. Возвращает True, если вопрос реально показан (отправлен
    или отредактирован) — вызывающий код (cb_source_select) использует это,
    чтобы понять, нужно ли сразу переходить к следующему шагу цепочки."""
    if get_gender(telegram_id) is not None:
        return False
    try:
        if edit_chat_id is not None and edit_message_id is not None:
            await bot.edit_message_text(
                _GENDER_PROMPT_TEXT,
                chat_id=edit_chat_id,
                message_id=edit_message_id,
                reply_markup=gender_kb(),
            )
        else:
            await bot.send_message(int(telegram_id), _GENDER_PROMPT_TEXT, reply_markup=gender_kb())
    except TelegramForbiddenError:
        mark_bot_blocked(telegram_id)
        return False
    except Exception:
        logging.warning("gender prompt failed: telegram_id=%s", telegram_id)
        return False
    return True


# ── Источник привлечения ──────────────────────────────────────────────────────
# Спрашивается ТОЛЬКО на Business-пути (Автоматизация чатов), сразу после
# подключения (handle_business_connection) — ПЕРЕД вопросом про пол. Демо и
# JSON-путь этот вопрос не показывают вообще.

_SOURCE_PROMPT_TEXT = "Кстати, как ты о нас узнал?"


def source_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 TikTok", callback_data="src:tiktok")],
        [InlineKeyboardButton(text="📸 Instagram", callback_data="src:instagram")],
        [InlineKeyboardButton(text="📺 YouTube", callback_data="src:youtube")],
        [InlineKeyboardButton(text="💬 С чата в Telegram", callback_data="src:tgchat")],
        [InlineKeyboardButton(text="👥 От друга", callback_data="src:friend")],
        [InlineKeyboardButton(text="🤷 Другое", callback_data="src:other")],
    ])


async def _maybe_prompt_source(bot: Bot, telegram_id: str) -> Message | None:
    """Спрашивает источник один раз, идемпотентно — no-op если уже отвечал.
    Отдельное (новое) сообщение — попытка editить сюда же сообщение "✅
    Готово, бот подключён!" (отправленное с main_kb(), reply-клавиатурой)
    не сработала на живом тесте: Telegram не даёт добавить inline-кнопки
    через edit_message_text сообщению, изначально отправленному с обычной
    (reply) клавиатурой. Это НАЧАЛО editable-цепочки — дальше (источник →
    пол → квикстарт) редактируется именно это сообщение, см.
    cb_source_select/cb_gender_select/_finish_onboarding_chain."""
    if get_acquisition_source(telegram_id) is not None:
        return None
    try:
        return await bot.send_message(int(telegram_id), _SOURCE_PROMPT_TEXT, reply_markup=source_kb())
    except TelegramForbiddenError:
        mark_bot_blocked(telegram_id)
    except Exception:
        logging.warning("source prompt failed: telegram_id=%s", telegram_id)
    return None


@router.callback_query(F.data.startswith("src:"))
async def cb_source_select(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    source = call.data.split(":", 1)[1]
    set_acquisition_source(telegram_id, source)
    await call.answer()
    # Пол спрашиваем только теперь — после того как юзер реально ответил на
    # вопрос про источник, а не одновременно с ним. Editим ЭТО ЖЕ сообщение
    # (было: delete + отдельное новое сообщение) — вся цепочка онбординг-
    # вопросов остаётся одним редактируемым сообщением.
    shown = await _maybe_prompt_gender(
        bot, telegram_id,
        edit_chat_id=call.message.chat.id,
        edit_message_id=call.message.message_id,
    )
    if not shown:
        # Пол уже известен (редкий кейс) — вопрос про пол пропускаем и сразу
        # переходим к финальному шагу цепочки.
        if list_contacts(telegram_id):
            try:
                await call.message.delete()
            except Exception:
                pass
        else:
            await _finish_onboarding_chain(bot, call.message.chat.id, call.message.message_id)


# ── /start ────────────────────────────────────────────────────────────────────

def _quickstart_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Да, есть", callback_data="qs:yes"),
        InlineKeyboardButton(text="🤷 Пока никого", callback_data="qs:no"),
    ]])




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


@router.callback_query(F.data == "qs:yes")
async def cb_quickstart_yes(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _start_unified_reply(call.message, state)


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


@router.callback_query(F.data == "qs:no")
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


async def _send_quickstart_phrases(msg: Message, state: FSMContext, target: str) -> None:
    """Своя, изолированная от phrases:*, ветка — переиспользует только
    ДАННЫЕ (OPENERS_FOR_HER/HIM), не общий код _send_opener/cb_phrases_gender,
    чтобы не задеть существующий путь «🎲 Готовые фразы для начала»."""
    items = OPENERS_FOR_HER if target == "her" else OPENERS_FOR_HIM
    phrase = await _pick_no_repeat(state, f"qs_opener_shown_{target}", items)
    intro = "Вот пара фраз для начала — сохрани, пригодятся:"
    if "[" in phrase:
        intro += " замени [то, что в скобках] на реальную деталь из анкеты."
    text = f"{intro}\n\n<code>{html.escape(phrase)}</code>\n\nПосле того как найдешь себе партнера возвращайся и пиши в 💬 Ответ с CueMe"
    await msg.answer(text, parse_mode="HTML", reply_markup=_quickstart_phrase_next_kb(target))


@router.callback_query(F.data.startswith("qsphr:"))
async def cb_quickstart_gender(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]  # her | him
    await call.answer()
    await _send_quickstart_phrases(call.message, state, target)


@router.callback_query(F.data.startswith("qsphr_next:"))
async def cb_quickstart_phrase_next(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]
    await call.answer("Другой вариант")
    await _send_quickstart_phrases(call.message, state, target)


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


async def _send_business_connect_prompt(target: Message, text: str) -> None:
    """Инструкция по подключению Автоматизации чатов — со скриншотом, куда
    именно нажимать в настройках Telegram, если задан ONBOARDING_PHOTO_PATH/
    ONBOARDING_PHOTO_FILE_ID (.env), иначе просто текст. Тот же приоритет
    файл-на-диске → file_id → голый текст, что и в _send_start_menu — общий
    хелпер, чтобы cb_onboarding_business и cmd_connect не дублировали его."""
    photo_path = Path(ONBOARDING_PHOTO_PATH) if ONBOARDING_PHOTO_PATH else None
    if photo_path and photo_path.is_file():
        await target.answer_photo(
            photo=FSInputFile(photo_path),
            caption=text,
            reply_markup=business_connect_kb(),
        )
    elif ONBOARDING_PHOTO_FILE_ID:
        await target.answer_photo(
            photo=ONBOARDING_PHOTO_FILE_ID,
            caption=text,
            reply_markup=business_connect_kb(),
        )
    else:
        await target.answer(text, reply_markup=business_connect_kb())


async def _send_start_menu(message: Message, telegram_id: str) -> None:
    if list_contacts(telegram_id):
        await _send_main_menu(message)
        return

    me = await message.bot.get_me()
    welcome_text = (
        "👋 Добро пожаловать в CueMe!\n\n"
        "<blockquote>❓ Подключить бота:\n"
        "1. Настройки → «Изменить» рядом с профилем\n"
        "2. Автоматизация чатов\n"
        f"3. Впиши @{me.username} и выбери меня\n"
        "4. Включи «Ответы на сообщения» и выбери чаты</blockquote>"
    )

    # Единственное сообщение при первом /start — фото-инструкция крепится
    # к нему caption'ом. Приоритет: 1) файл на диске сервера
    # (ONBOARDING_PHOTO_PATH) — грузится в Telegram заново при каждой
    # отправке; 2) file_id (уже загруженное ранее фото); 3) обычный текст,
    # если ни одно из двух не задано. Больше НИЧЕГО следом не шлём —
    # намеренно, чтобы не отвлекать от единственного действия (подключить).
    photo_path = Path(ONBOARDING_PHOTO_PATH) if ONBOARDING_PHOTO_PATH else None
    if photo_path and photo_path.is_file():
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=welcome_text,
            parse_mode="HTML",
            reply_markup=business_connect_kb(),
        )
    elif ONBOARDING_PHOTO_FILE_ID:
        await message.answer_photo(
            photo=ONBOARDING_PHOTO_FILE_ID,
            caption=welcome_text,
            parse_mode="HTML",
            reply_markup=business_connect_kb(),
        )
    else:
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=business_connect_kb())


@router.message(CommandStart())
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
        try:
            await bot.send_message(int(ADMIN_GROUP_CHAT_ID), f"🆕 Новый пользователь: {who}")
        except Exception:
            logging.warning("admin-group new-user notify failed: %s", who)


@router.callback_query(F.data.in_({"gender:male", "gender:female"}))
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


@router.message(Command("gender"))
async def cmd_gender(message: Message) -> None:
    await message.answer("Как теперь к тебе обращаться?", reply_markup=gender_kb())


@router.callback_query(F.data == "onb:business")
async def cb_onboarding_business(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await call.answer()
    telegram_id = str(call.from_user.id)
    upsert_user(telegram_id, f"user{call.from_user.id}")
    await _send_business_connect_prompt(call.message, await _business_connect_text(bot))


@router.callback_query(F.data == "onb:json")
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


# ── Загрузка JSON-файла ───────────────────────────────────────────────────────

@router.message(F.document)
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

@router.callback_query(F.data.startswith("setup:"))
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

@router.message(Command("connect"))
async def cmd_connect(message: Message, bot: Bot) -> None:
    await _send_business_connect_prompt(message, await _business_connect_text(bot))
