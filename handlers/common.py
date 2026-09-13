"""Общий слой хендлеров: то, что нужно СРАЗУ НЕСКОЛЬКИМ функциональным
модулям — константы кнопок, FSM-состояния, доступ (Premium/триал/пейволл),
мелкие хелперы форматирования и общие клавиатуры.

Правило зависимостей: этот модуль не импортирует другие handlers/* (иначе
циклы) — только config/storage/llm. Всё, что нужно ровно одному хендлеру,
живёт в его собственном модуле, а не здесь.

Код перенесён из main.py без изменений (структурный рефакторинг).
"""
import hashlib
import itertools
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    ADMIN_TELEGRAM_IDS,
    FREE_TRIAL_REQUESTS,
    PREMIUM_CACHE_TTL,
    PREMIUM_CHANNEL_ID,
    PREMIUM_SUBSCRIBE_URL,
    STAR_PRICE_DAY,
    STAR_PRICE_WEEK,
    STAR_PRICE_MONTH,
)
from llm import transcribe_audio
from storage import (
    get_deep_analysis_free_until,
    get_gender,
    get_latest_star_payment,
    get_promo_channel_premium_until,
    get_stars_premium_until,
    get_trial_used,
    increment_trial_used,
    list_contacts,
)


def _is_admin(telegram_id: str | int) -> bool:
    """Единая проверка доступа к админ-командам (/provider, /users, /export,
    /sources и т.п.) — по множеству ADMIN_TELEGRAM_IDS (включает
    ADMIN_TELEGRAM_ID, если задан)."""
    return bool(ADMIN_TELEGRAM_IDS) and str(telegram_id) in ADMIN_TELEGRAM_IDS


# BTN_SCREENSHOT/BTN_REPLY/BTN_LIVE — объединены в BTN_UNIFIED (см. ниже),
# из main_kb() убраны. Константы и старые ветки-обработчики оставлены
# закомментированными (не удалены физически) — на случай отката.
# BTN_SCREENSHOT    = "📸 По скриншоту"
# BTN_REPLY         = "💬 Ответить за меня"
# BTN_LIVE          = "💫 Новый диалог"
BTN_UNIFIED       = "💬 Ответ с CueMe"
BTN_DEEP          = "🔬 Анализ собеседника"
# BTN_DEEP_STYLE («🪞 Анализ своего стиля») убрана совсем по запросу — вместе
# со всей веткой (_gen_deep_style_analysis/_format_deep_style_analysis/
# _run_deep_style_analysis/_show_deep_style_analysis, /deep_style_analysis,
# deep_style_analysis-таблица в storage.py, build_deep_style_analysis в
# llm.py). Остаётся только «Анализ собеседника».
BTN_DATE          = "💐 Идеальное свидание"
# BTN_REVIVE («🔥 Скрипты общения») убрана совсем из главного меню — была
# внутри BTN_MORE, который тоже убран. _show_revive/cb_revive_next/
# REVIVE_QUESTIONS не удалены физически, просто больше не достижимы.
# BTN_REVIVE        = "🔥 Скрипты общения"
# BTN_INVITE («🎁 Пригласить друга») тоже была только внутри BTN_MORE —
# приглашение друга доступно через «👑 Подписка» (premium_menu_kb) и /invite.
# BTN_INVITE        = "🎁 Пригласить друга"
# BTN_ANALYZE («🔬 Разобраться») убрана совсем по запросу — раньше открывала
# инлайн-подменю (analyze_menu_kb) с единственной кнопкой BTN_DEEP (после
# того как «Анализ своего стиля» убрали, подменю на один пункт стало лишним
# тапом) — теперь BTN_DEEP прямо на главном экране, без промежуточного шага.
# BTN_MORE («⚙️ Ещё») убрана — «Идеальное свидание» стала кнопкой первого
# уровня, «Пригласить друга» доступно через «👑 Подписка»/командой /invite,
# «Скрипты общения» убраны совсем (см. BTN_REVIVE выше). more_menu_kb()
# оставлена закомментированной ниже — на случай отката.
# BTN_MORE          = "⚙️ Ещё"
BTN_SUBSCRIPTION  = "👑 Подписка"
# BTN_HELP («❓ Помощь», /help — полный список команд) НЕ на главном меню —
# доступна только командой /help. BTN_SUPPORT ниже — пятая кнопка главного
# меню (тоже «Помощь», разные эмодзи) — ведёт туда же (_show_help), с
# кнопкой на @CueMeSupport внизу того же сообщения (support_kb).
BTN_HELP          = "❓ Помощь"
BTN_SUPPORT       = "🆘 Помощь"
# BTN_ME («👤 Мой стиль») убрана вместе с командой /me — дублировала
# «Анализ своего стиля» (и была бесплатной лазейкой мимо подписки на неё;
# сам «Анализ своего стиля» тоже убран совсем, см. пометку у BTN_DEEP выше).
# BTN_MY_STYLE_FOR («🎯 Мой стиль с ним») убрана из меню, но _show_my_style_for
# не удалена — можно вернуть кнопку одной правкой.
# BTN_CONTACT («🔍 Стиль собеседника») удалена совсем — её interaction_card
# теперь блоком внутри «Анализ собеседника» (_format_deep_analysis). BTN_CONTACTS
# («📋 Контакты») убрана из меню — доступна только как команда /contacts.
# BTN_REWRITE («📝 Переписать») и /auto удалены совсем — их сценарий (черновик
# без привязки к входящему) теперь полностью закрывает «💫 Новый диалог».
_ALL_BTNS = {
    BTN_UNIFIED, BTN_DEEP, BTN_DATE, BTN_SUBSCRIPTION, BTN_HELP, BTN_SUPPORT,
}


