import asyncio
import hashlib
import html
import itertools
import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BusinessConnection,
    CallbackQuery, Document, ErrorEvent, Message,
    CopyTextButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import (
    ADMIN_TELEGRAM_ID,
    APP_NAME,
    BOT_TOKEN,
    FIRST_BUILD_THRESHOLD,
    FREE_TRIAL_REQUESTS,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENROUTER_API_KEY,
    PREMIUM_CACHE_TTL,
    PREMIUM_CHANNEL_ID,
    PREMIUM_SUBSCRIBE_URL,
    LLM_CACHE_TTL_SEC,
    REBUILD_THRESHOLD,
    REFRESH_SAMPLES_EVERY_N,
    REPLY_STYLES,
    SAMPLE_SIZE,
)
from features import detect_reply_situation, extract_features, stage_hint, totals_from_summary, winning_messages
from llm import (
    ILLEGIBLE_MARKER,
    PROVIDER_NAMES,
    RateLimitError,
    build_deep_analysis,
    build_deep_style_analysis,
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
    compare_my_styles,
    extract_chat_from_image,
    analyze_reply_dynamics,
    get_forced_provider,
    get_provider_stats,
    live_coach_step,
    make_features_summary,
    rewrite_message_explained,
    sample_texts,
    set_forced_provider,
    suggest_reply,
    suggest_reply_from_screenshot,
    suggest_reply_variants,
    transcribe_audio,
)
from tg_parser import parse_chat
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    delete_all_user_data,
    delete_contact_data,
    delete_deep_analysis,
    delete_style_card,
    find_contact_by_original_id,
    get_all_dated_messages,
    get_all_dated_my_messages,
    get_all_per_contact_style_cards,
    get_any_user_samples,
    get_auto_mode,
    get_biz_messages_for_contact,
    get_business_connection,
    get_contact_by_id,
    get_deep_analysis,
    get_deep_style_analysis,
    get_llm_cache,
    get_interaction_card,
    get_imported_messages,
    get_message_samples,
    get_trial_used,
    increment_trial_used,
    save_imported_messages,
    get_my_style_last_rebuild_count,
    get_my_style_per_contact,
    get_or_create_contact,
    get_running_notes,
    get_style_card,
    delete_deep_style_analysis,
    init_db,
    list_contacts,
    save_business_message,
    save_deep_analysis,
    save_deep_style_analysis,
    save_interaction_card,
    save_message_samples,
    save_my_style_per_contact,
    save_running_notes,
    save_style_card,
    record_event,
    set_auto_mode,
    set_llm_cache,
    update_contact_username,
    upsert_business_connection,
    upsert_chat_ref_mapping,
    upsert_user,
)

logging.basicConfig(level=logging.INFO)

dp = Dispatcher(storage=MemoryStorage())


@dp.errors()
async def on_unhandled_error(event: ErrorEvent) -> bool:
    """Глобальная сетка на необработанные исключения в хендлерах. Без неё сбой
    (например, недоступный LLM при генерации карточек) тихо убивал кнопку:
    спиннер гас, а пользователь не понимал, что произошло. Теперь — понятное
    сообщение вместо молчания."""
    logging.exception("unhandled update error: %s", event.exception)
    text = ("Лимит запросов исчерпан — попробуй через пару минут."
            if isinstance(event.exception, RateLimitError)
            else "Что-то пошло не так — попробуй ещё раз.")
    upd = event.update
    try:
        cq = getattr(upd, "callback_query", None)
        if cq is not None:
            try:
                await cq.answer(text, show_alert=True)
            except Exception:
                if cq.message is not None:
                    await cq.message.answer(text)
        elif getattr(upd, "message", None) is not None:
            await upd.message.answer(text)
    except Exception:
        logging.exception("error handler: не удалось уведомить пользователя")
    return True


BTN_REWRITE       = "📝 Переписать"
BTN_SCREENSHOT    = "📸 По скриншоту"
BTN_REPLY         = "💬 Ответить за меня"
BTN_LIVE          = "💫 Новый диалог"
BTN_DEEP          = "🔬 Глубокий анализ"
BTN_DEEP_STYLE    = "🪞 Глубокий анализ стиля"
BTN_HELP          = "❓ Помощь"
# BTN_ME («👤 Мой стиль») убрана вместе с командой /me — дублировала
# BTN_DEEP_STYLE (и была бесплатной лазейкой мимо подписки на неё).
# BTN_MY_STYLE_FOR («🎯 Мой стиль с ним») убрана из меню, но _show_my_style_for
# не удалена — можно вернуть кнопку одной правкой.
# BTN_CONTACT («🔍 Стиль собеседника») удалена совсем — её interaction_card
# теперь блоком внутри «Глубокий анализ» (_format_deep_analysis). BTN_CONTACTS
# («📋 Контакты») убрана из меню — доступна только как команда /contacts.
_ALL_BTNS = {
    BTN_REWRITE, BTN_SCREENSHOT, BTN_REPLY, BTN_LIVE, BTN_DEEP, BTN_DEEP_STYLE, BTN_HELP,
}

