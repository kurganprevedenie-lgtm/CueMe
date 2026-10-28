"""Пол, источник привлечения, последнее действие юзера (middleware).

Вынесено из main.py без изменения логики (строки 3208–3563 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import hashlib
import logging
import re
from aiogram import BaseMiddleware, Bot, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from llm import transcribe_audio
from storage import (
    get_acquisition_source,
    get_gender,
    set_last_action,
    mark_bot_blocked,
    list_contacts,
    set_acquisition_source,
)
from app_state import _contact_name, dp
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── Пол пользователя ─────────────────────────────────────────────────────────
# Спрашивается НЕ сразу на /start (чтобы не мешать пройти онбординг — демо/
# JSON/Business), а сразу после того как он реально завершён (см.
# _maybe_prompt_gender, вызывается в 4 точках: подключение Business,
# первое business-сообщение, завершение демо, загрузка JSON).
# GenderGateMiddleware ниже подключает жёсткий гейт уже ПОСЛЕ этого момента —
# нужен для согласования рода в русском: и когда бот обращается к пользователю
# напрямую, и в промптах генерации (варианты ответа пишутся от первого лица
# автора — «я устал»/«я устала»).

_GENDER_LABELS = {"male": "парень", "female": "девушка"}
_GENDER_PROMPT_TEXT = "Кстати — как к тебе обращаться?"


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


@dp.callback_query(F.data.startswith("src:"))
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
            await main._finish_onboarding_chain(bot, call.message.chat.id, call.message.message_id)


def _contact_words(user_gender: str | None) -> tuple[str, str]:
    """(родительный падеж «собеседник/собеседница», притяжательное «его/её») —
    кто на другом конце «Нового диалога». Гетеро дефолт для дейтинга:
    пользователь-девушка пишет парню, пользователь-парень (или пол
    неизвестен) — девушке."""
    if user_gender == "female":
        return "собеседника", "его"
    return "собеседницы", "её"


def gender_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🙋‍♂️ Я парень", callback_data="gender:male")
    b.button(text="🙋‍♀️ Я девушка", callback_data="gender:female")
    b.adjust(2)
    return b.as_markup()


class GenderGateMiddleware(BaseMiddleware):
    """Пока пол не выбран — перехватывает любое сообщение/callback (кроме самого
    выбора пола) и показывает клавиатуру выбора вместо обычной обработки. НЕ
    вмешивается, пока онбординг не завершён (нет ни одного контакта) — чтобы
    свободно пройти демо/JSON/Business; после первого контакта _maybe_prompt_gender
    уже проактивно спросил пол, и этот гейт просто ловит тех, кто проигнорировал."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        telegram_id = str(user.id)

        if isinstance(event, CallbackQuery) and event.data in ("gender:male", "gender:female"):
            return await handler(event, data)

        if get_gender(telegram_id) is not None:
            return await handler(event, data)

        if not list_contacts(telegram_id):
            return await handler(event, data)  # онбординг ещё не завершён — не мешаем

        target = event.message if isinstance(event, CallbackQuery) else event
        if target is not None:
            await target.answer(_GENDER_PROMPT_TEXT, reply_markup=gender_kb())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return None


# ── Последнее действие юзера (для «Где уходят» в /users) ─────────────────────
# Пишется на КАЖДОЕ сообщение/нажатие в чате с ботом (не business_message —
# это сообщения собеседников, а не действия юзера). Админские команды и
# кнопки не пишем, чтобы не засорять. Регистрируется раньше
# GenderGateMiddleware — действие фиксируется, даже если тот перехватит
# событие и покажет выбор пола.

_ADMIN_COMMANDS = {
    "users", "export", "provider", "apistatus", "sources", "inspect",
    "suggestion_stats", "funnel_status", "test_reminders", "broadcast_admin",
    "broadcast_deleted_messages", "broadcast_deleted_paywall",
    "broadcast_invite", "broadcast_price_drop", "gift_premium",
}
_ADMIN_CALLBACK_PREFIXES = ("bcast:", "export:", "gift:")