# Контекст действий (черновик/входящее/скриншот + выбранный стиль) — по user_id,
# и ВНУТРИ каждого юзера ещё и по action_id (не один слот, а словарь слотов).
# Нужно, чтобы параллельные генерации одного юзера (форварднул несколько сообщений
# подряд в «Ответить за меня», не дождавшись выбора стиля для первого — или у него
# включён авто-режим и он написал что-то ещё, пока не выбрал стиль скриншота) не
# затирали друг друга. action_id зашивается в callback_data (stylepick:<style>:<id>
# и т.п.), поэтому каждая клавиатура «привязана» к своему слоту, а не к «последнему».
_last_action: dict[int, dict[str, dict]] = {}
_action_seq = itertools.count(1)
_ACTION_TTL_SEC = 3600  # брошенные на середине слоты чистятся лениво при следующем действии юзера


def _new_action(user_id: int, ctx: dict) -> str:
    """Заводит новый слот действия для юзера, возвращает action_id для callback_data.
    Заодно чистит слоты этого юзера старше _ACTION_TTL_SEC, чтобы словарь не рос
    бесконечно у тех, кто бросает флоу на середине."""
    action_id = str(next(_action_seq))
    ctx["_ts"] = time.monotonic()
    slots = _last_action.setdefault(user_id, {})
    now = time.monotonic()
    for stale_id in [aid for aid, c in slots.items() if now - c.get("_ts", now) > _ACTION_TTL_SEC]:
        del slots[stale_id]
    slots[action_id] = ctx
    return action_id


def _get_action(user_id: int, action_id: str) -> dict | None:
    return _last_action.get(user_id, {}).get(action_id)


def _contact_name(c) -> str:
    name     = c["display_name"] or ""
    username = c["username"] or "" if "username" in c.keys() else ""
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or c["contact_alias"]


TELEGRAM_MAX_LEN = 4096  # лимит Telegram на длину одного сообщения


def _split_long_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Режет текст на части ≤ limit символов, по возможности по границам
    абзацев/строк — LLM-карточки (style_card и т.п.) иногда длиннее лимита
    Telegram и без этого падают с TelegramBadRequest «message is too long»."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def _answer_long(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Как message.answer(), но безопасно для текста длиннее лимита Telegram —
    клавиатура (если есть) уходит с последним куском. _split_long_text режет по
    границам абзацев, поэтому HTML-теги внутри одного абзаца (см. _format_variants)
    не рвутся посередине, пока сам абзац короче лимита."""
    chunks = _split_long_text(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=reply_markup if last else None, parse_mode=parse_mode)


async def _edit_or_answer_long(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Как call.message.edit_text(), но при переполнении лимита Telegram первый
    кусок идёт в edit, а остальные — отдельными сообщениями (edit не может
    «раздвоиться» на несколько сообщений). reply_markup — с ПОСЛЕДНИМ куском,
    как в _answer_long."""
    chunks = _split_long_text(text)
    last = len(chunks) - 1
    await message.edit_text(chunks[0], reply_markup=reply_markup if last == 0 else None, parse_mode=parse_mode)
    for i, chunk in enumerate(chunks[1:], start=1):
        await message.answer(chunk, reply_markup=reply_markup if i == last else None, parse_mode=parse_mode)


# ── Подписка (Tribute) ──────────────────────────────────────────────────────
# Пропуск — членство в приватном канале, которым управляет Tribute (добавляет
# при оплате, убирает при отмене/неоплате). Бот только читает текущий статус.