# Защита от параллельных пересборок одного контакта
_rebuilding: set[int] = set()

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
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Как message.answer(), но безопасно для текста длиннее лимита Telegram —
    клавиатура (если есть) уходит с последним куском."""
    chunks = _split_long_text(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=reply_markup if last else None)


async def _edit_or_answer_long(message: Message, text: str) -> None:
    """Как call.message.edit_text(), но при переполнении лимита Telegram первый
    кусок идёт в edit, а остальные — отдельными сообщениями (edit не может
    «раздвоиться» на несколько сообщений)."""
    chunks = _split_long_text(text)
    await message.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await message.answer(chunk)


# ── Подписка (Tribute) ──────────────────────────────────────────────────────
# Пропуск — членство в приватном канале, которым управляет Tribute (добавляет
# при оплате, убирает при отмене/неоплате). Бот только читает текущий статус.

_premium_cache: dict[str, tuple[bool, float]] = {}  # telegram_id -> (is_premium, checked_at)


async def _is_premium(bot: Bot, telegram_id: str) -> bool:
    """Проверяет членство в PREMIUM_CHANNEL_ID с кэшем на PREMIUM_CACHE_TTL сек,
    чтобы не дёргать Telegram API на каждое сообщение. Пока PREMIUM_CHANNEL_ID
    не настроен — всегда False (только бесплатные попытки)."""
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
    b.adjust(1)
    return b.as_markup()


async def _send_paywall(target: Message, text: str) -> None:
    await target.answer(text, reply_markup=paywall_kb())


async def _has_quota(bot: Bot, telegram_id: str) -> bool:
    """Есть ли доступ к генерации: premium или остались бесплатные попытки. Без списания."""
    if await _is_premium(bot, telegram_id):
        return True
    return get_trial_used(telegram_id) < FREE_TRIAL_REQUESTS


async def _quota_gate(bot: Bot, target: Message, telegram_id: str) -> bool:
    """Проверка доступа БЕЗ списания. Если попытки кончились — показывает пейволл.
    Списание делает _charge_trial_if_needed уже ПОСЛЕ успешной генерации."""
    if await _has_quota(bot, telegram_id):
        return True
    await _send_paywall(
        target,
        f"Бесплатные попытки закончились ({FREE_TRIAL_REQUESTS} использовано). "
        "Дальше — по подписке CueMe Premium."
    )
    return False


async def _charge_trial_if_needed(bot: Bot, telegram_id: str) -> None:
    """Списывает одну попытку триала. Вызывать ТОЛЬКО после успешного ответа LLM.
    Premium попытки не тратит."""
    if await _is_premium(bot, telegram_id):
        return
    increment_trial_used(telegram_id)


async def _require_premium(bot: Bot, target: Message, telegram_id: str) -> bool:
    """Гейт для функций без бесплатного триала (глубокий анализ, стиль
    собеседника, /compare и т.п.) — доступ только по активной подписке."""
    if await _is_premium(bot, telegram_id):
        return True

    await _send_paywall(target, "Эта функция доступна только по подписке CueMe Premium.")
    return False


def main_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_REWRITE), KeyboardButton(text=BTN_SCREENSHOT), KeyboardButton(text=BTN_REPLY))
    b.row(KeyboardButton(text=BTN_LIVE))
    b.row(KeyboardButton(text=BTN_DEEP), KeyboardButton(text=BTN_DEEP_STYLE))
    b.row(KeyboardButton(text=BTN_HELP))
    return b.as_markup(resize_keyboard=True)


def style_pick_kb(action_id: str) -> InlineKeyboardMarkup:
    """Единая клавиатура выбора стиля — общая для «Переписать», «Ответить за
    меня» и «По скриншоту». action_id зашит в callback_data и указывает на
    конкретный слот в _last_action[user_id] — так несколько параллельных
    черновиков/входящих у одного юзера не путаются друг с другом."""
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Подбери сам", callback_data=f"stylepick:auto:{action_id}")
    for key, (label, _desc) in REPLY_STYLES.items():
        b.button(text=label, callback_data=f"stylepick:{key}:{action_id}")
    b.adjust(1, 2)
    return b.as_markup()


def _auto_style_for_ctx(ctx: dict) -> str:
    """Консервативный выбор стиля по текущей ситуации. Пользователь всё ещё может
    нажать «Другой стиль», это только быстрый дефолт для частых кейсов."""
    kind = ctx.get("kind")
    text = (ctx.get("text") if kind in ("rewrite", "reply") else ctx.get("chat_text")) or ""
    last = _last_incoming_line(text) if kind == "screenshot" else text
    low = last.lower()
    signals = (ctx.get("data_signals") or "").lower()

    if "отказ/холод/негатив" in signals or any(
        p in low for p in ("не до знакомств", "всё равно", "все равно", "бесит", "не хочу", "не интересно", "неинтересно")
    ):
        return "confident"
    if any(p in low for p in ("страшно", "боюсь", "тревожно", "устала", "устал", "вымоталась", "плохо", "тяжело")):
        return "tender"
    if any(p in low for p in ("здравствуйте", "добрый день", "вы ", "вам ", "вас ")):
        return "formal"
    if any(p in low for p in ("ахах", "хаха", "смешно", "пицц", "шут", "угар")):
        return "humor"
    if any(p in low for p in ("снился", "снилась", "мечтаю", "скуч", "цел", "краси", "симпат")):
        return "flirt"
    if any(p in low for p in ("во сколько", "когда", "где", "встреч", "завтра", "сегодня", "кофе", "прогул")):
        return "friendly"
    if kind == "rewrite" and any(p in low for p in ("может", "сходим", "увидимся", "встретимся")):
        return "flirt"
    return "friendly"


# Telegram ограничивает CopyTextButton.text 256 символами.
_COPY_TEXT_LIMIT = 256


def style_result_kb(action_id: str, copy_text: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Другой стиль", callback_data=f"styleother:{action_id}")
    b.button(text="🔄 Перегенерировать", callback_data=f"styleregen:{action_id}")
    # Нативная кнопка Telegram копирует текст в буфер прямо на клиенте.
    # Если текст длиннее лимита — fallback на callback (шлём копируемым блоком).
    if copy_text and len(copy_text) <= _COPY_TEXT_LIMIT:
        b.button(text="📋 Скопировать", copy_text=CopyTextButton(text=copy_text))
    else:
        b.button(text="📋 Скопировать", callback_data=f"stylecopy:{action_id}")
    b.adjust(1, 2)
    return b.as_markup()


def _style_cache_key(kind: str, style: str, text: str, style_card: str, interaction_card: str) -> str:
    """Контент-адресный ключ кэша: включает карточки стиля, поэтому при их пересборке
    ключ меняется сам (авто-инвалидация без TTL-гонок)."""
    raw = "\x00".join([kind or "", style or "", text or "", style_card or "", interaction_card or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _run_style_generation(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str,
    state: FSMContext | None = None, force_fresh: bool = False,
) -> None:
    """Общий шаг генерации для всех трёх фич переписывания (rewrite/reply/screenshot)
    после того как стиль выбран. Шлёт результат + пояснение/оценку отдельным
    сообщением; для «Ответить за меня» и «По скриншоту» — ещё и напоминание
    как продолжить (авто-режим: следующее сообщение/скриншот без повторного
    нажатия кнопки меню).

    Гейт монетизации: проверка ДО вызова LLM, списание — только ПОСЛЕ успешного
    ответа. Кэш: попадание = НЕ вызов LLM → попытку не тратим. force_fresh=True
    (регенерация) обходит кэш, чтобы дать новый вариант, и обновляет запись."""
    kind      = ctx.get("kind")
    style_key = ctx.get("style")
    text      = ctx.get("text") if kind in ("rewrite", "reply") else ctx.get("chat_text")
    if not style_key or text is None:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card, interaction_card = ctx["style_card"], ctx["interaction_card"]
    cache_key = _style_cache_key(kind, style_key, text, style_card, interaction_card)

    result = expl = rating = None
    if not force_fresh:
        cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
        if cached:
            try:
                result, expl, rating = json.loads(cached)
                logging.info("style-gen: cache hit (%s/%s)", kind, style_key)
            except (ValueError, TypeError):
                result = None

    if result is None:
        # Реальный вызов LLM — здесь и только здесь гейт + списание.
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        prev = ctx.get("result") if force_fresh else None
        signals = ctx.get("data_signals")
        winning = ctx.get("winning")
        try:
            if kind == "rewrite":
                result, expl, rating = await rewrite_message_explained(
                    text, style_card, interaction_card, style_key, previous_result=prev
                )
            elif kind == "reply":
                result, expl, rating = await suggest_reply(
                    text, style_card, interaction_card, style_key,
                    previous_result=prev, data_signals=signals, winning_examples=winning,
                )
            else:  # screenshot
                result, expl, rating = await suggest_reply_from_screenshot(
                    text, style_card, interaction_card, style_key,
                    previous_result=prev, data_signals=signals, winning_examples=winning,
                )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("%s: ошибка генерации (стиль %s)", kind, style_key)
            await target.answer("Не получилось сгенерировать — попробуй ещё раз.")
            return

        # Успех — списываем попытку (premium не тратит) и кэшируем.
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps([result, expl, rating], ensure_ascii=False))
        # Телеметрия исходов (best-effort — сбой не должен ломать выдачу ответа).
        try:
            record_event(str(telegram_id), f"gen_{kind}", style_key or "")
        except Exception:
            logging.exception("telemetry: не удалось записать событие генерации")

    ctx["result"] = result
    await _answer_long(target, result, reply_markup=style_result_kb(action_id, result))
    tail = ""
    if expl:
        tail += f"💡 {expl}"
    if rating:
        tail += ("\n\n" if tail else "") + rating
    if tail:
        await target.answer(tail)

    if kind == "reply":
        await target.answer(
            "Пришли следующее сообщение собеседника, чтобы ответить и на него. "
            "Чтобы выйти из режима — нажми любую кнопку меню."
        )
    elif kind == "screenshot" and state is not None:
        await state.set_state(Screenshot.waiting_for_image)
        await target.answer(
            "Пришли следующий скриншот (или текст переписки), чтобы продолжить. "
            "Чтобы выйти из режима — нажми любую кнопку меню."
        )


_STYLE_KINDS = ("rewrite", "reply", "screenshot")


@dp.callback_query(F.data.startswith("stylepick:"))
async def cb_style_pick(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    _, style_key, action_id = parts
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") not in _STYLE_KINDS:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    if style_key == "auto":
        style_key = _auto_style_for_ctx(ctx)
    elif style_key not in REPLY_STYLES:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer()
    ctx["style"] = style_key
    label = REPLY_STYLES[style_key][0]
    await call.message.edit_text(f"Генерирую в стиле «{label}»...", reply_markup=None)
    await _run_style_generation(call.message, ctx, call.from_user.id, call.bot, action_id, state)


@dp.callback_query(F.data.startswith("styleother:"))
async def cb_style_other(call: CallbackQuery) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") not in _STYLE_KINDS:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer()
    await call.message.answer("В каком стиле?", reply_markup=style_pick_kb(action_id))


@dp.callback_query(F.data.startswith("styleregen:"))
async def cb_style_regen(call: CallbackQuery, state: FSMContext) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") not in _STYLE_KINDS or not ctx.get("style"):
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer("Перегенерирую...")
    # force_fresh: реген должен дать НОВЫЙ вариант, а не вернуть тот же из кэша.
    await _run_style_generation(call.message, ctx, call.from_user.id, call.bot, action_id, state, force_fresh=True)


@dp.callback_query(F.data.startswith("stylecopy:"))
async def cb_style_copy(call: CallbackQuery) -> None:
    """Fallback для текста длиннее лимита CopyTextButton (256): шлём его
    моноширинным блоком — по тапу Telegram копирует содержимое в буфер."""
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or not ctx.get("result"):
        await call.answer("Текст не найден — начни заново.", show_alert=True)
        return
    await call.answer("Нажми на текст, чтобы скопировать")
    wrapped = f"<code>{html.escape(ctx['result'])}</code>"
    if len(wrapped) <= 4096:
        await call.message.answer(wrapped, parse_mode="HTML")
    else:
        await _answer_long(call.message, ctx["result"])


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

class Rewrite(StatesGroup):
    waiting_for_draft = State()

class ReplyHelp(StatesGroup):
    waiting_for_incoming = State()

class Screenshot(StatesGroup):
    waiting_for_image = State()

class LiveDialogue(StatesGroup):
    waiting_for_name     = State()
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


# Троттлинг _refresh_samples: считаем business-сообщения на контакт и обновляем
# message_samples не чаще, чем раз в REFRESH_SAMPLES_EVERY_N. In-memory — при рестарте
# сбрасывается, тогда первый refresh просто случится раньше (не критично).
_refresh_pending: dict[int, int] = {}


def _should_refresh_samples(contact_id: int) -> bool:
    """True раз в REFRESH_SAMPLES_EVERY_N сообщений на контакт (и сбрасывает счётчик)."""
    n = _refresh_pending.get(contact_id, 0) + 1
    if n >= REFRESH_SAMPLES_EVERY_N:
        _refresh_pending[contact_id] = 0
        return True
    _refresh_pending[contact_id] = n
    return False


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

async def _maybe_rebuild(owner_user_id: str, contact_id: int, bot: Bot | None = None) -> None:
    global _rebuild_cooldown_until
    if contact_id in _rebuilding:
        return
    if time.monotonic() < _rebuild_cooldown_until:
        return  # лимит Groq недавно исчерпан — не дёргаем API на каждое сообщение

    last  = get_my_style_last_rebuild_count(contact_id)
    total = count_biz_messages_for_contact(owner_user_id, contact_id)

    # Первая сборка (карточки ещё нет) — сниженный порог, чтобы новый юзер
    # быстрее увидел результат; сообщения считаем из всех источников
    # (business + ручная вставка/JSON). Пересборка — обычный порог по biz-дельте.
    is_first = get_my_style_per_contact(contact_id) is None
    if is_first:
        combined = total + count_imported_messages(contact_id)
        if combined < FIRST_BUILD_THRESHOLD:
            return
    elif total - last < REBUILD_THRESHOLD:
        return

    # Порог достигнут — гарантируем свежие message_samples на момент пересборки
    # (перекрывает троттлинг на горячем пути) и сбрасываем счётчик.
    _refresh_pending.pop(contact_id, None)
    _refresh_samples(owner_user_id, contact_id)

    _rebuilding.add(contact_id)
    try:
        logging.info("auto-rebuild start: contact_id=%s (new=%s, first=%s)", contact_id, total - last, is_first)
        ok = await _rebuild_contact(owner_user_id, contact_id)
        if ok:
            per_contact = get_all_per_contact_style_cards(owner_user_id)
            if per_contact:
                overall = await build_overall_style(per_contact)
                save_style_card(owner_user_id, overall)
        logging.info("auto-rebuild done: contact_id=%s ok=%s", contact_id, ok)

        if ok and is_first and bot is not None:
            # Первый разбор готов — проактивно показываем черновик владельцу.
            card = get_my_style_per_contact(contact_id)
            c = get_contact_by_id(contact_id)
            label = _contact_name(c) if c else "собеседник"
            if card:
                try:
                    for chunk in _split_long_text(
                        f"🎉 Накопилось достаточно сообщений — вот первый набросок "
                        f"твоего стиля с {label}. Он черновой, станет точнее по мере "
                        f"переписки через бота:\n\n{card}"
                    ):
                        await bot.send_message(int(owner_user_id), chunk)
                except Exception:
                    logging.warning("first-build notify failed: owner=%s", owner_user_id)
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

    # «Живой диалог» уже накопил заметки о собеседнике — используем их напрямую,
    # пока не появится формально пересобранная карточка (без ожидания порога).
    notes = get_running_notes(contact_id)
    if notes and notes["notes_text"]:
        return notes["notes_text"]

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


# ── 🔬 Глубокий анализ ────────────────────────────────────────────────────────

DEEP_ANALYSIS_MIN_MSGS = 10  # минимум сообщений с каждой стороны, иначе анализ бессмысленен


def _periodized_dated_lines(rows: list[dict], target_total: int = 220, buckets: int = 6) -> list[str]:
    """Хронологический семпл с равномерным охватом всей истории (не только
    последних сообщений) — бьём на буквенных бакетов по времени и берём
    равномерные срезы внутри каждого, чтобы LLM видел динамику по периодам."""
    rows = sorted((r for r in rows if r["text"] and r["text"].strip()), key=lambda r: r["date"])
    if not rows:
        return []

    per_bucket  = max(1, target_total // buckets)
    bucket_size = max(1, len(rows) // buckets)
    lines: list[str] = []
    for i in range(0, len(rows), bucket_size):
        chunk = rows[i:i + bucket_size]
        step  = max(1, len(chunk) // per_bucket)
        for r in chunk[::step][:per_bucket]:
            who = "Я" if r["direction"] == "out" else "Собеседник"
            lines.append(f"{r['date'][:10]} {who}: {r['text']}")
    return lines


def _deep_stats_summary(rows: list[dict]) -> str:
    my = [r for r in rows if r["direction"] == "out" and r["text"]]
    ct = [r for r in rows if r["direction"] == "in" and r["text"]]
    dates = sorted(r["date"] for r in rows if r["text"])
    date_from = dates[0][:10] if dates else "?"
    date_to   = dates[-1][:10] if dates else "?"
    my_avg = sum(len(t["text"]) for t in my) / len(my) if my else 0
    ct_avg = sum(len(t["text"]) for t in ct) / len(ct) if ct else 0
    return (
        f"Период переписки: {date_from} — {date_to}\n"
        f"Я: {len(my)} сообщ., средн. {my_avg:.0f} симв.\n"
        f"Собеседник: {len(ct)} сообщ., средн. {ct_avg:.0f} симв."
    )


async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
    """Ленивая генерация с кэшем в deep_analysis. None — данных мало."""
    cached = get_deep_analysis(contact_id)
    if cached:
        return cached

    rows = get_all_dated_messages(owner_user_id, contact_id)
    my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
    ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
    if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
        return None

    dated_lines = _periodized_dated_lines(rows)
    stats       = _deep_stats_summary(rows)
    compat, history, swot, gifts = await build_deep_analysis(dated_lines, stats)
    save_deep_analysis(contact_id, compat, history, swot, gifts)
    return {
        "compatibility_text": compat, "history_text": history,
        "swot_text": swot, "gifts_text": gifts,
    }


def _format_deep_analysis(name: str, data: dict, interaction_card: str | None) -> tuple[str, str, str]:
    msg1 = (
        f"🔬 Глубокий анализ — {name}\n\n"
        f"💞 Совместимость\n\n{data['compatibility_text']}\n\n"
        f"📖 История отношений\n\n{data['history_text']}"
    )
    msg2 = f"🗣️ Стиль и привычки {name}\n\n{interaction_card}" if interaction_card else ""
    msg3 = (
        f"🧭 Сильные стороны, проблемы и точки роста\n\n{data['swot_text']}\n\n"
        f"🎁 Рекомендации подарков\n\n{data['gifts_text']}"
    )
    return msg1, msg2, msg3


def deep_analysis_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
    return b.as_markup()


async def _run_deep_analysis(
    bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
) -> None:
    if not await _require_premium(bot, target, telegram_id):
        return
    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    name = _contact_name(contact)

    wait_text = f"Готовлю глубокий анализ — {name}. Это займёт ~30 секунд..."
    await (target.edit_text(wait_text) if edit else target.answer(wait_text))

    try:
        data = await _gen_deep_analysis(contact_id, telegram_id)
    except RateLimitError:
        await target.answer("Лимит LLM исчерпан, попробуй позже.")
        return
    except Exception:
        logging.exception("deep_analysis: ошибка генерации")
        await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
        return

    if not data:
        await target.answer(
            f"Пока маловато данных по {name} для глубокого анализа — нужно минимум "
            f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
            "накопление через Автоматизацию чатов)."
        )
        return

    try:
        interaction_card = await _gen_interaction_card(contact_id, telegram_id)
    except Exception:
        logging.exception("deep_analysis: не удалось получить стиль собеседника")
        interaction_card = None

    msg1, msg2, msg3 = _format_deep_analysis(name, data, interaction_card)
    await _answer_long(target, msg1)
    if msg2:
        await _answer_long(target, msg2)
    await _answer_long(target, msg3, reply_markup=deep_analysis_result_kb(contact_id))


async def _show_deep_analysis(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer("Сначала загрузи JSON-файл чата.")
        return

    if len(contacts) == 1:
        await _run_deep_analysis(bot, message, telegram_id, contacts[0]["id"])
        return

    await message.answer("Для кого сделать глубокий анализ?", reply_markup=contacts_kb(contacts, "deepan"))


@dp.message(Command("deep_analysis"))
async def cmd_deep_analysis(message: Message, bot: Bot) -> None:
    await _show_deep_analysis(message, bot)


@dp.callback_query(F.data.startswith("deepan_refresh:"))
async def cb_deep_analysis_refresh(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer("Пересобираю анализ...")
    delete_deep_analysis(contact_id)
    await _run_deep_analysis(bot, call.message, telegram_id, contact_id)


@dp.callback_query(F.data.startswith("deepan:"))
async def cb_deep_analysis_contact(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _run_deep_analysis(bot, call.message, telegram_id, contact_id, edit=True)


# ── 🪞 Глубокий анализ моего стиля (агрегат по всем контактам) ────────────────

DEEP_STYLE_MIN_MSGS = 20  # минимум своих сообщений суммарно, иначе анализ бессмысленен


def _deep_style_stats_summary(rows: list[dict]) -> str:
    dates = sorted(r["date"] for r in rows if r["text"])
    date_from = dates[0][:10] if dates else "?"
    date_to   = dates[-1][:10] if dates else "?"
    avg = sum(len(r["text"]) for r in rows) / len(rows) if rows else 0
    return (
        f"Период: {date_from} — {date_to}\n"
        f"Всего сообщений: {len(rows)}, средняя длина {avg:.0f} симв."
    )


async def _gen_deep_style_analysis(telegram_id: str) -> dict | None:
    """Ленивая генерация с кэшем в deep_style_analysis. None — данных мало."""
    cached = get_deep_style_analysis(telegram_id)
    if cached:
        return cached

    rows = get_all_dated_my_messages(telegram_id)
    if len(rows) < DEEP_STYLE_MIN_MSGS:
        return None

    dated_lines = _periodized_dated_lines(rows)
    stats       = _deep_style_stats_summary(rows)
    profile, history, swot, tips = await build_deep_style_analysis(dated_lines, stats)
    save_deep_style_analysis(telegram_id, profile, history, swot, tips)
    return {
        "profile_text": profile, "history_text": history,
        "swot_text": swot, "tips_text": tips,
    }


def _format_deep_style_analysis(data: dict) -> tuple[str, str]:
    msg1 = (
        "🪞 Глубокий анализ твоего стиля\n\n"
        f"🎙️ Коммуникативный профиль\n\n{data['profile_text']}\n\n"
        f"📖 Как менялся твой стиль\n\n{data['history_text']}"
    )
    msg2 = (
        f"🧭 Сильные стороны, проблемы и точки роста\n\n{data['swot_text']}\n\n"
        f"🎯 Рекомендации для дейтинга\n\n{data['tips_text']}"
    )
    return msg1, msg2


def deep_style_result_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить анализ", callback_data="deepstyle_refresh")
    return b.as_markup()


async def _run_deep_style_analysis(bot: Bot, target: Message, telegram_id: str) -> None:
    if not await _require_premium(bot, target, telegram_id):
        return
    await target.answer("Готовлю глубокий анализ твоего стиля. Это займёт ~30 секунд...")

    try:
        data = await _gen_deep_style_analysis(telegram_id)
    except RateLimitError:
        await target.answer("Лимит LLM исчерпан, попробуй позже.")
        return
    except Exception:
        logging.exception("deep_style_analysis: ошибка генерации")
        await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
        return

    if not data:
        await target.answer(
            f"Пока маловато данных для глубокого анализа стиля — нужно минимум "
            f"{DEEP_STYLE_MIN_MSGS} твоих сообщений суммарно (JSON-экспорт или "
            "накопление через Автоматизацию чатов)."
        )
        return

    msg1, msg2 = _format_deep_style_analysis(data)
    await _answer_long(target, msg1)
    await _answer_long(target, msg2, reply_markup=deep_style_result_kb())


async def _show_deep_style_analysis(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    if not list_contacts(telegram_id):
        await message.answer("Сначала загрузи JSON-файл чата.")
        return
    await _run_deep_style_analysis(bot, message, telegram_id)


@dp.message(Command("deep_style_analysis"))
async def cmd_deep_style_analysis(message: Message, bot: Bot) -> None:
    await _show_deep_style_analysis(message, bot)


@dp.callback_query(F.data == "deepstyle_refresh")
async def cb_deep_style_analysis_refresh(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    await call.answer("Пересобираю анализ...")
    delete_deep_style_analysis(telegram_id)
    await _run_deep_style_analysis(bot, call.message, telegram_id)


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


def _persist_business_message(
    *, conn_id: str, owner_id: str, chat_ref: str, direction: str,
    text: str | None, is_voice: bool, date: str, tg_message_id: int,
    contact_tg_id: str, chat_first_name: str | None, chat_last_name: str | None,
    chat_username: str | None, sender_username: str | None,
) -> int | None:
    """Синхронная DB-часть обработки business-сообщения: сохранение + резолв контакта
    + троттлинг refresh. Возвращает contact_id для пересборки (или None).
    Выполняется в asyncio.to_thread, чтобы не блокировать event loop на живом потоке."""
    inserted = save_business_message(
        connection_id=conn_id, owner_user_id=owner_id, chat_ref=chat_ref,
        direction=direction, text=text, date=date, tg_message_id=tg_message_id,
        raw_meta=_msg_meta(text, is_voice),
    )
    if not inserted:
        # Повторная доставка того же сообщения — не триггерим пересборку.
        logging.info(
            "business_message дубль пропущен: conn=%s chat_ref=%s msg_id=%s",
            conn_id, chat_ref, tg_message_id,
        )
        return None
    logging.info(
        "business_message saved: conn=%s chat_ref=%s direction=%s",
        conn_id, chat_ref, direction,
    )
    upsert_user(owner_id, f"user{owner_id}")

    # Для приватного чата contact_tg_id всегда равен ID собеседника
    if contact_tg_id == owner_id:
        return None  # edge-case: не создаём контакт «сам с собой»
    original_id = f"user{contact_tg_id}"

    contact_row = find_contact_by_original_id(owner_id, original_id)
    if not contact_row:
        # Контакт ещё не создан — создаём автоматически из данных чата
        display_name = " ".join(
            p for p in (chat_first_name or "", chat_last_name or "") if p
        ).strip()
        cid = get_or_create_contact(owner_id, original_id, display_name)
        if chat_username:
            update_contact_username(cid, chat_username)
        upsert_chat_ref_mapping(owner_id, chat_ref, cid)
        logging.info("auto-created contact: id=%s name=%s", cid, display_name)
    else:
        cid = contact_row["id"]
        upsert_chat_ref_mapping(owner_id, chat_ref, cid)
        if direction == "in" and sender_username:
            update_contact_username(cid, sender_username)

    # Освежаем message_samples (без LLM, дёшево), но не чаще раза в N сообщений
    if _should_refresh_samples(cid):
        _refresh_samples(owner_id, cid)
    return cid


@dp.business_message()
async def handle_business_message(event: Message, bot: Bot) -> None:
    conn_id = event.business_connection_id
    if not conn_id:
        return

    conn_row = await asyncio.to_thread(get_business_connection, conn_id)
    if not conn_row:
        logging.warning("business_message: unknown connection %s", conn_id)
        return

    sender_id = str(event.from_user.id) if event.from_user else None
    if not sender_id:
        return

    owner_id  = conn_row["owner_user_id"]
    direction = "out" if sender_id == owner_id else "in"
    chat_ref  = _chat_ref(event.chat.id)
    text, is_voice = await _message_text(bot, event)  # голосовое → текст через Whisper
    date      = event.date.isoformat()

    # Синхронную DB-часть уводим в поток, чтобы не блокировать event loop.
    contact_id_for_rebuild = await asyncio.to_thread(
        _persist_business_message,
        conn_id=conn_id, owner_id=owner_id, chat_ref=chat_ref, direction=direction,
        text=text, is_voice=is_voice, date=date, tg_message_id=event.message_id,
        contact_tg_id=str(event.chat.id),
        chat_first_name=event.chat.first_name, chat_last_name=event.chat.last_name,
        chat_username=getattr(event.chat, "username", None),
        sender_username=event.from_user.username if event.from_user else None,
    )

    if contact_id_for_rebuild:
        asyncio.create_task(_maybe_rebuild(owner_id, contact_id_for_rebuild, bot))


# ── /start ────────────────────────────────────────────────────────────────────

def _capabilities_text() -> str:
    return (
        "Вот что я умею:\n\n"
        "📝 Переписать — твой черновик → выбери стиль → готовое под собеседника\n"
        "📸 По скриншоту — пришли скриншот переписки → выбери стиль ответа. "
        "Можно слать скриншоты один за другим без повторного нажатия кнопки\n"
        "💬 Ответить за меня — подскажу ответ на его сообщение, с выбором стиля\n"
        "🔬 Глубокий анализ — совместимость, история отношений, как писать "
        "этому человеку, подарки\n"
        "🪞 Глубокий анализ стиля — твой коммуникативный профиль и советы для дейтинга\n"
        "/contacts — загруженные чаты · /stats — портрет в цифрах · /compare — сравнить стили\n\n"
        f"💎 {FREE_TRIAL_REQUESTS} бесплатных попыток на переписать/ответить/скриншот, "
        "дальше и остальные функции — по подписке. Статус — /premium.\n\n"
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


def onboarding_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 Подключить через Настройки", callback_data="onb:business")
    b.button(text="💻 У меня есть комп (JSON)", callback_data="onb:json")
    b.button(text="🎬 Попробовать на примере", callback_data="demo")
    b.adjust(1)
    return b.as_markup()


# Как открыть Настройки и добраться до профиля — различается по платформам.
# Путь для iPhone проверен вручную (Настройки → «Изменить» у профиля →
# «Автоматизация чатов»). Android почти всегда зеркалит iOS-версию, поэтому
# тот же путь; десктоп не проверялся — формулировка чуть более общая.
_PLATFORM_OPEN_SETTINGS = {
    "iphone":  "Открой Telegram → внизу экрана нажми на вкладку ⚙️ Settings",
    "android": "Открой Telegram → нажми ☰ (три полоски) в левом верхнем углу → Настройки",
    "desktop": "Открой Telegram → нажми ☰ в левом верхнем углу (или на свой аватар "
               "в левой панели) → Настройки",
}
_PLATFORM_EDIT_STEP = {
    "iphone":  "Нажми «Изменить» рядом со своим профилем/фото",
    "android": "Нажми «Изменить» рядом со своим профилем/фото",
    "desktop": "Найди кнопку редактирования профиля (иконка карандаша рядом с твоим "
               "именем/фото) и открой её",
}
_PLATFORM_LABELS = {"iphone": "🍏 iPhone", "android": "🤖 Android", "desktop": "💻 Компьютер"}


def platform_pick_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label in _PLATFORM_LABELS.items():
        b.button(text=label, callback_data=f"onb:platform:{key}")
    b.adjust(3)
    return b.as_markup()


async def _business_connect_text(bot: Bot, platform: str) -> str:
    me = await bot.get_me()
    open_settings = _PLATFORM_OPEN_SETTINGS.get(platform, _PLATFORM_OPEN_SETTINGS["android"])
    edit_step     = _PLATFORM_EDIT_STEP.get(platform, _PLATFORM_EDIT_STEP["android"])
    return (
        "Подключи меня к своим чатам — я буду учиться твоему стилю прямо "
        "по живой переписке, ничего загружать не нужно:\n\n"
        f"1️⃣ {open_settings}\n"
        f"2️⃣ {edit_step}\n"
        "3️⃣ Выбери «Автоматизация чатов»\n"
        f"4️⃣ В поле впиши @{me.username} и выбери меня\n"
        "5️⃣ Включи переключатель «Ответы на сообщения»\n"
        "6️⃣ Выбери чаты, к которым дать доступ (можно один)\n\n"
        "Не нашёл пункт «Автоматизация чатов»? В поиске по настройкам введи "
        "«автоматизация» или «automation» — так быстрее всего.\n\n"
        "Всё — дальше просто переписывайся как обычно. Как только накопится "
        f"{FIRST_BUILD_THRESHOLD} сообщений, с момента подключения бота, по человеку, пришлю первый разбор твоего стиля.\n\n"
        "Имена и контакты собеседников не сохраняются — только анонимизированные паттерны."
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    telegram_id = str(message.from_user.id)
    caps = _capabilities_text()

    if list_contacts(telegram_id):
        await message.answer(f"С возвращением!\n\n{caps}", reply_markup=main_kb())
        return

    await message.answer(
        f"Привет! Я {APP_NAME} — твой дейтинг-коуч в переписках: пишу твоим голосом, "
        "но так, чтобы собеседнику хотелось отвечать.\n\n"
        "С чего начнём? 👇",
        reply_markup=onboarding_kb(),
    )


@dp.callback_query(F.data == "onb:business")
async def cb_onboarding_business(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    upsert_user(str(call.from_user.id), f"user{call.from_user.id}")
    await call.message.answer("Ты сейчас с какого устройства?", reply_markup=platform_pick_kb())


@dp.callback_query(F.data.startswith("onb:platform:"))
async def cb_onboarding_platform(call: CallbackQuery, bot: Bot) -> None:
    platform = call.data.split(":")[2]
    await call.answer()
    await call.message.answer(await _business_connect_text(bot, platform))


@dp.callback_query(F.data == "onb:json")
async def cb_onboarding_json(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Setup.waiting_for_json)
    await call.message.answer(
        "Загрузи переписку: Telegram Desktop → открой чат → ⋮ → "
        "Экспорт истории чата → формат JSON (без медиа) → пришли файл result.json сюда."
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
async def handle_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if message.text == BTN_REWRITE:
        await _start_rewrite(message, state)
    elif message.text == BTN_SCREENSHOT:
        await _start_screenshot(message, state)
    elif message.text == BTN_REPLY:
        await _start_reply(message, state)
    elif message.text == BTN_LIVE:
        await _start_live_dialogue(message, state)
    elif message.text == BTN_DEEP:
        await _show_deep_analysis(message, bot)
    elif message.text == BTN_DEEP_STYLE:
        await _show_deep_style_analysis(message, bot)
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
    delete_deep_style_analysis(telegram_id)

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
            "Нажми «🔬 Глубокий анализ» для разбора.",
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
    await message.answer("Ты сейчас с какого устройства?", reply_markup=platform_pick_kb())


# ── /provider — переключить LLM-провайдера (только для админа) ───────────────
# Меняет каскад ГЛОБАЛЬНО для всего бота (module-level _forced в llm.py), а не
# только для вызывающего — поэтому доступ только разработчику по ADMIN_TELEGRAM_ID.

@dp.message(Command("provider"))
async def cmd_provider(message: Message) -> None:
    if not ADMIN_TELEGRAM_ID or str(message.from_user.id) != ADMIN_TELEGRAM_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    variants = " · ".join(p.lower() for p in PROVIDER_NAMES) + " · auto"
    if len(parts) < 2:
        stats = get_provider_stats()
        stats_lines = ""
        if stats:
            rows = [
                f"• {n}: ok {s['ok']}, лимит {s['rate_limit']}, ошибок {s['error']}, ~{s['avg_ms']:.0f}мс"
                for n in PROVIDER_NAMES if (s := stats.get(n))
            ]
            if rows:
                stats_lines = "\n\n📊 Вызовы (с рестарта):\n" + "\n".join(rows)
        await message.answer(
            f"Сейчас активен: {get_forced_provider()}\n"
            f"Каскад: {' → '.join(PROVIDER_NAMES)}\n\n"
            f"Переключить: /provider <{variants}>\n"
            "После выбора что-нибудь перепиши — в логах будет «LLM [Провайдер]: ok ...».\n"
            "/provider auto — вернуть обычный каскад."
            + stats_lines
        )
        return
    try:
        result = set_forced_provider(parts[1].strip())
    except ValueError as e:
        await message.answer(str(e))
        return
    if result == "auto":
        await message.answer(f"✅ Провайдер: авто-каскад ({' → '.join(PROVIDER_NAMES)}).")
    else:
        await message.answer(
            f"✅ Принудительно выбран: {result}.\n"
            "Перепиши любое сообщение для проверки. /provider auto — вернуть каскад."
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
        await _answer_long(message, f"Мой стиль с {name}:\n\n{card}")
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

    await _edit_or_answer_long(call.message, f"Мой стиль с {name}:\n\n{card}")


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
async def handle_draft(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    draft = (txt or "").strip()
    if not draft:
        await message.answer("Пришли черновик текстом или голосовым.")
        return

    data = await state.get_data()
    await state.clear()

    if not await _quota_gate(bot, message, str(message.from_user.id)):
        return

    action_id = _new_action(message.from_user.id, {
        "kind": "rewrite", "text": draft, "result": None, "style": None,
        "style_card": data["style_card"], "interaction_card": data["interaction_card"],
    })
    await message.answer("В каком стиле переписать?", reply_markup=style_pick_kb(action_id))


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
        await state.update_data(
            style_card=style_card, interaction_card=interaction_card, contact_id=c["id"]
        )
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

    await state.update_data(
        style_card=style_card, interaction_card=interaction_card, contact_id=contact_id
    )
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(
        f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
    )


def _format_blocks(blocks: list[dict]) -> str:
    """Собирает блоки observation/mechanism/action в читаемое сообщение."""
    return "\n\n".join(
        f"🔍 {b['observation']}\n⚙️ {b['mechanism']}\n🎯 {b['action']}" for b in blocks
    )


def _last_incoming_line(chat_text: str) -> str:
    """Последняя непустая строка распознанной переписки — приближение последней
    реплики собеседника для ситуативной эвристики (скриншот/OCR). Если OCR
    сохранил роли, пропускаем строки автора («Я: ...») и берём последнюю чужую."""
    lines = [line.strip() for line in (chat_text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    self_re = re.compile(r"^(я|me|you)\s*[:：-]", re.IGNORECASE)
    other_re = re.compile(r"^(собеседник|он|она|они|контакт|не я)\s*[:：-]", re.IGNORECASE)

    for s in reversed(lines):
        if other_re.match(s):
            return s
    for s in reversed(lines):
        if not self_re.match(s):
            return s
    return lines[-1]


def _reply_data_signals(samples: dict | None, last_incoming: str) -> str | None:
    """Факты для промпта ответа (без LLM): стадия общения по объёму переписки +
    пометка о тяжёлой/сухой последней реплике. Готовый блок-список или None."""
    parts: list[str] = []
    if samples:
        # Стадия — по РЕАЛЬНОМУ объёму из features_summary; семплы усечены и годятся
        # лишь как фолбэк, если сводку не удалось распарсить.
        totals = totals_from_summary(samples.get("features_summary") or "")
        if totals:
            my_n, c_n = totals
        else:
            my_n = len(samples.get("my_sample") or [])
            c_n = len(samples.get("contact_sample") or [])
        if my_n + c_n >= 4:  # тот же порог, что и для разбора динамики
            parts.append(stage_hint(my_n, c_n))
    situ = detect_reply_situation(last_incoming)
    if situ:
        parts.append(situ)
    return "\n".join(f"• {p}" for p in parts) if parts else None


def _winning_for_contact(owner: str, contact_id) -> list[str] | None:
    """Few-shot «удачных заходов» автора с этим контактом (features.winning_messages
    по накопленной переписке). None, если контакта/данных нет — best-effort."""
    if not contact_id:
        return None
    try:
        wins = winning_messages(get_all_dated_messages(owner, contact_id))
    except Exception:
        logging.exception("winning: не удалось посчитать удачные заходы")
        return None
    return wins or None


async def _send_reply_analysis(message: Message, contact_id, incoming: str) -> None:
    """Короткий разбор динамики переписки перед выбором стиля.
    Дополняет готовый ответ, не заменяет его. При любой проблеме — молча пропускаем,
    чтобы не ломать основной flow ответа."""
    if not contact_id:
        return
    samples = get_message_samples(contact_id)
    if not samples:
        return
    my_sample      = samples["my_sample"] or []
    contact_sample = samples["contact_sample"] or []
    # Слишком мало сообщений — разбор был бы «на воде». Не тратим вызов LLM.
    if len(my_sample) + len(contact_sample) < 4:
        return
    try:
        blocks = await analyze_reply_dynamics(
            incoming,
            my_sample,
            contact_sample,
            samples["features_summary"],
        )
    except Exception:
        logging.exception("reply-analysis: не удалось сгенерировать разбор")
        return
    if blocks:
        await message.answer("🧭 Разбор переписки:\n\n" + _format_blocks(blocks))


_VARIANT_LETTERS = "АБВГДЕЁЖЗИ"


def _format_variants(variants: list[tuple[str, str]]) -> str:
    blocks = []
    for i, (name, text) in enumerate(variants):
        letter = _VARIANT_LETTERS[i] if i < len(_VARIANT_LETTERS) else str(i + 1)
        blocks.append(f"Вариант {letter}: {name}\n- {text}")
    return "Вот несколько вариантов — выбирай стиль или комбинируй.\n\n" + "\n\n".join(blocks)


def variants_result_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Другой тон", callback_data=f"varother:{action_id}")
    b.button(text="🔄 Другие варианты", callback_data=f"varregen:{action_id}")
    b.adjust(2)
    return b.as_markup()


async def _run_reply_variants(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str, force_fresh: bool = False,
) -> None:
    """Генерирует несколько именованных вариантов ответа ОДНИМ вызовом LLM для
    «Ответить за меня» (замена прежнего «сначала выбери стиль»). Гейт и списание
    триала — один раз за вызов (не за каждый вариант), т.к. это один вызов LLM."""
    text = ctx.get("text")
    if text is None:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card, interaction_card = ctx["style_card"], ctx["interaction_card"]
    signals = ctx.get("data_signals")
    winning = ctx.get("winning")
    cache_key = _style_cache_key("reply_variants", "", text, style_card, interaction_card)

    variants = None
    if not force_fresh:
        cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
        if cached:
            try:
                variants = [tuple(v) for v in json.loads(cached)]
                logging.info("reply-variants: cache hit")
            except (ValueError, TypeError):
                variants = None

    if variants is None:
        # Реальный вызов LLM — здесь и только здесь гейт + списание.
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        prev = ctx.get("variants") if force_fresh else None
        try:
            variants = await suggest_reply_variants(
                text, style_card, interaction_card,
                data_signals=signals, previous_variants=prev, winning_examples=winning,
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("reply-variants: ошибка генерации")
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return

        # Успех — списываем ОДНУ попытку (не за каждый вариант — это один вызов
        # LLM) и кэшируем, даже если разбор дал меньше вариантов, чем просили.
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps(variants, ensure_ascii=False))
        try:
            record_event(str(telegram_id), "gen_reply_variants", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие генерации вариантов")

    if not variants:
        await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
        return

    ctx["variants"] = variants
    await _answer_long(target, _format_variants(variants), reply_markup=variants_result_kb(action_id))
    await target.answer(
        "Пришли следующее сообщение собеседника, чтобы ответить и на него. "
        "Чтобы выйти из режима — нажми любую кнопку меню."
    )


@dp.callback_query(F.data.startswith("varother:"))
async def cb_variants_other(call: CallbackQuery) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") != "reply":
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer()
    await call.message.answer("В каком стиле ответить?", reply_markup=style_pick_kb(action_id))


@dp.callback_query(F.data.startswith("varregen:"))
async def cb_variants_regen(call: CallbackQuery) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") != "reply":
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer("Подбираю другие варианты...")
    await _run_reply_variants(call.message, ctx, call.from_user.id, call.bot, action_id, force_fresh=True)


@dp.message(ReplyHelp.waiting_for_incoming, _not_command)
async def handle_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await message.answer("Пришли сообщение собеседника текстом или голосовым.")
        return

    data = await state.get_data()
    # Состояние НЕ сбрасываем — иначе следующее сообщение улетит в общий
    # авто-режим («Переписать») вместо продолжения «Ответить за меня».
    # Выйти из режима — любая кнопка меню (handle_menu_button сбрасывает state).

    if not await _quota_gate(bot, message, str(message.from_user.id)):
        return

    contact_id = data.get("contact_id")
    # «Разбор переписки» (_send_reply_analysis) здесь отключён намеренно:
    # пользователь ждёт просто ответ, а не аналитику перед каждым ответом.
    # Вернуть — один вызов: await _send_reply_analysis(message, contact_id, incoming)
    samples = get_message_samples(contact_id) if contact_id else None
    ctx = {
        "kind": "reply", "text": incoming, "result": None, "style": None,
        "style_card": data["style_card"], "interaction_card": data["interaction_card"],
        "data_signals": _reply_data_signals(samples, incoming),
        "winning": _winning_for_contact(str(message.from_user.id), contact_id),
    }
    action_id = _new_action(message.from_user.id, ctx)
    await _run_reply_variants(message, ctx, message.from_user.id, bot, action_id)


@dp.message(Command("reply"))
async def cmd_reply(message: Message, state: FSMContext) -> None:
    await _start_reply(message, state)


# ── 💫 Живой диалог с нуля (холодный старт, без порога накопления) ───────────

_LIVE_NEUTRAL_STYLE_PLACEHOLDER = (
    "Данных о твоём стиле письма пока нет — пиши нейтрально: разговорной "
    "длиной, без выраженного регистра/тона, без домыслов о привычках автора. "
    "Как только появятся другие данные (JSON-экспорт, другие переписки), "
    "стиль подключится сам."
)

LIVE_NOTES_SUMMARY_EVERY = 4  # раз в сколько сообщений показывать «что я уже понял»


def _running_notes_preview(notes_text: str, n: int = 2) -> str:
    """Последние n непустых строк заметок — для короткого «что я уже понял»."""
    lines = [ln.strip() for ln in (notes_text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def live_variants_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"liveregen:{action_id}")
    return b.as_markup()


async def _start_live_dialogue(message: Message, state: FSMContext) -> None:
    await state.set_state(LiveDialogue.waiting_for_name)
    await message.answer(
        "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов."
    )


@dp.message(LiveDialogue.waiting_for_name)
async def handle_live_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пришли имя текстом.")
        return

    telegram_id = str(message.from_user.id)
    upsert_user(telegram_id, f"user{telegram_id}")
    contact_id = get_or_create_contact(telegram_id, f"live_{uuid.uuid4().hex}", name)

    await state.set_state(LiveDialogue.waiting_for_incoming)
    await state.update_data(contact_id=contact_id, dialogue_history=[])
    await message.answer(
        f"Готово — «{name}». Присылай её сообщения по одному, на каждое сразу дам "
        "несколько вариантов ответа. Чтобы выйти из режима — нажми любую кнопку меню."
    )


@dp.message(LiveDialogue.waiting_for_incoming, _not_command)
async def handle_live_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await message.answer("Пришли сообщение собеседницы текстом или голосовым.")
        return

    data = await state.get_data()
    # Состояние НЕ сбрасываем — можно форвардить сообщения одно за другим без
    # повторного нажатия кнопки. Выйти из режима — любая кнопка меню.

    if not await _quota_gate(bot, message, str(message.from_user.id)):
        return

    contact_id = data.get("contact_id")
    if not contact_id:
        await message.answer("Контекст диалога потерян — начни заново через «💫 Новый диалог».")
        return

    telegram_id = str(message.from_user.id)
    style_card = await _gen_style_card(telegram_id) or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
    notes_row = get_running_notes(contact_id)
    running_notes = notes_row["notes_text"] if notes_row else None
    message_count = notes_row["message_count"] if notes_row else 0
    dialogue_history = data.get("dialogue_history") or []

    ctx = {
        "kind": "live", "text": incoming, "contact_id": contact_id,
        "style_card": style_card, "running_notes": running_notes,
        "dialogue_history": dialogue_history, "message_count": message_count,
        "variants": None,
    }
    action_id = _new_action(message.from_user.id, ctx)

    # Короткая история диалога — эфемерно, в FSM; долгая память — running_notes в БД.
    new_history = (dialogue_history + [incoming])[-8:]
    await state.update_data(dialogue_history=new_history)

    await _run_live_coach_step(message, ctx, message.from_user.id, bot, action_id)


async def _run_live_coach_step(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str, force_fresh: bool = False,
) -> None:
    """«Живой диалог»: первый проход — live_coach_step (советы + допись заметок,
    одна попытка триала на пересланное сообщение). «Другие варианты» — просто
    suggest_reply_variants поверх уже сохранённых заметок, БЕЗ повторной записи
    в running_notes (иначе один и тот же инсайт задвоился бы в заметках)."""
    text = ctx.get("text")
    contact_id = ctx.get("contact_id")
    if text is None or not contact_id:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card = ctx["style_card"]
    running_notes = ctx.get("running_notes") or ""

    if force_fresh:
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        try:
            variants = await suggest_reply_variants(
                text, style_card, running_notes, previous_variants=ctx.get("variants"),
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("live-coach: ошибка регена вариантов")
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return
        await _charge_trial_if_needed(bot, str(telegram_id))
        try:
            record_event(str(telegram_id), "gen_live_regen", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие live-регена")
        if not variants:
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return
        ctx["variants"] = variants
        await _answer_long(target, _format_variants(variants), reply_markup=live_variants_kb(action_id))
        return

    cache_key = _style_cache_key("live", "", text, style_card, running_notes)
    cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
    variants = updated_notes = None
    if cached:
        try:
            payload = json.loads(cached)
            variants = [tuple(v) for v in payload["variants"]]
            updated_notes = payload["notes"]
            logging.info("live-coach: cache hit")
        except (ValueError, TypeError, KeyError):
            variants = updated_notes = None

    if variants is None:
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        try:
            variants, updated_notes = await live_coach_step(
                text, style_card, running_notes or None, ctx.get("dialogue_history"),
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("live-coach: ошибка генерации")
            await target.answer("Не получилось сгенерировать совет — попробуй ещё раз.")
            return

        # Успех — списываем ОДНУ попытку (один вызов LLM даёт и советы, и заметки).
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps({"variants": variants, "notes": updated_notes}, ensure_ascii=False))
        try:
            record_event(str(telegram_id), "gen_live", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие live-генерации")

        new_count = ctx.get("message_count", 0) + 1
        save_running_notes(contact_id, updated_notes, new_count)
        ctx["message_count"] = new_count

    if not variants:
        await target.answer("Не получилось сгенерировать совет — попробуй ещё раз.")
        return

    ctx["variants"] = variants
    ctx["running_notes"] = updated_notes
    await _answer_long(target, _format_variants(variants), reply_markup=live_variants_kb(action_id))

    message_count = ctx.get("message_count", 0)
    if updated_notes and (message_count == 1 or message_count % LIVE_NOTES_SUMMARY_EVERY == 0):
        preview = _running_notes_preview(updated_notes)
        if preview:
            await target.answer(f"Что я уже понял:\n{preview}")

    await target.answer(
        "Пришли следующее сообщение собеседницы — отвечу и на него. "
        "Чтобы выйти из режима — нажми любую кнопку меню."
    )


@dp.callback_query(F.data.startswith("liveregen:"))
async def cb_live_regen(call: CallbackQuery) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") != "live":
        await call.answer("Контекст устарел — начни заново через «💫 Новый диалог».", show_alert=True)
        return
    await call.answer("Подбираю другие варианты...")
    await _run_live_coach_step(call.message, ctx, call.from_user.id, call.bot, action_id, force_fresh=True)


# ── 📸 Ответить по скриншоту ──────────────────────────────────────────────────

async def _start_screenshot(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)
    if not list_contacts(telegram_id):
        await message.answer("Сначала загрузи JSON-файл чата.")
        return
    await state.set_state(Screenshot.waiting_for_image)
    await message.answer("Пришли скриншот переписки (или вставь текст диалога), на который нужно ответить:")


@dp.message(Command("screenshot"))
async def cmd_screenshot(message: Message, state: FSMContext) -> None:
    await _start_screenshot(message, state)


@dp.message(Screenshot.waiting_for_image, F.photo)
async def handle_screenshot_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    await message.answer("Читаю скриншот...")
    try:
        buf = await bot.download(message.photo[-1])
        chat_text = await extract_chat_from_image(buf.read())
    except Exception:
        logging.exception("screenshot: не удалось скачать/распознать")
        chat_text = ""

    if not chat_text or chat_text.strip() == ILLEGIBLE_MARKER:
        await message.answer("Не смог прочитать скриншот — пришли текст переписки сообщением.")
        return  # остаёмся в Screenshot.waiting_for_image

    await _proceed_screenshot_style_pick(message, state, chat_text)


@dp.message(Screenshot.waiting_for_image, F.text)
async def handle_screenshot_text(message: Message, state: FSMContext) -> None:
    chat_text = (message.text or "").strip()
    if not chat_text:
        await message.answer("Пришли скриншот или текст переписки.")
        return
    await _proceed_screenshot_style_pick(message, state, chat_text)


def screenshot_contact_pick_kb(contacts: list, action_id: str) -> InlineKeyboardMarkup:
    """Как contacts_kb, но с кнопкой для человека, которого ещё нет в базе —
    для него используется общий (агрегатный) стиль, без interaction_card."""
    b = InlineKeyboardBuilder()
    for c in contacts:
        b.button(text=_contact_name(c), callback_data=f"shotcontact:{c['id']}:{action_id}")
    b.button(text="🆕 Новый человек (нет в базе)", callback_data=f"shotcontact:new:{action_id}")
    b.adjust(1)
    return b.as_markup()


async def _proceed_screenshot_style_pick(message: Message, state: FSMContext, chat_text: str) -> None:
    await state.clear()
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)

    action_id = _new_action(message.from_user.id, {"kind": "screenshot_pending", "chat_text": chat_text})
    await message.answer("Чья это переписка?", reply_markup=screenshot_contact_pick_kb(contacts, action_id))


@dp.callback_query(F.data.startswith("shotcontact:"))
async def cb_screenshot_contact(call: CallbackQuery, bot: Bot) -> None:
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
        return
    _, raw_id, action_id = parts
    telegram_id = str(call.from_user.id)

    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") != "screenshot_pending":
        await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
        return

    if raw_id == "new":
        await call.answer()
        await _prompt_screenshot_style_no_contact(bot, call.message, call.from_user.id, telegram_id, ctx["chat_text"], edit=True)
        return

    contact_id = int(raw_id)
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    await _prompt_screenshot_style(bot, call.message, call.from_user.id, telegram_id, contact_id, ctx["chat_text"], edit=True)


async def _prompt_screenshot_style(
    bot: Bot, target: Message, user_id: int, telegram_id: str, contact_id: int, chat_text: str, edit: bool = False
) -> None:
    # ВАЖНО: user_id передаётся отдельным параметром, а не берётся из
    # target.from_user — при edit=True target это call.message, чей
    # .from_user это БОТ, а не пользователь (стандартная ловушка aiogram).
    if not await _quota_gate(bot, target, telegram_id):
        return
    # Генерация карточек ходит в LLM — без обработки ошибок сбой (лимит/провайдер
    # недоступен) тихо убивал кнопку: спиннер гас, а сообщение не менялось.
    try:
        style_card = await _style_for_rewrite(telegram_id, contact_id)
        interaction_card = (await _gen_interaction_card(contact_id, telegram_id) or "") if style_card else ""
    except RateLimitError:
        await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
        return
    except Exception:
        logging.exception("screenshot: не удалось сгенерировать карточки")
        await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
        return
    if not style_card:
        text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
        await (target.edit_text(text) if edit else target.answer(text))
        return

    samples = get_message_samples(contact_id)
    action_id = _new_action(user_id, {
        "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
        "style_card": style_card, "interaction_card": interaction_card,
        "data_signals": _reply_data_signals(samples, _last_incoming_line(chat_text)),
        "winning": _winning_for_contact(telegram_id, contact_id),
    })
    text = "В каком стиле ответить?"
    kb = style_pick_kb(action_id)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _prompt_screenshot_style_no_contact(
    bot: Bot, target: Message, user_id: int, telegram_id: str, chat_text: str, edit: bool = False
) -> None:
    """Для человека, которого ещё нет в базе — общий (агрегатный) стиль автора,
    без per-contact interaction_card (промпт сам подставит нейтральный фолбэк)."""
    if not await _quota_gate(bot, target, telegram_id):
        return
    try:
        style_card = await _gen_style_card(telegram_id)
    except RateLimitError:
        await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
        return
    except Exception:
        logging.exception("screenshot(new): не удалось сгенерировать стиль")
        await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
        return
    if not style_card:
        text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
        await (target.edit_text(text) if edit else target.answer(text))
        return

    action_id = _new_action(user_id, {
        "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
        "style_card": style_card, "interaction_card": "",
        "data_signals": _reply_data_signals(None, _last_incoming_line(chat_text)),
    })
    text = "В каком стиле ответить?"
    kb = style_pick_kb(action_id)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# ── /rebuild — принудительная пересборка всех карточек ───────────────────────

@dp.message(Command("rebuild"))
async def cmd_rebuild(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    if not await _require_premium(bot, message, telegram_id):
        return
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
            logging.exception("rebuild failed for contact_id=%s", c["id"])

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
        "Вот что я умею (то же самое есть и кнопками в меню):\n\n"
        "📝 Написать за тебя — выбор стиля у каждого: флирт/юмор/нежно/"
        "уверенно/дружески/формально\n"
        "/rewrite — переписать свой черновик под собеседника\n"
        "/reply — ответить на его сообщение\n"
        "/screenshot — ответить по скриншоту переписки (можно слать скриншоты "
        "один за другим)\n\n"
        "🔬 Разобраться\n"
        "/deep_analysis — совместимость, история отношений, стиль и привычки "
        "собеседника, идеи подарков\n"
        "/deep_style_analysis — твой коммуникативный профиль и советы для дейтинга\n"
        "/compare — сравнить, как ты пишешь разным людям\n"
        "/stats — портрет в цифрах, бесплатно\n\n"
        "⚙️ Аккаунт\n"
        "/contacts — список загруженных чатов\n"
        "/connect — как подключить Автоматизацию чатов (живой поток переписки)\n"
        "/auto — вкл/выкл авто-режим: когда включён, любой присланный текст "
        "сразу предлагается переписать, без команды и кнопки\n"
        "/premium — статус подписки\n"
        "/rebuild — принудительно пересобрать все карточки заново\n"
        "/delete — удалить свои данные\n\n"
        "🎬 Остальное\n"
        "/start — начало работы\n"
        "/demo — попробовать на примере\n"
        "/help — это сообщение\n\n"
        f"💎 {FREE_TRIAL_REQUESTS} бесплатных попыток на переписать/ответить/скриншот, "
        "дальше и остальные функции — по подписке. Статус — /premium."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show_help(message)


# ── /premium — статус подписки ────────────────────────────────────────────────

@dp.message(Command("premium"))
async def cmd_premium(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    if await _is_premium(bot, telegram_id):
        await message.answer("💎 Подписка CueMe Premium активна — весь функционал без ограничений.")
        return

    used = get_trial_used(telegram_id)
    left = max(0, FREE_TRIAL_REQUESTS - used)
    await message.answer(
        f"Бесплатных попыток осталось: {left} из {FREE_TRIAL_REQUESTS} "
        "(Переписать / Ответить за меня / По скриншоту).\n"
        "Глубокий анализ, стиль собеседника и сравнение стилей — только по подписке.",
        reply_markup=paywall_kb(),
    )


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
async def cmd_compare(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    if not await _require_premium(bot, message, telegram_id):
        return
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
        await _answer_long(message, result)
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

@dp.message(Command("auto"))
async def cmd_auto(message: Message) -> None:
    """Переключатель авто-режима: когда включён — любой присланный текст предлагается
    переписать без нажатия кнопки. По умолчанию управляется явно."""
    telegram_id = str(message.from_user.id)
    enabled, contact_id = get_auto_mode(telegram_id)

    if enabled:
        set_auto_mode(telegram_id, False, contact_id)
        await message.answer(
            "🔕 Авто-режим выключен. Произвольный текст больше не превращается в черновик — "
            "жми «📝 Переписать», когда нужно. Включить снова: /auto"
        )
        return

    # Включаем — нужен целевой контакт
    if not contact_id:
        contacts = list_contacts(telegram_id)
        if not contacts:
            await message.answer(
                "Сначала загрузи чат или подключи Автоматизацию — тогда будет кого переписывать."
            )
            return
        if len(contacts) == 1:
            contact_id = contacts[0]["id"]
        else:
            await message.answer(
                "У тебя несколько контактов. Выбери целевой через настройку контакта, "
                "потом снова /auto."
            )
            return

    set_auto_mode(telegram_id, True, contact_id)
    c = get_contact_by_id(contact_id)
    name = _contact_name(c) if c else "контакт"
    await message.answer(
        f"🔔 Авто-режим включён — {name}. Любое присланное сообщение предложу переписать "
        "под этот стиль. Выключить: /auto"
    )


@dp.message(F.text & ~F.text.in_(_ALL_BTNS))
async def auto_rewrite_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if await state.get_state() is not None:
        return

    telegram_id = str(message.from_user.id)
    # Explicit-гейт: реагируем на произвольный текст ТОЛЬКО если авто-режим явно включён
    # (переключается командой /auto). Иначе случайное сообщение не превращается в черновик.
    enabled, contact_id = get_auto_mode(telegram_id)
    if not enabled:
        return
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

    if not await _quota_gate(bot, message, telegram_id):
        return

    draft = message.text.strip()
    action_id = _new_action(message.from_user.id, {
        "kind": "rewrite", "text": draft, "result": None, "style": None,
        "style_card": style_card, "interaction_card": interaction_card,
    })
    await message.answer("В каком стиле переписать?", reply_markup=style_pick_kb(action_id))


# ── запуск ────────────────────────────────────────────────────────────────────

def _validate_startup_config() -> None:
    """Fail-fast проверка до запуска polling. Хотя бы один LLM-ключ обязателен —
    иначе бот не сможет генерировать ответы. Отсутствие отдельных ключей — warning
    (каскад их просто пропустит)."""
    keys = {
        "GEMINI_API_KEY":     GEMINI_API_KEY,
        "GROQ_API_KEY":       GROQ_API_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    }
    present = [name for name, val in keys.items() if val]
    for name, val in keys.items():
        if not val:
            logging.warning("%s не задан — провайдер будет пропускаться в каскаде.", name)
    if not present:
        raise RuntimeError(
            "Не задан ни один LLM-ключ (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY). "
            "Бот не сможет генерировать ответы — заполни .env."
        )
    if not GROQ_API_KEY:
        logging.warning(
            "GROQ_API_KEY не задан — распознавание голоса/скриншотов пойдёт только "
            "через Gemini-fallback."
        )
    logging.info("Конфиг проверен. Доступные LLM-ключи: %s", ", ".join(present))


async def main() -> None:
    _validate_startup_config()
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
        BotCommand(command="screenshot",  description="Ответить по скриншоту"),
        BotCommand(command="reply",       description="Помочь ответить собеседнику"),
        BotCommand(command="auto",        description="Вкл/выкл авто-режим переписывания"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
        BotCommand(command="deep_analysis", description="Глубокий анализ отношений"),
        BotCommand(command="deep_style_analysis", description="Глубокий анализ моего стиля"),
        BotCommand(command="premium",     description="Статус подписки"),
        BotCommand(command="delete",      description="Удалить свои данные"),
        BotCommand(command="rebuild",     description="Пересобрать все карточки"),
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