def _message_action(message: Message) -> str | None:
    text = message.text or ""
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0][1:].lower()
        return None if cmd in _ADMIN_COMMANDS else f"cmd:/{cmd}"
    if message.photo:
        return "photo"
    if message.voice:
        return "voice"
    if message.document:
        return "document"
    if message.video or message.video_note:
        return "video"
    return "text" if (text or message.caption) else "other"


class LastActionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            if isinstance(event, CallbackQuery):
                action = event.data or ""
                if action.startswith(_ADMIN_CALLBACK_PREFIXES):
                    action = None
            else:
                action = _message_action(event)
            if action:
                try:
                    await asyncio.to_thread(set_last_action, str(user.id), action[:200])
                except Exception:
                    logging.exception("last_action: не удалось записать")
        return await handler(event, data)


# Человекочитаемые названия для /users. Точное совпадение — сначала, потом
# префиксы (у части callback_data в конце id контакта/действия). Неизвестное
# показывается как есть.
_ACTION_LABELS: dict[str, str] = {
    "cmd:/start": "🚀 /start", "cmd:/menu": "📋 /menu", "cmd:/help": "❓ /help",
    "cmd:/premium": "👑 /premium", "cmd:/connect": "🔌 /connect", "cmd:/reply": "💬 /reply",
    "cmd:/invite": "👥 /invite", "cmd:/redeem": "🎟 /redeem", "cmd:/myref": "👥 /myref",
    "cmd:/contacts": "📋 /contacts", "cmd:/progress": "⏳ /progress", "cmd:/gender": "🙋 /gender",
    "cmd:/deep_analysis": "🔬 /deep_analysis", "cmd:/rebuild": "🔄 /rebuild",
    "cmd:/delete": "🗑 /delete", "cmd:/wipe": "🗑 /wipe",
    "text": "✍️ Прислал текст", "photo": "🖼 Прислал фото", "voice": "🎙 Прислал голосовое",
    "document": "📎 Прислал файл", "video": "🎬 Прислал видео", "other": "📦 Прислал что-то другое",
    "mm:unified": "💬 Ответ с CueMe (меню)", "mm:deep": "🔬 Анализ собеседника (меню)",
    "mm:date": "💡 Идеальное свидание (меню)", "mm:support": "🆘 Помощь (меню)",
    "watch:menu": "🗑 Удалённые сообщения (меню)", "watch:locked": "🔒 Удалённые сообщения: под замком",
    "show_premium": "👑 Подписка", "show_invite": "👥 Реферальная система",
    "sub:to_menu": "⬅️ Назад", "back_to_menu": "⬅️ Вернуться в меню",
    "stars_menu": "⭐ Оплатить Stars", "stars_back": "⬅️ Назад из Stars",
    "stars_buy:day": "⭐ Купить день", "stars_buy:week": "⭐ Купить неделю",
    "stars_buy:month": "⭐ Купить месяц",
    "promo:offer": "📢 Подписаться на канал (из Подписки)", "promo:check": "✅ Я подписался (из Подписки)",
    "gender:male": "🙋‍♂️ Выбрал пол: парень", "gender:female": "🙋‍♀️ Выбрал пол: девушка",
    "src:friend": "📍 Источник: друг", "src:instagram": "📍 Источник: Instagram",
    "src:tiktok": "📍 Источник: TikTok", "src:youtube": "📍 Источник: YouTube",
    "src:tgchat": "📍 Источник: Telegram-чат", "src:other": "📍 Источник: другое",
    "qs:yes": "💬 Попробовать ответ (напоминание)", "qs:no": "🙅 Не сейчас (напоминание)",
    "qsphr:her": "🎲 Фразы для неё", "qsphr:him": "🎲 Фразы для него",
    "live:coach": "🎯 Живой коучинг", "live:phrases": "🎲 Готовые фразы",
    "phrases:her": "🎲 Фразы для неё", "phrases:him": "🎲 Фразы для него",
    "revive_next": "🔄 Другой вопрос", "unified_contact:new": "➕ Новый собеседник",
    "unified_reply_restart": "👥 Ответ для другого собеседника",
    "delrev_premium": "👑 Premium с тизера удалённых", "delrev_all": "👀 Показать все удалённые",
    "delall": "🗑 Удалить все данные", "delallyes": "🗑 Удалить все данные: да",
    "delno": "↩️ Удаление: отмена", "wipeno": "↩️ Очистка: отмена",
}
_ACTION_LABEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("unlock:check:", "✅ Я подписался (гейт)"),
    ("unlock:reveal:", "👀 Показать после подписки (гейт)"),
    ("watch:toggle:", "🗑 Переключил настройку удалённых"),
    ("deepan_refresh:", "🔄 Обновить анализ"),
    ("deepan:", "🔬 Анализ собеседника: выбрал контакт"),
    ("idealdate_refresh:", "🔄 Другая идея свидания"),
    ("idealdate:", "💡 Идеальное свидание: выбрал контакт"),
    ("unified_contact:", "💬 Ответ с CueMe: выбрал контакт"),
    ("reply:", "💬 Ответить: выбрал контакт"),
    ("mystyle:", "🎯 Мой стиль: выбрал контакт"),
    ("setup:", "⚙️ Настройка контакта"),
    ("varregen:", "🔄 Другие варианты ответа"),
    ("liveregen:", "🔄 Другие варианты (живой диалог)"),
    ("qsphr_next:", "🔄 Другая фраза"), ("phrase_next:", "🔄 Другая фраза"),
    ("delrev:", "👀 Показать удалённое"), ("del:", "🗑 Удалить контакт"),
    ("delyes:", "🗑 Удалить контакт: да"), ("wipeyes:", "🗑 Очистка: да"),
)