_premium_cache: dict[str, tuple[bool, float]] = {}  # telegram_id -> (is_premium, checked_at)


async def _is_premium(bot: Bot, telegram_id: str) -> bool:
    """Проверяет членство в PREMIUM_CHANNEL_ID с кэшем на PREMIUM_CACHE_TTL сек,
    чтобы не дёргать Telegram API на каждое сообщение. Пока PREMIUM_CHANNEL_ID
    не настроен — всегда False (только бесплатные попытки). Реферальная
    награда (_has_referral_premium), награда за подписку на промо-канал
    (_has_promo_channel_premium) и оплата Telegram Stars (_has_stars_premium)
    дают полный Premium в обход канала — Stars НЕ добавляет в приватный канал,
    это независимое окно доступа, см. users.stars_premium_until."""
    if _has_referral_premium(telegram_id):
        return True
    if _has_promo_channel_premium(telegram_id):
        return True
    if _has_stars_premium(telegram_id):
        return True
    if not PREMIUM_CHANNEL_ID:
        return False

    cached = _premium_cache.get(telegram_id)
    if cached and time.monotonic() - cached[1] < PREMIUM_CACHE_TTL:
        return cached[0]

    try:
        member = await bot.get_chat_member(PREMIUM_CHANNEL_ID, int(telegram_id))
        is_prem = member.status in ("member", "administrator", "creator")
    except Exception:
        is_prem = False

    _premium_cache[telegram_id] = (is_prem, time.monotonic())
    return is_prem


def paywall_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if PREMIUM_SUBSCRIBE_URL:
        b.button(text="💎 Оформить подписку", url=PREMIUM_SUBSCRIBE_URL)
    b.button(text="⭐ Оплатить Stars", callback_data="stars_menu")
    b.adjust(1)
    return b.as_markup()


def premium_menu_kb() -> InlineKeyboardMarkup:
    """Клавиатура под карточкой «👑 Подписка»: оформить (Tribute) + оплата
    Stars прямо в Telegram + два бесплатных пути (реферальная награда и
    подписка на промо-канал) + «⬅️ Назад» в главное меню. Кнопка
    «🎁 Пригласи друга» переименована в «👥 Реферальная система» — тот же
    callback_data="show_invite", просто название под новую иерархию экранов
    (Подписка → Реферальная система)."""
    b = InlineKeyboardBuilder()
    if PREMIUM_SUBSCRIBE_URL:
        b.button(text="💎 Оформить подписку", url=PREMIUM_SUBSCRIBE_URL)
    b.button(text="⭐ Оплатить Stars", callback_data="stars_menu")
    b.button(text="👥 Реферальная система", callback_data="show_invite")
    b.button(text="📢 Подписаться на канал", callback_data="promo:offer")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


def stars_tariff_kb() -> InlineKeyboardMarkup:
    """Тарифы Stars: день/неделя — разовая покупка, месяц — нативная
    Stars-подписка с автопродлением (помечена отдельно, т.к. отменяется
    иначе — через настройки Telegram, не через бота)."""
    b = InlineKeyboardBuilder()
    b.button(text=f"⭐ День — {STAR_PRICE_DAY} Stars", callback_data="stars_buy:day")
    b.button(text=f"⭐ Неделя — {STAR_PRICE_WEEK} Stars", callback_data="stars_buy:week")
    b.button(text=f"⭐ Месяц (автопродление) — {STAR_PRICE_MONTH} Stars", callback_data="stars_buy:month")
    b.button(text="‹ Назад", callback_data="stars_back")
    b.adjust(1)
    return b.as_markup()


async def _send_paywall(target: Message, text: str, edit: bool = False) -> None:
    """edit=True — редактирует ТО ЖЕ сообщение (например, главное меню или
    экран с контактом), а не шлёт отдельное новое — вызывающий код передаёт
    edit только когда точно знает, что target это сообщение БОТА (иначе
    edit_text упадёт на чужом/юзерском сообщении)."""
    if edit:
        await target.edit_text(text, reply_markup=premium_menu_kb())
    else:
        await target.answer(text, reply_markup=premium_menu_kb())


async def _has_quota(bot: Bot, telegram_id: str) -> bool:
    """Есть ли доступ к генерации: premium или остались бесплатные попытки. Без списания."""
    if await _is_premium(bot, telegram_id):
        return True
    return get_trial_used(telegram_id) < FREE_TRIAL_REQUESTS


async def _quota_gate(bot: Bot, target: Message, telegram_id: str, edit: bool = False) -> bool:
    """Проверка доступа БЕЗ списания. Если попытки кончились — показывает пейволл.
    Списание делает _charge_trial_if_needed уже ПОСЛЕ успешной генерации."""
    if await _has_quota(bot, telegram_id):
        return True
    await _send_paywall(
        target,
        "Бесплатные попытки закончились — но, похоже, тебе заходит 😏 Дальше — "
        "по подписке: весь функционал плюс полный разбор собеседника с подарками.",
        edit=edit,
    )
    return False


async def _charge_trial_if_needed(bot: Bot, telegram_id: str) -> None:
    """Списывает одну попытку триала. Вызывать ТОЛЬКО после успешного ответа LLM.
    Premium попытки не тратит."""
    if await _is_premium(bot, telegram_id):
        return
    increment_trial_used(telegram_id)


async def _require_premium(bot: Bot, target: Message, telegram_id: str, edit: bool = False) -> bool:
    """Гейт для функций без бесплатного триала (анализ собеседника, стиль
    собеседника и т.п.) — доступ только по активной подписке."""
    if await _is_premium(bot, telegram_id):
        return True

    await _send_paywall(target, "Эта функция доступна только по подписке CueMe Premium.", edit=edit)
    return False


def _has_referral_premium(telegram_id: str) -> bool:
    """Активно ли реферальное окно полной Premium-подписки."""
    until = get_deep_analysis_free_until(telegram_id)
    return bool(until and until > datetime.now(timezone.utc))


def _has_promo_channel_premium(telegram_id: str) -> bool:
    """Активно ли окно Premium за подписку на промо-канал (PROMO_CHANNEL_USERNAME —
    ПУБЛИЧНЫЙ канал, не путать с приватным PREMIUM_CHANNEL_ID/Tribute)."""
    until = get_promo_channel_premium_until(telegram_id)
    return bool(until and until > datetime.now(timezone.utc))


def _has_stars_premium(telegram_id: str) -> bool:
    """Активно ли окно Premium, купленное за Telegram Stars — независимо от
    членства в приватном канале Tribute (см. _run_stars_successful_payment)."""
    until = get_stars_premium_until(telegram_id)
    return bool(until and until > datetime.now(timezone.utc))


_BACK_TO_MENU_BUTTON = InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="back_to_menu")