def _action_label(raw: str | None) -> str:
    if not raw:
        return "—"
    if raw in _ACTION_LABELS:
        return _ACTION_LABELS[raw]
    for prefix, label in _ACTION_LABEL_PREFIXES:
        if raw.startswith(prefix):
            return label
    return raw


dp.message.outer_middleware(LastActionMiddleware())
dp.callback_query.outer_middleware(LastActionMiddleware())
dp.message.outer_middleware(GenderGateMiddleware())
dp.callback_query.outer_middleware(GenderGateMiddleware())


# style_pick_kb/_auto_style_for_ctx/style_result_kb (точечный выбор одного стиля
# после показа вариантов, кнопка «Другой тон») убраны вместе с ней — см. main.py
# variants_result_kb ниже. Точечный выбор стиля больше нигде не используется.


def _style_cache_key(
    kind: str, style: str, text: str, style_card: str, interaction_card: str, extra: str = "",
) -> str:
    """Контент-адресный ключ кэша: включает карточки стиля, поэтому при их пересборке
    ключ меняется сам (авто-инвалидация без TTL-гонок). extra — доп. фактор,
    меняющий генерацию (например пол автора), не завязанный на карточки."""
    raw = "\x00".join([kind or "", style or "", text or "", style_card or "", interaction_card or "", extra or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _msg_meta(text: str | None, is_voice: bool = False) -> dict:
    meta = {"length": len(text) if text else 0, "has_emoji": bool(text) and bool(_EMOJI_RE.search(text))}
    if is_voice:
        meta["voice"] = True
    return meta


async def _message_text(bot: Bot, event: Message) -> tuple[str | None, bool]:
    """Возвращает (текст, было_голосовое). Голосовое расшифровывается через Whisper."""
    text = event.text or event.caption
    if text:
        return text, False
    media = event.voice or event.audio
    if media:
        try:
            buf = await bot.download(media)
            transcript = await transcribe_audio(buf.read(), "voice.ogg")
            if transcript:
                logging.info("voice transcribed: %d символов", len(transcript))
                return transcript, True
            logging.warning("voice: пустая транскрипция")
        except Exception:
            logging.exception("voice: не удалось скачать/расшифровать")
    return None, False


def _not_command(message: Message) -> bool:
    """True если сообщение НЕ похоже на слэш-команду. Команды (/premium, /help
    и т.п.) должны срабатывать даже посреди пересылки сообщений в «Ответить за
    меня»/«Живом диалоге» — иначе они проглатываются этими режимами (состояние
    там нарочно не сбрасывается между сообщениями) и юзер не может проверить
    статус или выйти иначе как кнопкой меню."""
    return not (message.text or "").startswith("/")