def _with_back_to_menu(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавляет строку «⬅️ Вернуться в меню» под уже собранной клавиатурой
    результата генерации (Ответ с CueMe / Анализ собеседника / Идеальное
    свидание) — не трогает остальные кнопки той клавиатуры."""
    markup.inline_keyboard.append([_BACK_TO_MENU_BUTTON])
    return markup


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


# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    waiting_for_json    = State()
    waiting_for_contact = State()

class ReplyHelp(StatesGroup):
    waiting_for_incoming = State()

# Screenshot (FSM для отдельной команды /screenshot) убрана вместе со всей
# функцией «скриншот переписки → ответ» — см. пометку у секции «Ответить по
# скриншоту» ниже. Класс оставлен закомментированным — некоторые функции по
# соседству (_run_variants_generation) раньше ссылались на него.
# class Screenshot(StatesGroup):
#     waiting_for_image = State()

class LiveDialogue(StatesGroup):
    waiting_for_name     = State()
    waiting_for_incoming = State()

class UnifiedReply(StatesGroup):
    """«💬 Ответ с CueMe» — единая точка входа вместо БТН_REPLY/BTN_LIVE
    (BTN_SCREENSHOT была третьей — фото-вход в эту же точку убран вместе с
    функцией «скриншот переписки → ответ», см. handle_unified_input): текст/
    форвард → определение контакта → приводит к одному из существующих
    пайплайнов (ReplyHelp для существующего контакта, LiveDialogue для
    нового)."""
    waiting_for_input = State()
    waiting_for_name  = State()


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


def _ru_days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "дня"
    return "дней"


def _format_remaining(delta: timedelta) -> str:
    """«3 дня» / «18 часов» / «меньше часа» — дни, если остались хотя бы
    сутки, иначе часы (по условию задачи: дни и часы, если осталось меньше
    суток — часов одних достаточно, до минут не мельчим)."""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "меньше минуты"
    days = total_seconds // 86400
    if days >= 1:
        return f"{days} {_ru_days_word(days)}"
    hours = total_seconds // 3600
    if hours >= 1:
        hour_word = "час" if hours == 1 else ("часа" if 2 <= hours <= 4 else "часов")
        return f"{hours} {hour_word}"
    return "меньше часа"


_RU_MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _format_until(dt: datetime) -> str:
    """«25 августа 2026, 18:29 UTC» — конкретная дата и время окончания, а не
    только относительный остаток (по прямой просьбе — юзер прислал скриншот
    официальной Telegram-квитанции с таким форматом, «будет действовать до
    25 Aug 2026 18:29:34 UTC», и попросил такую же конкретику у нас; месяц —
    по-русски, без английских сокращений, секунды опущены как лишняя точность
    для UI)."""
    return f"{dt.day} {_RU_MONTHS_GEN[dt.month]} {dt.year}, {dt.strftime('%H:%M')} UTC"


def _premium_expiry_info(telegram_id: str) -> tuple[str, datetime | None, bool]:
    """(источник, until, is_subscription) — общий разбор ДЛЯ ЛЮБОГО активного
    Premium, переиспользуется и в тексте «Подписка» (_premium_expiry_line), и
    в экспорте /users (_collect_users_data): раньше там был отдельный, более
    бедный расчёт premium_source, который не проверял Stars вообще и всё, что
    не реферал/промо-канал, молча относил к Tribute — эта функция единая
    точка правды и для текста, и для экспорта.

    Порядок проверки — тот же приоритет, что уже использует _is_premium
    (реферал → промо-канал → Stars → членство в канале Tribute): источник
    "tribute" возвращается, только если ни один из первых трёх не дал
    активного окна — это ПОДРАЗУМЕВАЕТ, что _is_premium(telegram_id) уже
    True (иначе Premium вообще нет, вызывать не нужно). until=None только
    для Tribute — точной даты окончания бот не знает (Tribute продлевает/
    отменяет подписку на своей стороне, боту доступен только факт членства
    через get_chat_member). is_subscription — True только для Stars-тарифа
    "месяц" (нативная подписка с автопродлением), иначе False."""
    now = datetime.now(timezone.utc)

    until = get_deep_analysis_free_until(telegram_id)
    if until and until > now:
        return "referral", until, False

    until = get_promo_channel_premium_until(telegram_id)
    if until and until > now:
        return "promo_channel", until, False

    until = get_stars_premium_until(telegram_id)
    if until and until > now:
        payment = get_latest_star_payment(telegram_id)
        return "stars", until, bool(payment and payment["is_subscription"])

# ── Экран главного меню ──────────────────────────────────────────────────────
# Живёт в общем слое, а не в handlers/main_menu.py, потому что на него
# ссылаются в обе стороны: main_menu показывает его, а «⬅️ Назад» из
# «Подписки» (handlers/subscription.py) и _send_start_menu (onboarding)
# редактируют/шлют его же — держать здесь дешевле, чем разруливать цикл
# импортов между модулями хендлеров.

_MAIN_MENU_TEXT = (
    "👋 Вот что я умею:\n\n"
    "💬 Ответ с CueMe — подскажу, что написать в моменте\n"
    "🔬 Анализ собеседника — разберу вашу переписку по фактам\n"
    "💐 Идеальное свидание — накидаю идеи для свидания"
)


def main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню — 5 пунктов, той же вёрстки/механики, что «Подписка»
    (premium_menu_kb): inline-кнопки на одном сообщении, редактируемом при
    переходах. «👑 Подписка» ведёт в уже существующую edit-in-place иерархию
    (callback_data="show_premium" — тот же, что и «⬅️ Назад» из Реферальной
    системы, см. cb_show_premium). Остальные — одноразовые действия
    (мультишаговые FSM-флоу/результаты своим сообщением), не вложенные
    экраны — см. cb_main_menu_action."""
    b = InlineKeyboardBuilder()
    b.button(text=BTN_UNIFIED, callback_data="mm:unified")
    b.button(text=BTN_DEEP, callback_data="mm:deep")
    b.button(text=BTN_DATE, callback_data="mm:date")
    b.button(text=BTN_SUBSCRIPTION, callback_data="show_premium")
    b.button(text=BTN_SUPPORT, callback_data="mm:support")
    b.adjust(1)
    return b.as_markup()


async def _send_main_menu(target: Message, edit: bool = False) -> None:
    """Экран главного меню — общий для /menu, кнопки «⬅️ Вернуться в меню»
    под результатами генерации и возврата «⬅️ Назад» из «👑 Подписка».
    edit=True (Назад из Подписки) — редактирует ТО ЖЕ сообщение (та же
    механика, что и у самой Подписки). edit=False — новое сообщение."""
    if edit:
        await target.edit_text(_MAIN_MENU_TEXT, reply_markup=main_menu_kb())
    else:
        await target.answer(_MAIN_MENU_TEXT, reply_markup=main_menu_kb())
