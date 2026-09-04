import asyncio
import csv
import difflib
import hashlib
import html
import io
import itertools
import json
import logging
import random
import re
import string
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    BusinessConnection,
    CallbackQuery, Document, ErrorEvent, FSInputFile, InputRichMessage, Message,
    InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import (
    ADMIN_GROUP_CHAT_ID,
    ADMIN_TELEGRAM_IDS,
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
    ONBOARDING_PHOTO_FILE_ID,
    ONBOARDING_PHOTO_PATH,
    ONBOARDING_JSON_POST_URL,
    OPENERS_FOR_HER,
    OPENERS_FOR_HIM,
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REWARD_DAYS,
    REBUILD_THRESHOLD,
    REFERRAL_REWARD_DAYS,
    REFRESH_SAMPLES_EVERY_N,
    REVIVE_QUESTIONS,
    SAMPLE_SIZE,
    STAR_PRICE_DAY,
    STAR_PRICE_WEEK,
    STAR_PRICE_MONTH,
    STARS_SUBSCRIPTION_PERIOD,
    TEST_ACCOUNT_USERNAMES,
)
from features import detect_reply_situation, extract_features, stage_hint, totals_from_summary, winning_messages
from llm import (
    ILLEGIBLE_MARKER,
    PROVIDER_NAMES,
    RateLimitError,
    build_compatibility_interpretation,
    build_ideal_date,
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
    extract_chat_from_image,
    analyze_reply_dynamics,
    get_forced_provider,
    get_provider_stats,
    live_coach_step,
    make_features_summary,
    sample_texts,
    screenshot_variants,
    set_forced_provider,
    suggest_reply_variants,
    transcribe_audio,
)
from compatibility_metrics import compute_all as compute_compat_metrics
from tg_parser import parse_chat
from tools.export import extract_conversation, to_html, to_text
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    count_successful_referrals,
    delete_all_user_data,
    delete_contact_data,
    delete_deep_analysis,
    delete_ideal_date,
    delete_style_card,
    event_counts_by_user,
    find_contact_by_original_id,
    get_all_dated_messages,
    get_all_dated_my_messages,
    get_all_per_contact_style_cards,
    get_any_user_samples,
    get_biz_messages_for_contact,
    get_business_connection,
    get_business_connections_history,
    get_contact_last_messages,
    get_recent_unmatched_suggestions,
    get_latest_business_connection,
    get_contact_by_id,
    get_deep_analysis,
    get_acquisition_source,
    get_deep_analysis_free_until,
    get_gender,
    get_promo_channel_premium_until,
    get_ideal_date,
    get_last_event_time,
    get_last_incoming_message_time,
    get_latest_star_payment,
    get_llm_cache,
    get_interaction_card,
    get_imported_messages,
    get_message_samples,
    get_or_create_referral_code,
    get_pending_referral,
    get_referrer_by_code,
    get_stars_premium_until,
    get_trial_used,
    get_user,
    increment_trial_used,
    mark_bot_blocked,
    mark_bot_unblocked,
    mark_referral_credited,
    save_imported_messages,
    get_my_style_last_rebuild_count,
    get_my_style_per_contact,
    get_or_create_contact,
    get_running_notes,
    get_style_card,
    init_db,
    list_all_users,
    list_contacts,
    mark_suggestion_matched,
    referral_counts_by_user,
    save_business_message,
    save_deep_analysis,
    save_ideal_date,
    save_interaction_card,
    save_message_samples,
    save_my_style_per_contact,
    save_referral_pending,
    save_running_notes,
    save_style_card,
    save_suggestions,
    suggestion_stats_by_user,
    record_event,
    record_star_payment,
    set_acquisition_source,
    set_deep_analysis_free_until,
    set_gender,
    set_promo_channel_reward,
    set_stars_premium_until,
    has_claimed_promo_reward,
    set_llm_cache,
    update_contact_username,
    upsert_business_connection,
    upsert_chat_ref_mapping,
    upsert_user,
    users_with_deep_analysis,
    users_with_style_card,
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
BTN_HELP          = "❓ Помощь"
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
    BTN_UNIFIED, BTN_DEEP, BTN_DATE, BTN_SUBSCRIPTION, BTN_HELP,
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
    подписка на промо-канал)."""
    b = InlineKeyboardBuilder()
    if PREMIUM_SUBSCRIBE_URL:
        b.button(text="💎 Оформить подписку", url=PREMIUM_SUBSCRIBE_URL)
    b.button(text="⭐ Оплатить Stars", callback_data="stars_menu")
    b.button(text="🎁 Пригласи друга", callback_data="show_invite")
    b.button(text="📢 Подписаться на канал", callback_data="promo:offer")
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


async def _send_paywall(target: Message, text: str) -> None:
    await target.answer(text, reply_markup=premium_menu_kb())


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
        "Бесплатные попытки закончились — но, похоже, тебе заходит 😏 Дальше — "
        "по подписке: весь функционал плюс полный разбор собеседника с подарками."
    )
    return False


async def _charge_trial_if_needed(bot: Bot, telegram_id: str) -> None:
    """Списывает одну попытку триала. Вызывать ТОЛЬКО после успешного ответа LLM.
    Premium попытки не тратит."""
    if await _is_premium(bot, telegram_id):
        return
    increment_trial_used(telegram_id)


async def _require_premium(bot: Bot, target: Message, telegram_id: str) -> bool:
    """Гейт для функций без бесплатного триала (анализ собеседника, стиль
    собеседника и т.п.) — доступ только по активной подписке."""
    if await _is_premium(bot, telegram_id):
        return True

    await _send_paywall(target, "Эта функция доступна только по подписке CueMe Premium.")
    return False


# ── Реферальная программа ─────────────────────────────────────────────────────
# Пригласивший получает REFERRAL_REWARD_DAYS дней полной Premium-подписки,
# когда друг реально начинает пользоваться ботом (создан первый контакт).
# Друг вводит персональный код пригласившего командой /redeem — см.
# cmd_redeem ниже для анти-абуз проверок.


async def _credit_referral_if_pending(bot: Bot, referred_id: str) -> None:
    """Друг реально начал пользоваться (создан первый контакт) → начисляем
    рефереру Premium-награду и уведомляем. Каждый новый друг НАКАПЛИВАЕТ
    награду — REFERRAL_REWARD_DAYS прибавляются к уже активному окну (если
    оно ещё не истекло), а не перезаписывают его с текущего момента.
    Идемпотентно: credited-флаг + PRIMARY KEY(referred_id) не дают начислить
    дважды за одного и того же друга."""
    referrer_id = get_pending_referral(referred_id)
    if not referrer_id:
        return
    now = datetime.now(timezone.utc)
    current_until = get_deep_analysis_free_until(referrer_id)
    base = current_until if current_until and current_until > now else now
    until = base + timedelta(days=REFERRAL_REWARD_DAYS)
    set_deep_analysis_free_until(referrer_id, until)
    mark_referral_credited(referred_id)
    try:
        await bot.send_message(
            int(referrer_id),
            "🎉 Твой друг начал пользоваться CueMe! Держи подарок — "
            f"+{REFERRAL_REWARD_DAYS} дня Premium подписки "
            f"(до {until.strftime('%d.%m.%Y %H:%M UTC')}).",
        )
    except Exception:
        logging.warning("referral notify failed: referrer=%s", referrer_id)


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


# ── Награда за подписку на промо-канал (ТРЕТИЙ бесплатный путь к Premium) ────
# PROMO_CHANNEL_USERNAME — ПУБЛИЧНЫЙ промо-канал (t.me/CueMee), НЕ путать с
# PREMIUM_CHANNEL_ID (приватный канал-пропуск Tribute, платный, отдельная
# механика в _is_premium). Награда — ОДИН раз за всё время: has_claimed_promo_reward
# навсегда true после первого начисления, отписка-подписка заново не даёт дубль.

@dp.callback_query(F.data == "promo:offer")
async def cb_promo_offer(call: CallbackQuery) -> None:
    await call.answer()
    if has_claimed_promo_reward(str(call.from_user.id)):
        await call.message.answer("Ты уже получал эту награду раньше 🙂")
        return
    await call.message.answer(
        f"Подпишись на {PROMO_CHANNEL_USERNAME} и получи "
        f"{PROMO_CHANNEL_REWARD_DAYS} дня Premium бесплатно:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📢 Открыть канал",
                url=f"https://t.me/{PROMO_CHANNEL_USERNAME.lstrip('@')}",
            )],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="promo:check")],
        ]),
    )


@dp.callback_query(F.data == "promo:check")
async def cb_promo_check(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    if has_claimed_promo_reward(telegram_id):
        await call.answer("Уже получено раньше", show_alert=True)
        return

    try:
        member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
        subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        logging.exception("promo channel check failed for %s", telegram_id)
        subscribed = False

    if not subscribed:
        await call.answer("Не вижу подписки — проверь и попробуй снова", show_alert=True)
        return

    until = datetime.now(timezone.utc) + timedelta(days=PROMO_CHANNEL_REWARD_DAYS)
    set_promo_channel_reward(telegram_id, until)
    await call.answer()
    await call.message.answer(f"🎉 Готово! {PROMO_CHANNEL_REWARD_DAYS} дня Premium активны.")


# ── Stars-подписка (Telegram Stars, XTR) ──────────────────────────────────────
# Второй, независимый способ оплаты рядом с Tribute — НЕ канал-пропуск: Stars
# просто открывает окно users.stars_premium_until (см. _has_stars_premium),
# без членства в PREMIUM_CHANNEL_ID. День/неделя — разовая покупка (Telegram
# не поддерживает Stars-подписки короче 30 дней), продлевать нужно вручную —
# бот предложит это через _premium_status_text/paywall, когда окно истекло.
# Месяц — нативная Stars-подписка (subscription_period=STARS_SUBSCRIPTION_PERIOD,
# ровно 30 дней — единственное значение, которое принимает Telegram) с
# автопродлением; списывается Telegram-ом самостоятельно, отменяется
# пользователем через настройки Telegram, не через бота.

_STARS_TIERS = {
    "day":   {"stars": STAR_PRICE_DAY,   "days": 1,  "title": "CueMe Premium — 1 день"},
    "week":  {"stars": STAR_PRICE_WEEK,  "days": 7,  "title": "CueMe Premium — 1 неделя"},
    "month": {"stars": STAR_PRICE_MONTH, "days": 30, "title": "CueMe Premium — 1 месяц (автопродление)"},
}


@dp.callback_query(F.data == "stars_menu")
async def cb_stars_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(
        "⭐ Оплата Telegram Stars — прямо в Telegram, без сторонних сайтов. "
        "Выбери тариф:",
        reply_markup=stars_tariff_kb(),
    )


@dp.callback_query(F.data == "stars_back")
async def cb_stars_back(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_premium_screen(call.message, bot, str(call.from_user.id))


@dp.callback_query(F.data.startswith("stars_buy:"))
async def cb_stars_buy(call: CallbackQuery, bot: Bot) -> None:
    tier = call.data.split(":", 1)[1]
    tier_info = _STARS_TIERS.get(tier)
    await call.answer()
    if not tier_info:
        return

    telegram_id = str(call.from_user.id)
    stars = tier_info["stars"]
    title = tier_info["title"]
    description = (
        "Доступ ко всем функциям CueMe (переписать, анализ собеседника, "
        "анализ стиля и т.д.) на выбранный срок."
    )
    # payload — служебный, не показывается юзеру: tier нужен в successful_payment,
    # чтобы понять, сколько дней/автопродление начислить; telegram_id и рандомный
    # хвост — на случай отладки по логам, при обработке доверяем call.from_user, не payload.
    payload = f"stars:{tier}:{telegram_id}:{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label=title, amount=stars)]

    if tier == "month":
        # sendInvoice в этой версии Bot API/aiogram НЕ принимает subscription_period —
        # нативная Stars-подписка создаётся только через createInvoiceLink, поэтому
        # для месяца шлём ссылку с Pay-кнопкой, а не инвойс напрямую в чат.
        try:
            link = await bot.create_invoice_link(
                title=title, description=description, payload=payload,
                currency="XTR", prices=prices, provider_token="",
                subscription_period=STARS_SUBSCRIPTION_PERIOD,
            )
        except Exception:
            logging.exception("stars: не удалось создать invoice link (месяц) для %s", telegram_id)
            await call.message.answer("Не получилось создать ссылку на оплату — попробуй ещё раз позже.")
            return
        b = InlineKeyboardBuilder()
        b.button(text=f"⭐ Оформить за {stars} Stars", url=link)
        await call.message.answer(
            f"{title}\n\nАвтопродление каждые 30 дней, отменить можно в любой "
            "момент в настройках Telegram → Мои подписки.",
            reply_markup=b.as_markup(),
        )
        return

    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id, title=title, description=description,
            payload=payload, currency="XTR", prices=prices, provider_token="",
        )
    except Exception:
        logging.exception("stars: не удалось отправить инвойс (%s) для %s", tier, telegram_id)
        await call.message.answer("Не получилось выставить счёт — попробуй ещё раз позже.")


@dp.pre_checkout_query()
async def process_stars_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    """ok=True после базовой валидации payload — своих провайдер-проверок для
    Stars не требуется (provider_token=''), но формат должен быть наш и
    сумма — ровно тарифная, иначе отклоняем, чтобы не провести левый платёж."""
    parts = pre_checkout_query.invoice_payload.split(":")
    ok = (
        len(parts) == 4
        and parts[0] == "stars"
        and parts[1] in _STARS_TIERS
        and pre_checkout_query.currency == "XTR"
        and pre_checkout_query.total_amount == _STARS_TIERS[parts[1]]["stars"]
    )
    await bot.answer_pre_checkout_query(
        pre_checkout_query.id, ok=ok,
        error_message=None if ok else "Счёт устарел или повреждён — попробуй оплатить заново.",
    )


@dp.message(F.successful_payment)
async def process_stars_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    if sp.currency != "XTR":
        return  # не наш платёж — Tribute вообще не идёт через Bot Payments API

    parts = sp.invoice_payload.split(":")
    if len(parts) != 4 or parts[0] != "stars" or parts[1] not in _STARS_TIERS:
        logging.warning("stars: successful_payment с неожиданным payload %r", sp.invoice_payload)
        return

    tier = parts[1]
    telegram_id = str(message.from_user.id)  # доверяем from_user, не payload
    is_subscription = sp.subscription_expiration_date is not None

    if is_subscription:
        expires_at = datetime.fromtimestamp(sp.subscription_expiration_date, tz=timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=_STARS_TIERS[tier]["days"])

    # Telegram может задублировать доставку successful_payment — record_star_payment
    # идемпотентен по charge_id (UNIQUE), применяем окно Premium только на первой записи.
    is_new = record_star_payment(
        telegram_id=telegram_id, tier=tier, stars_amount=sp.total_amount,
        charge_id=sp.telegram_payment_charge_id, is_subscription=is_subscription,
        expires_at=expires_at,
    )
    if not is_new:
        return

    set_stars_premium_until(telegram_id, expires_at)
    record_event(telegram_id, "stars_payment", f"{tier}:{sp.total_amount}")

    until_label = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    extra = " Продлится автоматически, спишется ещё раз через 30 дней." if is_subscription else ""
    await message.answer(f"🎉 Готово! Premium активен до {until_label}.{extra}")


def _invite_text(telegram_id: str) -> str:
    """Тело приглашения — общее для /invite и рассылки-напоминания
    (cmd_broadcast_invite), чтобы формулировка гарантированно не разъезжалась."""
    code = get_or_create_referral_code(telegram_id)
    count = count_successful_referrals(telegram_id)

    if _has_referral_premium(telegram_id):
        until = get_deep_analysis_free_until(telegram_id)
        reward_line = f"✅ Premium подписка (по рефералам) активна до {until.strftime('%d.%m.%Y %H:%M UTC')}\n"
    else:
        reward_line = ""

    return (
        "🎁 Пригласи друга\n\n"
        f"👥 Приведено друзей: {count}\n"
        f"{reward_line}\n"
        "Скинь другу этот код — пусть введёт его командой /redeem в этом боте.\n"
        "Как только он реально начнёт пользоваться CueMe — тебе дадутся "
        f"{REFERRAL_REWARD_DAYS} дня Premium подписки:\n\n"
        f"<code>/redeem {html.escape(code)}</code>\n\n"
        "(тапни по коду, чтобы скопировать)"
    )


async def _show_invite(message: Message, bot: Bot, telegram_id: str | None = None) -> None:
    telegram_id = telegram_id or str(message.from_user.id)
    await message.answer(_invite_text(telegram_id), parse_mode="HTML")


@dp.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    await _show_invite(message, bot)


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
            text = "💡 Кстати, забыл сказать —\n\n" + _invite_text(telegram_id)
            await bot.send_message(int(telegram_id), text, parse_mode="HTML")
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


class ReferralRedeem(StatesGroup):
    waiting_for_code = State()


@dp.message(Command("redeem"))
async def cmd_redeem(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await _process_redeem(message, parts[1].strip())
        return
    await state.set_state(ReferralRedeem.waiting_for_code)
    await message.answer("Введи код от друга:")


@dp.message(ReferralRedeem.waiting_for_code)
async def handle_redeem_code(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _process_redeem(message, (message.text or "").strip())


async def _process_redeem(message: Message, code: str) -> None:
    """Анти-абуз для /redeem:
    • код должен существовать (принадлежать реальному пользователю);
    • нельзя погасить свой же код (самоприглашение);
    • нельзя погасить код, если у тебя УЖЕ есть хоть один контакт — значит
      ты реально пользовался ботом раньше, «новым другом» задним числом стать
      нельзя (в отличие от старой ссылочной схемы, здесь /redeem доступен
      только ПОСЛЕ выбора пола, так что users-строка есть у всех — надёжный
      признак «нового» теперь список контактов, а не факт существования в БД);
    • один человек может погасить код только один раз — save_referral_pending
      это PRIMARY KEY(referred_telegram_id), INSERT OR IGNORE."""
    telegram_id = str(message.from_user.id)
    code = code.upper().strip()

    referrer_id = get_referrer_by_code(code) if code else None
    if not referrer_id:
        await message.answer("Код не найден — проверь, что ввёл его без опечаток.")
        return
    if referrer_id == telegram_id:
        await message.answer("Это твой собственный код 🙂")
        return
    if list_contacts(telegram_id):
        await message.answer("Похоже, ты уже пользуешься CueMe — этот код не для тебя.")
        return
    if get_pending_referral(telegram_id):
        await message.answer("Ты уже вводил реферальный код раньше.")
        return

    save_referral_pending(referrer_id, telegram_id)
    await message.answer(
        "Принято! Как только ты начнёшь пользоваться ботом — твой друг получит награду.\n\n"
        "Начни с кем-то новый диалог или подключи Автоматизацию чатов — /connect."
    )


@dp.message(Command("myref"))
async def cmd_myref(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    count = count_successful_referrals(telegram_id)
    lines = ["🎁 Награда за рефералов:\n"]

    if _has_referral_premium(telegram_id):
        until = get_deep_analysis_free_until(telegram_id)
        until_str = until.strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"✅ Premium подписка — активна до {until_str}")
    else:
        lines.append("⏳ Активной награды нет — пригласи друга через /invite")

    lines.append(f"👥 Приведено друзей: {count}")
    await message.answer("\n".join(lines))


def main_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    # b.row(KeyboardButton(text=BTN_SCREENSHOT), KeyboardButton(text=BTN_REPLY))
    # b.row(KeyboardButton(text=BTN_LIVE))
    b.row(KeyboardButton(text=BTN_UNIFIED))
    b.row(KeyboardButton(text=BTN_DEEP), KeyboardButton(text=BTN_DATE))
    b.row(KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_HELP))
    return b.as_markup(resize_keyboard=True)


# more_menu_kb убрана вместе с BTN_MORE — «Идеальное свидание» стало кнопкой
# первого уровня, «Скрипты общения» убраны совсем, «Пригласить друга»
# доступно через «👑 Подписка»/командой /invite. Не удалена физически —
# на случай отката.
# def more_menu_kb() -> InlineKeyboardMarkup:
#     b = InlineKeyboardBuilder()
#     b.button(text=BTN_DATE, callback_data="menu:date")
#     b.button(text=BTN_REVIVE, callback_data="menu:revive")
#     b.button(text=BTN_INVITE, callback_data="menu:invite")
#     b.adjust(1)
#     return b.as_markup()


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


async def _maybe_prompt_gender(bot: Bot, telegram_id: str) -> None:
    """Спрашивает пол один раз, сразу после реального завершения онбординга
    (первый контакт создан — демо/JSON/Business). Идемпотентно — no-op, если
    уже спрашивали/выбрали."""
    if get_gender(telegram_id) is not None:
        return
    try:
        await bot.send_message(int(telegram_id), _GENDER_PROMPT_TEXT, reply_markup=gender_kb())
    except TelegramForbiddenError:
        mark_bot_blocked(telegram_id)
    except Exception:
        logging.warning("gender prompt failed: telegram_id=%s", telegram_id)


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


async def _maybe_prompt_source(bot: Bot, telegram_id: str) -> None:
    """Спрашивает источник один раз, идемпотентно — no-op если уже отвечал."""
    if get_acquisition_source(telegram_id) is not None:
        return
    try:
        await bot.send_message(int(telegram_id), _SOURCE_PROMPT_TEXT, reply_markup=source_kb())
    except TelegramForbiddenError:
        mark_bot_blocked(telegram_id)
    except Exception:
        logging.warning("source prompt failed: telegram_id=%s", telegram_id)


@dp.callback_query(F.data.startswith("src:"))
async def cb_source_select(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    source = call.data.split(":", 1)[1]
    set_acquisition_source(telegram_id, source)
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    # Пол спрашиваем только теперь — после того как юзер реально ответил на
    # вопрос про источник, а не одновременно с ним.
    await _maybe_prompt_gender(bot, telegram_id)


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


# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    waiting_for_json    = State()
    waiting_for_contact = State()

class ReplyHelp(StatesGroup):
    waiting_for_incoming = State()

class Screenshot(StatesGroup):
    waiting_for_image = State()

class LiveDialogue(StatesGroup):
    waiting_for_name     = State()
    waiting_for_incoming = State()

class UnifiedReply(StatesGroup):
    """«💬 Ответ с CueMe» — единая точка входа вместо БТН_SCREENSHOT/
    BTN_REPLY/BTN_LIVE: фото/текст/форвард → определение контакта →
    приводит к одному из существующих пайплайнов (ReplyHelp для
    существующего контакта, LiveDialogue для нового)."""
    waiting_for_input = State()
    waiting_for_name  = State()


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
            # Первый разбор готов — короткое уведомление. Сама карточка сохранена
            # и доступна по кнопке «🔬 Анализ собеседника» (полотно не шлём).
            c = get_contact_by_id(contact_id)
            label = _contact_name(c) if c else "собеседником"
            try:
                await bot.send_message(
                    int(owner_user_id),
                    f"✅ Собралось достаточно сообщений с {label} — готов «🔬 Анализ "
                    "собеседника». Открой его в меню, чтобы посмотреть разбор.",
                )
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


# ── 🔬 Анализ собеседника ─────────────────────────────────────────────────────

DEEP_ANALYSIS_MIN_MSGS = 10  # минимум сообщений с каждой стороны, иначе анализ бессмысленен


_JUNK_WORDS = {"чо", "лол", "кек", "рофл", "ору", "угар", "мда", "эм", "ауф", "хм"}


def _is_junk_message(text: str) -> bool:
    """Мусор для целей ЦИТИРОВАНИЯ в LLM-анализе (build_deep_analysis) — смех/
    междометия, голая пунктуация/эмодзи, куцые реакции без контекста типа
    "сукаааа"/"лол"/"чо". Само сообщение из данных не убирается (см.
    _periodized_dated_lines) — только помечается как непригодное для цитаты,
    чтобы модель не приводила его как иллюстративный «бид»/пример. Короткие, но
    содержательные ответы («да», «нет», «ок») мусором НЕ считаются — это
    реальные прямые ответы (см. правку про «уход от прямого ответа»)."""
    t = text.strip()
    if len(t) <= 1:
        return True
    if not re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", t):
        return True  # только пунктуация/эмодзи, ни одной буквы или цифры
    if " " in t:
        return False  # многословные сообщения не считаем голой репликой
    low = t.lower().strip(" !?.,)(")
    if not low:
        return True
    if low in _JUNK_WORDS:
        return True
    # длинный прогон одного символа с коротким "корнем" — сукаааа, нееееет,
    # дааааа: схлопываем 3+ повторов подряд и смотрим, что осталось
    collapsed = re.sub(r"(.)\1{2,}", r"\1", low)
    if len(collapsed) <= 4 and len(low) >= 6:
        return True
    # смеховое чередование ха/ах/хи/хо и т.п. — считаем по доле покрытия, а не
    # точным совпадением целиком, чтобы ловить и захламлённые вставными буквами
    # варианты вроде "хехахааххааххаппхахахаха"
    if len(low) >= 4:
        hits = len(re.findall(r"ха|ах|хи|их|хе|ех|хо|ох", low))
        if hits * 2 / len(low) >= 0.6:
            return True
    return False


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
            when = r["date"][:16].replace("T", " ")
            tag  = " [шум, не цитировать]" if _is_junk_message(r["text"]) else ""
            lines.append(f"{when} {who}: {r['text']}{tag}")
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


# v1 (совместимость/история по периодам/флаги/подарки, 3 сообщения msg1/msg2/msg3
# с отдельным interaction_card) — оставлено для отката. Заменено 4-блочной
# структурой (совместимость/как писать/длина-ритм-регистр/флаги), которая сама
# закрывает то, что раньше показывал отдельный interaction_card-вызов в msg2.
# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis. None — данных мало."""
#     cached = get_deep_analysis(contact_id)
#     if cached:
#         return cached
#
#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None
#
#     dated_lines = _periodized_dated_lines(rows)
#     stats       = _deep_stats_summary(rows)
#     compat, history, swot, gifts = await build_deep_analysis(
#         dated_lines, stats, user_gender=get_gender(owner_user_id),
#     )
#     save_deep_analysis(contact_id, compat, history, swot, gifts)
#     return {
#         "compatibility_text": compat, "history_text": history,
#         "swot_text": swot, "gifts_text": gifts,
#     }
#
#
# def _format_deep_analysis(name: str, data: dict, interaction_card: str | None) -> tuple[str, str, str]:
#     msg1 = (
#         f"🔬 Анализ собеседника — {name}\n\n"
#         f"💞 Совместимость\n\n{data['compatibility_text']}\n\n"
#         f"📖 История отношений\n\n{data['history_text']}"
#     )
#     msg2 = f"🗣️ Стиль и привычки {name}\n\n{interaction_card}" if interaction_card else ""
#     msg3 = (
#         f"🚩💚 Флаги\n\n{data['swot_text']}\n\n"
#         f"🎁 Рекомендации подарков\n\n{data['gifts_text']}"
#     )
#     return msg1, msg2, msg3


# v-5axis (единый текст «Название: N/5», медаль 0-25, парсинг текста обратно
# для Rich Message) — оставлено для отката. Заменено детерминированными
# метриками (compatibility_metrics.py) + LLM только для интерпретации уже
# посчитанных фактов (build_compatibility_interpretation в llm.py) — не нужен
# весь текст переписки в промпте, только 7 готовых фактов, поэтому быстрее и
# не рискует таймаутом на больших контактах. См. новые версии ниже.
# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis. None — данных мало."""
#     cached = get_deep_analysis(contact_id)
#     if cached:
#         return cached

#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None

#     dated_lines = _periodized_dated_lines(rows)
#     stats       = _deep_stats_summary(rows)
#     compat = await build_deep_analysis(
#         dated_lines, stats, rows, user_gender=get_gender(owner_user_id),
#     )
#     save_deep_analysis(contact_id, compat)
#     return {"compatibility_text": compat}


# def _format_deep_analysis(name: str, data: dict) -> str:
#     """Единый блок — 5 осей с обоснованием вместо прежних 4 разрозненных
#     блоков (совместимость/как писать/флаги/готовое сообщение — «как писать» и
#     «флаги» пересказывали то же самое, что теперь показывают оси; «готовое
#     сообщение» дублировало отдельную функцию «Ответить за меня», убрано без
#     замены). Разбивку на несколько сообщений при превышении лимита Telegram
#     делает _answer_long — она режет по границам абзацев, тут не нужно.
#     Это plain-text ФОЛБЭК для _run_deep_analysis — основной путь теперь Rich
#     Message (см. _parse_compat_text/_build_rich_analysis_html), этот формат
#     остаётся на случай, если Rich Message не отправился."""
#     return f"🔬 Анализ собеседника — {name}\n\n{data['compatibility_text']}"


# def _parse_compat_text(compatibility_text: str) -> tuple[str, list[tuple[str, int, str]], str] | None:
#     """Разбирает уже готовый текст build_deep_analysis (медаль+сумма, 5 осей
#     «Название: N/5» + обоснование, финальная строка 👉 совет) обратно на
#     структурные куски для Rich Message. build_deep_analysis НЕ меняется —
#     это чисто раскладка уже сгенерированного текста, не новая генерация.
#     None, если структура неожиданная (не 5 осей/нет совета) — сигнал сразу
#     уйти в текстовый фолбэк, не пытаясь звать Rich Message API вслепую."""
#     lines = compatibility_text.splitlines()
#     if not lines:
#         return None
#     medal_line = lines[0].strip()
#     axes: list[tuple[str, int, str]] = []
#     advice = ""
#     i = 1
#     n = len(lines)
#     while i < n:
#         line = lines[i].strip()
#         if not line:
#             i += 1
#             continue
#         m = _AXIS_HEADER_RE.match(line)
#         if m:
#             axis_name, score = m.group(1), int(m.group(2))
#             i += 1
#             body_lines = []
#             while i < n and lines[i].strip() and not _AXIS_HEADER_RE.match(lines[i].strip()):
#                 body_lines.append(lines[i].strip())
#                 i += 1
#             axes.append((axis_name, score, " ".join(body_lines)))
#             continue
#         if line.startswith("👉"):
#             advice = line.lstrip("👉").strip()
#             i += 1
#             continue
#         i += 1  # неожиданная строка — пропускаем, не валим весь парсинг

#     if len(axes) != 5 or not advice:
#         return None
#     return medal_line, axes, advice


# def _short_phrase(text: str, max_len: int = 55) -> str:
#     """Первое предложение уже готового текста оси, обрезанное по длине —
#     компактная «суть» для ячейки таблицы. Без LLM: не просим модель отдельно
#     генерировать короткую версию (нет возможности прогнать живой тест на
#     промпт прямо сейчас), просто урезаем то, что она уже написала."""
#     text = text.strip()
#     if not text:
#         return "—"
#     m = re.match(r"(.+?[.!?])(?:\s|$)", text)
#     first = m.group(1) if m else text
#     if len(first) > max_len:
#         first = first[: max_len - 1].rstrip() + "…"
#     return first


# def _build_rich_analysis_html(
#     name: str, medal_line: str, axes: list[tuple[str, int, str]], advice: str,
# ) -> str:
#     """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
#     таблица с 5 баллами сразу видна, полное обоснование — в <details> без
#     open (свёрнуто по умолчанию), совет — <mark> акцентом."""
#     esc = html.escape
#     rows = "\n".join(
#         f'<tr><td align="left">{esc(axis_name)}</td>'
#         f'<td align="center">{score}/5</td>'
#         f'<td align="left">{esc(_short_phrase(body))}</td></tr>'
#         for axis_name, score, body in axes
#     )
#     detail_paras = "\n".join(
#         f"<p><b>{esc(axis_name)}</b> — {score}/5. {esc(body)}</p>"
#         for axis_name, score, body in axes
#     )
#     return (
#         f"<h2>🔬 Анализ собеседника — {esc(name)}</h2>\n"
#         f"<h3>{esc(medal_line)}</h3>\n"
#         "<table>\n"
#         '<tr><th align="left">Показатель</th><th align="center">Балл</th>'
#         '<th align="left">Суть</th></tr>\n'
#         f"{rows}\n"
#         "</table>\n"
#         "<details>\n"
#         "<summary>Показать обоснование</summary>\n"
#         f"{detail_paras}\n"
#         "</details>\n"
#         f"<p><mark>👉 {esc(advice)}</mark></p>"
#     )


# def deep_analysis_result_kb(contact_id: int) -> InlineKeyboardMarkup:
#     b = InlineKeyboardBuilder()
#     b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
#     return b.as_markup()


# async def _run_deep_analysis(
#     bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
# ) -> None:
#     # Реферальная награда теперь даёт полный Premium (учтено внутри _is_premium,
#     # которую вызывает _require_premium) — отдельной проверки тут больше не нужно.
#     if not await _require_premium(bot, target, telegram_id):
#         return
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         text = "Контакт не найден."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#     name = _contact_name(contact)

#     wait_text = f"Готовлю анализ собеседника — {name}. Это займёт ~30 секунд..."
#     await (target.edit_text(wait_text) if edit else target.answer(wait_text))

#     try:
#         data = await _gen_deep_analysis(contact_id, telegram_id)
#     except RateLimitError:
#         await target.answer("Лимит LLM исчерпан, попробуй позже.")
#         return
#     except Exception:
#         logging.exception("deep_analysis: ошибка генерации")
#         await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
#         return

#     if not data:
#         await target.answer(
#             f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
#             f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
#             "накопление через Автоматизацию чатов)."
#         )
#         return

#     # Rich Message (таблица + сворачиваемое обоснование) — основной путь;
#     # ЛЮБОЙ сбой (парсинг текста, отказ Bot API, нет капабилити у клиента и
#     # т.п.) откатывается на обычный текст, чтобы пользователь в любом случае
#     # получил результат — это платная core-фича, тишины быть не должно.
#     parsed = _parse_compat_text(data["compatibility_text"])
#     sent_rich = False
#     if parsed is not None:
#         medal_line, axes, advice = parsed
#         try:
#             rich_html = _build_rich_analysis_html(name, medal_line, axes, advice)
#             await bot.send_rich_message(
#                 chat_id=target.chat.id,
#                 rich_message=InputRichMessage(html=rich_html),
#                 reply_markup=deep_analysis_result_kb(contact_id),
#             )
#             sent_rich = True
#         except Exception:
#             logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

#     if not sent_rich:
#         await _answer_long(
#             target, _format_deep_analysis(name, data), reply_markup=deep_analysis_result_kb(contact_id),
#         )


# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis, инвалидация по REBUILD_THRESHOLD
#     (тот же паттерн, что my_style_per_contact) — не пересчитываем на каждый
#     запрос. None — данных мало. Метрики (compatibility_metrics.py) считаются
#     по ВСЕЙ истории контакта без семплирования — это дёшево (текст+дата+
#     направление, без LLM), в отличие от старой системы, которой нужна была
#     урезанная выборка под лимит промпта."""
#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None

#     total_count = count_biz_messages_for_contact(owner_user_id, contact_id) + count_imported_messages(contact_id)
#     cached = get_deep_analysis(contact_id)
#     if cached and total_count - cached["last_rebuild_count"] < REBUILD_THRESHOLD:
#         return cached

#     metrics = compute_compat_metrics(rows)
#     interpretations, advice = await build_compatibility_interpretation(
#         metrics, user_gender=get_gender(owner_user_id),
#     )
#     for key, text in interpretations.items():
#         metrics[key]["interpretation"] = text

#     metrics_json = json.dumps(metrics, ensure_ascii=False)
#     save_deep_analysis(contact_id, metrics_json, advice, total_count)
#     return {"metrics_json": metrics_json, "advice_text": advice, "last_rebuild_count": total_count}


# def _short_words(text: str, max_words: int = 5) -> str:
#     """3-5 слов сути для ячейки таблицы — из уже готовой интерпретации/факта,
#     без отдельного LLM-вызова на короткую версию."""
#     text = (text or "").strip()
#     if not text:
#         return "—"
#     words = text.split()
#     short = " ".join(words[:max_words]).rstrip(".,;:")
#     if len(words) > max_words:
#         short += "…"
#     return short


# def _build_rich_analysis_html(name: str, metrics: dict, advice: str) -> str:
#     """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
#     таблица с 6 метриками сразу видна, полные интерпретации — в <details> без
#     open (свёрнуто по умолчанию), совет — <mark> акцентом. metrics — dict в
#     порядке compatibility_metrics.METRICS, каждое значение {"label","short",
#     "fact","interpretation"}."""
#     esc = html.escape
#     rows = "\n".join(
#         f'<tr><td align="left">{esc(m["label"])}</td>'
#         f'<td align="center">{esc(m["short"])}</td>'
#         f'<td align="left">{esc(_short_words(m.get("interpretation") or m["fact"]))}</td></tr>'
#         for m in metrics.values()
#     )
#     detail_paras = "\n".join(
#         f"<p><b>{esc(m['label'])}</b>: {esc(m.get('interpretation') or m['fact'])}</p>"
#         for m in metrics.values()
#     )
#     return (
#         f"<h2>🔬 Анализ собеседника — {esc(name)}</h2>\n"
#         "<table>\n"
#         '<tr><th align="left">Метрика</th><th align="center">Значение</th>'
#         '<th align="left">Суть</th></tr>\n'
#         f"{rows}\n"
#         "</table>\n"
#         "<details>\n"
#         "<summary>Показать подробности</summary>\n"
#         f"{detail_paras}\n"
#         "</details>\n"
#         f"<p><mark>👉 {esc(advice)}</mark></p>"
#     )


# def _format_deep_analysis_text(name: str, metrics: dict, advice: str) -> str:
#     """Plain-text ФОЛБЭК для _run_deep_analysis, если Rich Message не
#     отправился — таблица моноширинным блоком, подробности обычным текстом,
#     без сворачивания (в чистом тексте сворачивать нечем)."""
#     header = f"🔬 Анализ собеседника — {html.escape(name)}\n\n"
#     table_lines = [f"{m['label']}: {m['short']}" for m in metrics.values()]
#     table = "<pre>" + html.escape("\n".join(table_lines)) + "</pre>"
#     details = "\n\n".join(
#         f"<b>{html.escape(m['label'])}</b>: {html.escape(m.get('interpretation') or m['fact'])}"
#         for m in metrics.values()
#     )
#     return f"{header}{table}\n\n{details}\n\n👉 {html.escape(advice)}"


def _volume_trend_to_dict(vt) -> dict:
    """VolumeTrend (compatibility_metrics.py) → JSON-совместимый dict —
    dataclass с dataclass-полями напрямую не сериализуется в json.dumps."""
    def _period(p):
        return None if p is None else {"label": p.label, "n_author": p.n_author, "n_contact": p.n_contact}
    return {
        "granularity": vt.granularity,
        "periods": [_period(p) for p in vt.periods],
        "peak": _period(vt.peak),
        "latest": _period(vt.latest),
    }


async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
    """Ленивая генерация с кэшем в deep_analysis, инвалидация по REBUILD_THRESHOLD
    (тот же паттерн, что my_style_per_contact) — не пересчитываем на каждый
    запрос. None — данных мало. Метрики (compatibility_metrics.py) считаются
    по ВСЕЙ истории контакта без семплирования — дёшево (текст+дата+
    направление, без LLM), в отличие от старой 5-осевой системы, которой
    нужна была урезанная выборка под лимит промпта."""
    rows = get_all_dated_messages(owner_user_id, contact_id)
    my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
    ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
    if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
        return None

    total_count = count_biz_messages_for_contact(owner_user_id, contact_id) + count_imported_messages(contact_id)
    cached = get_deep_analysis(contact_id)
    if cached and total_count - cached["last_rebuild_count"] < REBUILD_THRESHOLD:
        return cached

    # Разведочный LLM-проход для секции «Тепло» (проверка неоднозначных похвал)
    # убран вместе с самой секцией — все метрики карточки снова считаются
    # детерминированно, один вызов compute_all без предварительных обращений
    # к LLM (сам разбор метрик ниже — build_compatibility_interpretation — на
    # месте, он интерпретирует уже посчитанные числа).
    metrics = compute_compat_metrics(rows)
    volume_trend = metrics.pop("_volume_trend")
    interpretations, dynamics_text, synthesis, advice = await build_compatibility_interpretation(
        metrics, volume_trend, user_gender=get_gender(owner_user_id),
    )
    for key, text in interpretations.items():
        metrics[key]["interpretation"] = text

    texted = [r for r in rows if r.get("text") and r.get("date")]
    dates = sorted(r["date"] for r in texted)
    metrics["_meta"] = {
        "total": len(texted),
        "date_from": dates[0][:10] if dates else "",
        "date_to": dates[-1][:10] if dates else "",
    }
    metrics["_volume_trend"] = _volume_trend_to_dict(volume_trend)

    metrics_json = json.dumps(metrics, ensure_ascii=False)
    save_deep_analysis(contact_id, metrics_json, dynamics_text, synthesis, advice, total_count)
    return {
        "metrics_json": metrics_json, "dynamics_text": dynamics_text,
        "synthesis_text": synthesis, "advice_text": advice, "last_rebuild_count": total_count,
    }


def deep_analysis_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
    return b.as_markup()


# async def _run_deep_analysis(
#     bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
# ) -> None:
#     # Реферальная награда теперь даёт полный Premium (учтено внутри _is_premium,
#     # которую вызывает _require_premium) — отдельной проверки тут больше не нужно.
#     if not await _require_premium(bot, target, telegram_id):
#         return
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         text = "Контакт не найден."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#     name = _contact_name(contact)

#     wait_text = f"Готовлю анализ собеседника — {name}. Это займёт ~20 секунд..."
#     await (target.edit_text(wait_text) if edit else target.answer(wait_text))

#     try:
#         data = await _gen_deep_analysis(contact_id, telegram_id)
#     except RateLimitError:
#         await target.answer("Лимит LLM исчерпан, попробуй позже.")
#         return
#     except Exception:
#         logging.exception("deep_analysis: ошибка генерации")
#         await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
#         return

#     if not data:
#         await target.answer(
#             f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
#             f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
#             "накопление через Автоматизацию чатов)."
#         )
#         return

#     metrics = json.loads(data["metrics_json"])
#     advice = data["advice_text"]

#     # Rich Message (таблица + сворачиваемые подробности) — основной путь;
#     # ЛЮБОЙ сбой (отказ Bot API, нет капабилити у клиента и т.п.) откатывается
#     # на обычный текст, чтобы пользователь в любом случае получил результат —
#     # это платная core-фича, тишины быть не должно.
#     sent_rich = False
#     try:
#         rich_html = _build_rich_analysis_html(name, metrics, advice)
#         await bot.send_rich_message(
#             chat_id=target.chat.id,
#             rich_message=InputRichMessage(html=rich_html),
#             reply_markup=deep_analysis_result_kb(contact_id),
#         )
#         sent_rich = True
#     except Exception:
#         logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

#     if not sent_rich:
#         await _answer_long(
#             target, _format_deep_analysis_text(name, metrics, advice),
#             reply_markup=deep_analysis_result_kb(contact_id), parse_mode="HTML",
#         )


_METRIC_EMOJI = {
    "balance": "⚖️",
    "response_speed": "⏱️",
    "initiation": "🙋",
    "long_pauses": "⏸️",
    "questions": "❓",
    "circadian": "🕒",
}

def _direction_label(direction: str) -> str:
    return "ты" if direction == "out" else "собеседник"


def _quote_examples_suffix(m: dict) -> str:
    """Доп. строка с реальными цитатами для любой метрики, у которой они есть
    (поле "examples" — сейчас его отдаёт «Кто чаще задаёт вопросы», см.
    compatibility_metrics.question_balance). У метрик без цитат поля нет и
    суффикс пустой, поэтому вызывать можно на всех подряд, без частных
    условий по ключу. Цитаты обрезаются до 80 символов — тот же паттерн, что
    у initiative_axis в features.py. Добавляется ПОСЛЕ interpretation/fact, а
    не встраивается в промпт интерпретации: LLM переписывает только смысл
    факта и не видит сырые цитаты, значит не может их исказить/потерять."""
    examples = m.get("examples") or []
    if not examples:
        return ""

    def _quote(text: str) -> str:
        return text if len(text) <= 80 else text[:77].rstrip() + "…"

    parts = [
        f"«{_quote(text)}» ({_direction_label(direction)})"
        for direction, text in examples
    ]
    return " Например, " + "; ".join(parts) + "."


_MAX_TREND_ROWS = 12  # длинная история (месяцы) может дать 30+ периодов — не годится для одной таблицы в сообщении


def _trend_table_rows(vt: dict) -> list[dict]:
    """Последние _MAX_TREND_ROWS периодов для таблицы — полная история всё
    равно участвует в подсчёте peak/latest (см. compatibility_metrics.py),
    здесь только отображение."""
    periods = vt.get("periods") or []
    return periods[-_MAX_TREND_ROWS:]


def _rich_heading(text: str) -> str:
    """Жирный заголовок внутри Rich Message: <p><b>...</b></p>, НЕ
    <h2>/<h3> (heading-блок, RichBlockSectionHeading в Bot API 10.1+).
    Heading-блок — отдельный тип блока со своим "size" (1-6), и каждый
    Telegram-клиент вправе рисовать его собственной типографикой — на
    iPhone это оказался крупный засечный шрифт, на десктопе/вебе — обычный
    текст. Bold-текст ВНУТРИ параграфа (RichBlockParagraph) такому не
    подвержен — это тот же механизм, что уже даёт одинаковый на всех
    платформах blockquote (тоже просто paragraph-контент, не отдельный
    блок-тип). Единая точка форматирования жирных заголовков во всей
    карточке — и для общего заголовка, и для заголовков метрик/секций, не
    два разных места."""
    return f"<p><b>{html.escape(text)}</b></p>"


def _build_rich_analysis_html(name: str, metrics: dict, dynamics_text: str, synthesis: str, advice: str) -> str:
    """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
    жирный заголовок (см. _rich_heading — НЕ <h2>/<h3>) с эмодзи по смыслу
    метрики + <blockquote> под ним на каждую метрику, отдельная секция
    «Динамика переписки» с настоящей таблицей периодов, «Вывод» — синтез,
    «Что дальше» — совет обычным текстом, без выделения."""
    esc = html.escape
    meta = metrics.get("_meta") or {}
    vt = metrics.get("_volume_trend") or {}

    subtitle = ""
    if meta.get("total"):
        subtitle = (
            f"<p><i>{meta['total']} сообщений, {esc(meta.get('date_from', ''))} — "
            f"{esc(meta.get('date_to', ''))}</i></p>\n"
        )

    metric_block_parts = []
    for key, m in metrics.items():
        if key.startswith("_"):
            continue
        text = m.get("interpretation") or m["fact"]
        text += _quote_examples_suffix(m)  # пусто у метрик без цитат
        heading = f"{_METRIC_EMOJI.get(key, '')} {m['label']}".strip()
        metric_block_parts.append(
            f"{_rich_heading(heading)}\n<blockquote>{esc(text)}</blockquote>"
        )
    metric_blocks = "\n".join(metric_block_parts)

    trend_rows = _trend_table_rows(vt)
    if trend_rows:
        table_lines = "\n".join(
            f'<tr><td align="left">{esc(p["label"])}</td>'
            f'<td align="center">{p["n_author"]}</td>'
            f'<td align="center">{p["n_contact"]}</td>'
            f'<td align="center">{p["n_author"] + p["n_contact"]}</td></tr>'
            for p in trend_rows
        )
        trend_table = (
            "<table>\n"
            '<tr><th align="left">Период</th><th align="center">Ты</th>'
            '<th align="center">Собеседник</th><th align="center">Всего</th></tr>\n'
            f"{table_lines}\n"
            "</table>\n"
        )
    else:
        trend_table = ""

    return (
        f"{_rich_heading(f'🔬 Анализ собеседника — {name}')}\n"
        f"{subtitle}"
        f"{metric_blocks}\n"
        f"{_rich_heading('📈 Динамика переписки')}\n"
        f"{trend_table}"
        f"<blockquote>{esc(dynamics_text)}</blockquote>\n"
        f"{_rich_heading('🧩 Вывод')}\n"
        f"<blockquote>{esc(synthesis)}</blockquote>\n"
        f"{_rich_heading('👉 Что дальше')}\n"
        f"<p>{esc(advice)}</p>"
    )


def _format_deep_analysis_text(name: str, metrics: dict, dynamics_text: str, synthesis: str, advice: str) -> str:
    """Plain-text ФОЛБЭК для _run_deep_analysis, если Rich Message не
    отправился — та же структура, эмодзи-заголовки вместо heading, таблица
    периодов моноширинным блоком. HTML-спецсимволы в значениях (например
    «<1 мин») экранируются ДО оборачивания в <pre>/<b> — иначе parse_mode=
    HTML сочтёт их невалидными тегами и упадёт сам фолбэк."""
    meta = metrics.get("_meta") or {}
    vt = metrics.get("_volume_trend") or {}

    header = f"🔬 Анализ собеседника — {html.escape(name)}\n"
    if meta.get("total"):
        header += f"<i>{meta['total']} сообщений, {html.escape(meta.get('date_from', ''))} — {html.escape(meta.get('date_to', ''))}</i>\n"
    header += "\n"

    metric_part_list = []
    for key, m in metrics.items():
        if key.startswith("_"):
            continue
        text = m.get("interpretation") or m["fact"]
        text += _quote_examples_suffix(m)  # пусто у метрик без цитат
        metric_part_list.append(
            f"<b>{_METRIC_EMOJI.get(key, '')} {html.escape(m['label'])}</b>\n{html.escape(text)}"
        )
    metric_parts = "\n\n".join(metric_part_list)

    trend_rows = _trend_table_rows(vt)
    if trend_rows:
        lines = [f"{p['label']}: ты {p['n_author']}, собеседник {p['n_contact']}, всего {p['n_author'] + p['n_contact']}" for p in trend_rows]
        trend_table = "<pre>" + html.escape("\n".join(lines)) + "</pre>\n"
    else:
        trend_table = ""

    return (
        f"{header}{metric_parts}\n\n"
        f"📈 <b>Динамика переписки</b>\n{trend_table}{html.escape(dynamics_text)}\n\n"
        f"🧩 <b>Вывод</b>\n{html.escape(synthesis)}\n\n"
        f"👉 <b>Что дальше</b>\n{html.escape(advice)}"
    )


async def _run_deep_analysis(
    bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
) -> None:
    # Реферальная награда теперь даёт полный Premium (учтено внутри _is_premium,
    # которую вызывает _require_premium) — отдельной проверки тут больше не нужно.
    if not await _require_premium(bot, target, telegram_id):
        return
    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    name = _contact_name(contact)

    wait_text = f"Готовлю анализ собеседника — {name}. Это займёт ~20 секунд..."
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
            f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
            f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
            "накопление через Автоматизацию чатов)."
        )
        return

    metrics = json.loads(data["metrics_json"])
    dynamics_text = data["dynamics_text"]
    synthesis = data["synthesis_text"]
    advice = data["advice_text"]

    # Rich Message (заголовки + цитаты на метрику + таблица динамики) —
    # основной путь; ЛЮБОЙ сбой (отказ Bot API, нет капабилити у клиента и
    # т.п.) откатывается на обычный текст, чтобы пользователь в любом случае
    # получил результат — это платная core-фича, тишины быть не должно.
    sent_rich = False
    try:
        rich_html = _build_rich_analysis_html(name, metrics, dynamics_text, synthesis, advice)
        await bot.send_rich_message(
            chat_id=target.chat.id,
            rich_message=InputRichMessage(html=rich_html),
            reply_markup=deep_analysis_result_kb(contact_id),
        )
        sent_rich = True
    except Exception:
        logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

    if not sent_rich:
        await _answer_long(
            target, _format_deep_analysis_text(name, metrics, dynamics_text, synthesis, advice),
            reply_markup=deep_analysis_result_kb(contact_id), parse_mode="HTML",
        )


async def _show_deep_analysis(message: Message, bot: Bot, telegram_id: str | None = None) -> None:
    # telegram_id передаётся явно из cb_submenu (call.from_user), т.к. message
    # там — это сообщение БОТА с инлайн-клавиатурой, а не сообщение юзера, и
    # message.from_user в этом случае был бы ботом, а не человеком.
    telegram_id = telegram_id or str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await _send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        await _run_deep_analysis(bot, message, telegram_id, contacts[0]["id"])
        return

    await message.answer("Для кого сделать анализ собеседника?", reply_markup=contacts_kb(contacts, "deepan"))


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


# ── 💐 Идеальное свидание ─────────────────────────────────────────────────────

IDEAL_DATE_MIN_MSGS = 5  # минимум сообщений собеседника, иначе не за что зацепиться


def _spread_sample(rows: list[dict], direction: str, target: int, offset: float = 0.0) -> list[str]:
    """Равномерная выборка target сообщений заданного направления по ВСЕЙ истории
    (не только последние N) — как периодизация в _periodized_dated_lines, но
    плоским списком текстов. Так упоминания интересов из любого периода переписки
    попадают в промпт, а не только из свежих сообщений.
    offset ∈ [0,1) сдвигает точку внутри каждого временного окна — при offset=0
    выборка детерминированная, со случайным offset «Другая идея» видит ДРУГИЕ
    сообщения (тот же равномерный охват, другие представители)."""
    msgs = [
        r["text"] for r in sorted(
            (r for r in rows if r["direction"] == direction and r["text"] and r["text"].strip()),
            key=lambda r: r["date"],
        )
    ]
    if len(msgs) <= target:
        return msgs
    step = len(msgs) / target
    last = len(msgs) - 1
    return [msgs[min(last, int(i * step + offset * step))] for i in range(target)]


def _ideal_date_samples(contact_id: int, owner_user_id: str, offset: float = 0.0) -> dict | None:
    """Семплы для build_ideal_date по ВСЕЙ истории переписки (business + JSON,
    через get_all_dated_messages) с равномерным охватом всех периодов — как в
    «Анализе собеседника», а не только последние сообщения. offset сдвигает
    выборку («Другая идея» → другие сообщения). None — сообщений собеседника
    слишком мало для осмысленной идеи."""
    rows = get_all_dated_messages(owner_user_id, contact_id)
    contact_msgs = _spread_sample(rows, "in", 100, offset)
    my_msgs      = _spread_sample(rows, "out", 40, offset)
    if len(contact_msgs) < IDEAL_DATE_MIN_MSGS:
        return None
    stats = _deep_stats_summary(rows)
    return {"contact_sample": contact_msgs, "my_sample": my_msgs, "features_summary": stats}


async def _gen_ideal_date(contact_id: int, owner_user_id: str, fresh: bool = False) -> dict | None:
    """Ленивая генерация с кэшем в ideal_date. None — данных мало.
    fresh=True («Другая идея») — не читает кэш и берёт СЛУЧАЙНО сдвинутую
    выборку, чтобы модель увидела другие сообщения и дала заметно другую идею."""
    if not fresh:
        cached = get_ideal_date(contact_id)
        if cached:
            return cached

    offset = random.random() if fresh else 0.0
    samples = _ideal_date_samples(contact_id, owner_user_id, offset)
    if not samples:
        return None

    interaction_card = await _gen_interaction_card(contact_id, owner_user_id) or ""
    date_idea, gift_ideas = await build_ideal_date(
        samples["contact_sample"], samples["my_sample"],
        interaction_card, samples["features_summary"],
    )
    save_ideal_date(contact_id, date_idea, gift_ideas)
    return {"date_idea": date_idea, "gift_ideas": gift_ideas}


def _format_ideal_date(name: str, data: dict) -> str:
    """Оба блока (идея свидания + подарки) — одним сообщением."""
    return (
        f"💐 Идеальное свидание — {name}\n\n"
        f"{data['date_idea'].strip()}\n\n"
        f"{data['gift_ideas'].strip()}"
    )


def ideal_date_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другая идея", callback_data=f"idealdate_refresh:{contact_id}")
    return b.as_markup()


async def _run_ideal_date(
    bot: Bot, target: Message, telegram_id: str, contact_id: int,
    edit: bool = False, fresh: bool = False,
) -> None:
    if not await _require_premium(bot, target, telegram_id):
        return
    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    name = _contact_name(contact)

    wait_text = f"Придумываю идеальное свидание с {name}. Это займёт ~20 секунд..."
    await (target.edit_text(wait_text) if edit else target.answer(wait_text))

    try:
        data = await _gen_ideal_date(contact_id, telegram_id, fresh=fresh)
    except RateLimitError:
        await target.answer("Лимит LLM исчерпан, попробуй позже.")
        return
    except Exception:
        logging.exception("ideal_date: ошибка генерации")
        await target.answer("Не удалось придумать идею — попробуй ещё раз.")
        return

    if not data:
        await target.answer(
            f"Пока маловато сообщений от {name}, чтобы зацепиться за что-то "
            f"конкретное — нужно хотя бы {IDEAL_DATE_MIN_MSGS} его сообщений "
            "(JSON-экспорт или накопление через Автоматизацию чатов)."
        )
        return

    await _answer_long(target, _format_ideal_date(name, data), reply_markup=ideal_date_result_kb(contact_id))


async def _show_ideal_date(message: Message, bot: Bot, telegram_id: str | None = None) -> None:
    telegram_id = telegram_id or str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await _send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        await _run_ideal_date(bot, message, telegram_id, contacts[0]["id"])
        return

    await message.answer("С кем свидание?", reply_markup=contacts_kb(contacts, "idealdate"))


@dp.callback_query(F.data.startswith("idealdate_refresh:"))
async def cb_ideal_date_refresh(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer("Придумываю другую идею...")
    delete_ideal_date(contact_id)
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, fresh=True)


@dp.callback_query(F.data.startswith("idealdate:"))
async def cb_ideal_date_contact(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, edit=True)


# ── Business API ──────────────────────────────────────────────────────────────

@dp.business_connection()
async def handle_business_connection(event: BusinessConnection, bot: Bot) -> None:
    upsert_business_connection(
        connection_id=event.id,
        owner_user_id=str(event.user.id),
        can_reply=event.can_reply,
        is_enabled=event.is_enabled,
    )
    status = "подключён" if event.is_enabled else "отключён"
    logging.info("business_connection %s: owner=%s %s", event.id, event.user.id, status)
    if event.is_enabled:
        owner_id = str(event.user.id)
        # Строка в users нужна СРАЗУ, а не только когда юзер ответит на
        # источник/пол (set_acquisition_source/set_gender делают upsert) —
        # иначе если он заблокирует бота раньше, mark_bot_blocked() будет
        # UPDATE по несуществующей строке (молчаливый no-op), а сам юзер
        # до ответа останется невидим в /users.
        upsert_user(owner_id, f"user{owner_id}")
        try:
            await bot.send_message(
                event.user.id,
                "✅ Готово, бот подключён! CueMe готов помогать тебе в переписках )",
            )
        except TelegramForbiddenError:
            mark_bot_blocked(owner_id)
            return
        except Exception:
            logging.warning("business-connect notify failed: owner=%s", event.user.id)
        await asyncio.sleep(3)
        # Пол спрашиваем не сразу, а из cb_source_select — ПОСЛЕ того как юзер
        # реально ответит на вопрос про источник (последовательно, не хором).
        await _maybe_prompt_source(bot, owner_id)


# ── Использование подсказок CueMe в реальной переписке ────────────────────────
# Порог совпадения (SequenceMatcher.ratio, 0-1) между текстом реального
# исходящего сообщения и одной из подсказок, которые бот показывал за
# последние 24ч этому контакту: >=0.85 — «как есть», 0.5-0.85 — «с правками»,
# ниже — не считается использованием (случайное совпадение отдельных слов).
_SUGGESTION_EXACT_RATIO = 0.85
_SUGGESTION_EDITED_RATIO = 0.5
_SUGGESTION_MATCH_WINDOW = timedelta(hours=24)
_EDGE_PUNCT = string.punctuation + "«»—–…\"'"


def _normalize_for_match(text: str) -> str:
    """Нижний регистр, схлопнутые пробелы, пунктуация обрезана ТОЛЬКО по
    краям (не внутри — иначе «без изменений» и правки было бы не отличить)."""
    t = re.sub(r"\s+", " ", text.lower()).strip()
    return t.strip(_EDGE_PUNCT).strip()


def _match_outgoing_to_suggestion(
    owner_id: str, contact_id: int | None, text: str | None, business_message_id: int,
) -> None:
    """Сравнивает реальное исходящее сообщение с недавними (<=24ч) ещё не
    засчитанными подсказками этого контакта — если находит совпадение выше
    порога, помечает ЛУЧШУЮ (по ratio) подсказку использованной. Чисто
    CPU-сравнение (difflib), без сети/LLM — безопасно звать синхронно внутри
    _persist_business_message (уже выполняется в asyncio.to_thread)."""
    if not text or not contact_id:
        return
    since = (datetime.now(timezone.utc) - _SUGGESTION_MATCH_WINDOW).isoformat()
    candidates = get_recent_unmatched_suggestions(owner_id, contact_id, since)
    if not candidates:
        return

    norm_out = _normalize_for_match(text)
    best_row, best_ratio = None, 0.0
    for row in candidates:
        ratio = difflib.SequenceMatcher(
            None, norm_out, _normalize_for_match(row["suggestion_text"]),
        ).ratio()
        if ratio > best_ratio:
            best_row, best_ratio = row, ratio

    if best_row is None or best_ratio < _SUGGESTION_EDITED_RATIO:
        return
    match_kind = "exact" if best_ratio >= _SUGGESTION_EXACT_RATIO else "edited"
    mark_suggestion_matched(best_row["id"], business_message_id, best_ratio, match_kind)


def _persist_business_message(
    *, conn_id: str, owner_id: str, chat_ref: str, direction: str,
    text: str | None, is_voice: bool, date: str, tg_message_id: int,
    contact_tg_id: str, chat_first_name: str | None, chat_last_name: str | None,
    chat_username: str | None, sender_username: str | None,
) -> int | None:
    """Синхронная DB-часть обработки business-сообщения: сохранение + резолв контакта
    + троттлинг refresh + сопоставление с подсказками CueMe (для исходящих).
    Возвращает contact_id для пересборки (или None). Выполняется в
    asyncio.to_thread, чтобы не блокировать event loop на живом потоке."""
    message_id = save_business_message(
        connection_id=conn_id, owner_user_id=owner_id, chat_ref=chat_ref,
        direction=direction, text=text, date=date, tg_message_id=tg_message_id,
        raw_meta=_msg_meta(text, is_voice),
    )
    if message_id is None:
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

    if direction == "out":
        _match_outgoing_to_suggestion(owner_id, cid, text, message_id)

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
        # Друг подключил Business и пошёл живой поток — засчитываем реферала
        # (идемпотентно: после первого зачёта get_pending_referral вернёт None)
        # и спрашиваем пол (тоже идемпотентно — no-op, если уже выбран).
        await _credit_referral_if_pending(bot, owner_id)
        await _maybe_prompt_gender(bot, owner_id)
        asyncio.create_task(_maybe_rebuild(owner_id, contact_id_for_rebuild, bot))


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
        "перешли туда любое сообщение или скриншот переписки, и я заведу "
        "первый контакт.",
        reply_markup=_no_contacts_kb(),
    )


async def _send_no_dialogs_hint(message: Message) -> None:
    """Показывается, когда у юзера ещё нет ни одного контакта (диалога) —
    сразу конкретный вопрос с двумя вариантами следующего шага (без
    свободного выбора среди всех кнопок главного меню — на живых тестерах
    общий текст с полной клавиатурой не работал, терялись)."""
    # main_kb (reply-клавиатура) — единственное место, где она доходит до
    # юзера на чистом Business-пути (до первого сообщения ещё не было
    # повода её прислать). Шлём ПЕРВЫМ сообщением (до inline-вопроса ниже) —
    # два разных типа клавиатур (reply + inline) подряд в некоторых клиентах
    # ведут себя надёжнее в этом порядке. Раньше здесь стоял zero-width
    # space (U+200B) — не исключено, что Telegram Bot API его не всегда
    # принимает как валидный text; "·" — заведомо обычный непустой символ.
    # Обёрнуто в try/except с логированием — раньше сбой (если он был) тут
    # проходил тихо, и без journalctl-следа причину было не установить.
    try:
        await message.answer("·", reply_markup=main_kb())
    except Exception:
        logging.exception("_send_no_dialogs_hint: не удалось отправить main_kb")

    await message.answer(
        "Готово, бот подключён! Есть кто-то конкретный, с кем сейчас переписываешься?",
        reply_markup=_quickstart_kb(),
    )


@dp.callback_query(F.data == "qs:yes")
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


@dp.callback_query(F.data == "qs:no")
async def cb_quickstart_no(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer("Кому бы написал(-а)?", reply_markup=_quickstart_gender_kb())


def _quickstart_phrase_next_kb(target: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data=f"qsphr_next:{target}")
    b.button(text="💘 Дайвинчик", url="https://t.me/leomatchbot")
    b.button(text="⚡ FastLove", url="https://t.me/fastlovetg_bot")
    b.adjust(1)
    return b.as_markup()


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


@dp.callback_query(F.data.startswith("qsphr:"))
async def cb_quickstart_gender(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]  # her | him
    await call.answer()
    await _send_quickstart_phrases(call.message, state, target)


@dp.callback_query(F.data.startswith("qsphr_next:"))
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
        [InlineKeyboardButton(text="👑 Подписка", callback_data="show_premium")],
        [InlineKeyboardButton(text="✨ Возможности бота", url="https://t.me/CueMee")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/furdokw")],
    ])


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


async def _send_start_menu(message: Message, telegram_id: str) -> None:
    if list_contacts(telegram_id):
        await message.answer(
            "С возвращением!\n\n"
            "Жми «💬 Ответ с CueMe» или «🔬 Анализ собеседника» 👇",
            reply_markup=main_kb(),
        )
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
    try:
        record_event(telegram_id, "start")  # для "последнее действие" в /users
    except Exception:
        logging.exception("cmd_start: не удалось записать событие")
    await _send_start_menu(message, telegram_id)
    username = message.from_user.username
    is_test_account = bool(username) and username.lower() in TEST_ACCOUNT_USERNAMES
    if is_new and ADMIN_GROUP_CHAT_ID and not is_test_account:
        who = f"@{username}" if username else f"id{telegram_id} (без username)"
        try:
            await bot.send_message(int(ADMIN_GROUP_CHAT_ID), f"🆕 Новый пользователь: {who}")
        except Exception:
            logging.warning("admin-group new-user notify failed: %s", who)


@dp.callback_query(F.data.in_({"gender:male", "gender:female"}))
async def cb_gender_select(call: CallbackQuery, state: FSMContext) -> None:
    gender = call.data.split(":", 1)[1]
    telegram_id = str(call.from_user.id)
    set_gender(telegram_id, gender)
    # Подтверждение — всплывающим тостом, не отдельным сообщением в чате.
    await call.answer(f"Обращаюсь как к «{_GENDER_LABELS[gender]}»")
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()

    if list_contacts(telegram_id):
        # Уже есть хотя бы один контакт (демо/JSON/Business) — вопрос про пол
        # всегда задаётся СРАЗУ после того, как онбординг только что показал
        # меню и инструкции; повторно слать "С возвращением!" тут не нужно.
        pass
    else:
        # Пол спросили сразу после подключения Автоматизации чатов, ещё до
        # первого реального сообщения — контакта пока нет. Полный экран
        # приветствия тут ни к чему, это уже пройденный шаг — просто
        # показываем подсказку начать диалог (несёт и меню-клавиатуру).
        await _send_no_dialogs_hint(call.message)


@dp.message(Command("gender"))
async def cmd_gender(message: Message) -> None:
    await message.answer("Как теперь к тебе обращаться?", reply_markup=gender_kb())


@dp.callback_query(F.data == "onb:business")
async def cb_onboarding_business(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await call.answer()
    telegram_id = str(call.from_user.id)
    upsert_user(telegram_id, f"user{call.from_user.id}")
    await call.message.answer(
        await _business_connect_text(bot),
        reply_markup=business_connect_kb(),
    )


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
        await _start_unified_reply(message, state)
    elif message.text == BTN_DEEP:
        await _show_deep_analysis(message, bot)
    elif message.text == BTN_DATE:
        await _show_ideal_date(message, bot)
    elif message.text == BTN_SUBSCRIPTION:
        await _show_premium_screen(message, bot, str(message.from_user.id))
    elif message.text == BTN_HELP:
        await _show_help(message)


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
                    "Жми «💬 Ответ с CueMe» — подскажу что ответить.",
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
            "Нажми «🔬 Анализ собеседника» для разбора.",
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
        await state.clear()
        await call.message.edit_text(
            f"Готово! Данные по {name} загружены.\n"
            "Жми «💬 Ответ с CueMe» — подскажу что ответить."
        )
        await call.message.answer("Готово к работе 👇", reply_markup=main_kb())
    else:
        await state.clear()
        await call.message.edit_text("Файл загружен. Используй кнопки меню.")
        await call.message.answer("Меню:", reply_markup=main_kb())


# ── /connect ─────────────────────────────────────────────────────────────────

@dp.message(Command("connect"))
async def cmd_connect(message: Message, bot: Bot) -> None:
    await message.answer(
        await _business_connect_text(bot),
        reply_markup=business_connect_kb(),
    )


# ── /users — список всех пользователей + сводка (только для админа) ─────────
# Шлётся в ADMIN_GROUP_CHAT_ID (если задан), иначе прямым ответом вызвавшему.

async def _resolve_username(bot: Bot, telegram_id: str) -> str:
    """username не хранится в БД (его нет в апдейтах Business API) — тянем
    напрямую у Telegram под отчёт. Без username/при ошибке — сам id как есть."""
    try:
        chat = await bot.get_chat(int(telegram_id))
        if chat.username:
            return f"@{chat.username}"
    except Exception:
        pass
    return f"id{telegram_id}"


_INACTIVE_AFTER_DAYS = 14  # порог для "Неактивных" в сводке /users


def _relative_label(iso_str: str | None, now: datetime) -> str:
    """"3 дня назад" / "сегодня" / "никогда" — для last-action/last-incoming
    полей в /users. iso_str — дата в формате, который пишут record_event
    (UTC ISO с tz) или business_messages.date (может быть без tz — в таком
    случае считаем его тоже UTC, как и остальные даты в проекте)."""
    if not iso_str:
        return "никогда"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return "никогда"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now - dt).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def _days_since(iso_str: str | None, now: datetime) -> int | None:
    """Сколько полных дней прошло с даты. None — даты нет/не распарсилась
    (в CSV уходит пустой ячейкой, а не нулём: «неизвестно» ≠ «сегодня»)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def _csv_dt(iso_str: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Дата для CSV в стабильном ISO-подобном виде (сортируется как текст).
    Пустая строка, если даты нет."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime(fmt)


def _yn(value: bool) -> str:
    return "да" if value else "нет"


async def _collect_users_data(bot: Bot) -> tuple[list[dict], dict]:
    """ОДИН тяжёлый проход по всем юзерам — из него рендерятся и HTML-отчёт
    (_build_users_report), и CSV (_build_users_csv), чтобы не звать get_chat и
    подсчёт сообщений дважды. Возвращает (строки по юзерам, агрегаты).

    Метрики по фичам берутся ПАКЕТНО (5 запросов на весь отчёт, не на юзера) —
    см. event_counts_by_user и соседние в storage.py."""
    now = datetime.now(timezone.utc)
    users = list_all_users()

    # Пакетные метрики — по одному запросу на всех юзеров сразу.
    events_by_user = event_counts_by_user()
    deep_analysis_users = users_with_deep_analysis()
    style_card_users = users_with_style_card()
    referrals_by_user = referral_counts_by_user()

    rows: list[dict] = []
    totals = {
        "total": len(users), "with_gender": 0, "with_contact": 0,
        "with_ref_premium": 0, "blocked": 0, "automation_off": 0, "inactive": 0,
        "premium_now": 0, "used_reply": 0, "used_screenshot": 0, "used_live": 0,
        "used_deep_analysis": 0, "active_7d": 0,
    }

    for u in users:
        tid = u["telegram_id"]
        who = await _resolve_username(bot, tid)
        contacts = list_contacts(tid)

        if u["gender"]:
            totals["with_gender"] += 1
        if contacts:
            totals["with_contact"] += 1

        # Контактов может быть больше, чем реально накопленных переписок —
        # контакт создаётся уже от одного исходящего business-сообщения,
        # до того как придёт хоть один входящий. Число сообщений тут же —
        # чтобы это не выглядело странно в отчёте.
        msg_count = sum(
            count_biz_messages_for_contact(tid, c["id"]) + count_imported_messages(c["id"])
            for c in contacts
        )

        premium_line = ""
        until_raw = u["deep_analysis_free_until"]
        if until_raw:
            try:
                until_dt = datetime.fromisoformat(until_raw)
                if until_dt > now:
                    premium_line = f"\n    👑 Premium (реферал) до {until_dt.strftime('%d.%m %H:%M')}"
                    totals["with_ref_premium"] += 1
            except ValueError:
                pass

        is_blocked = bool(u["blocked_bot"])
        if is_blocked:
            totals["blocked"] += 1

        # Отдельный от blocked_bot сигнал отвала — можно отключить
        # Автоматизацию чатов в настройках Telegram, не блокируя самого бота
        # (и наоборот). Берём САМОЕ СВЕЖЕЕ подключение — юзер мог
        # переподключаться несколько раз, старые connection_id не трогаем.
        latest_conn = get_latest_business_connection(tid)
        automation_off = bool(latest_conn) and not latest_conn["is_enabled"]
        if automation_off:
            totals["automation_off"] += 1

        last_action_raw = get_last_event_time(tid)
        last_incoming_raw = get_last_incoming_message_time(tid)
        # "Последняя активность" для отсева неактивных — позже из двух
        # сигналов (сам что-то сделал ИЛИ ему написали).
        candidates = [d for d in (last_action_raw, last_incoming_raw) if d]
        last_activity_raw = max(candidates) if candidates else None
        is_stale = False
        if last_activity_raw:
            try:
                last_dt = datetime.fromisoformat(last_activity_raw)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                is_stale = (now - last_dt).days > _INACTIVE_AFTER_DAYS
            except ValueError:
                pass
        else:
            is_stale = True  # ни одного сигнала активности вообще
        if is_stale:
            totals["inactive"] += 1

        # "Активен" (не заблокировал, не отключал автоматизацию) — НЕ значит
        # "сообщения реально идут". Отдельно помечаем застой, чтобы статус не
        # выглядел противоречиво рядом с "N дн. назад" по последнему сообщению.
        issues = []
        if is_blocked:
            issues.append("🚫 Заблокировал бота")
        if automation_off:
            issues.append("🔌 Отключил Автоматизацию чатов")
        if is_stale and not is_blocked and not automation_off:
            issues.append(f"⚠️ Нет активности &gt;{_INACTIVE_AFTER_DAYS} дн.")
        status_line = " · ".join(issues) if issues else "✅ Активен"

        # Использование фич — из уже загруженных пакетных срезов, без запросов.
        ev = events_by_user.get(tid, {})
        uses_reply = ev.get("gen_reply_variants", 0)
        uses_screenshot = ev.get("gen_screenshot_variants", 0)
        uses_live = ev.get("gen_live", 0) + ev.get("gen_live_regen", 0)
        used_deep_analysis = tid in deep_analysis_users

        # Premium: _is_premium ходит в Telegram (с кэшем), поэтому зовём ОДИН
        # раз, а источник доопределяем локальными проверками в том же порядке
        # приоритета, что внутри самой _is_premium — без второго обращения.
        is_premium_now = await _is_premium(bot, tid)
        if not is_premium_now:
            premium_source = "нет"
        elif _has_referral_premium(tid):
            premium_source = "referral"
        elif _has_promo_channel_premium(tid):
            premium_source = "promo_channel"
        else:
            premium_source = "tribute"
        if is_premium_now:
            totals["premium_now"] += 1

        days_since_active = _days_since(last_action_raw, now)
        active_7d = days_since_active is not None and days_since_active <= 7

        if uses_reply:
            totals["used_reply"] += 1
        if uses_screenshot:
            totals["used_screenshot"] += 1
        if uses_live:
            totals["used_live"] += 1
        if used_deep_analysis:
            totals["used_deep_analysis"] += 1
        if active_7d:
            totals["active_7d"] += 1

        rows.append({
            "username": who,
            "telegram_id": tid,
            "gender": _GENDER_LABELS.get(u["gender"], "?"),
            "source": _SOURCE_LABELS.get(u["acquisition_source"], "не указан"),
            "contacts_count": len(contacts),
            "messages_count": msg_count,
            "blocked": is_blocked,
            "automation_off": automation_off,
            "signup_date": _csv_dt(u["created_at"], "%Y-%m-%d"),
            "days_since_signup": _days_since(u["created_at"], now),
            "last_action_at": _csv_dt(last_action_raw),
            "last_incoming_at": _csv_dt(last_incoming_raw),
            "days_since_last_active": days_since_active,
            "active_last_7d": active_7d,
            "uses_reply": uses_reply,
            "uses_screenshot": uses_screenshot,
            "uses_live": uses_live,
            "used_deep_analysis": used_deep_analysis,
            "has_style_card": tid in style_card_users,
            "is_premium_now": is_premium_now,
            "premium_source": premium_source,
            "trial_used": u["trial_used"],
            "referrals_made": referrals_by_user.get(tid, 0),
            # только для HTML-отчёта, в CSV не идут
            "_status_line": status_line,
            "_premium_line": premium_line,
            "_last_action_label": _relative_label(last_action_raw, now),
            "_last_incoming_label": _relative_label(last_incoming_raw, now),
        })

    return rows, totals


def _build_users_report(rows: list[dict], totals: dict, hidden_count: int = 0) -> list[str]:
    """Возвращает список блоков (шапка, по одному на юзера, сводка) — НЕ
    склеенную строку, чтобы cmd_users мог резать на чанки по границам
    блоков, а не посреди HTML-тега.

    rows — сюда передаются ТОЛЬКО активные (см. cmd_users: фильтр по
    _status_line == "✅ Активен") — заблокировавших бота, отключивших
    Автоматизацию и застойных юзеров в сообщение не выводим, они есть
    только в CSV. hidden_count — сколько строк скрыто, для пометки в шапке."""
    blocks = ["👥 <b>Пользователи CueMe</b>"]
    if hidden_count:
        blocks.append(
            f"Показаны только активные — {hidden_count} неактивных/заблокировавших/"
            "отключивших Автоматизацию скрыты из сообщения, они есть в CSV-файле ниже."
        )

    for r in rows:
        blocks.append(
            f"👤 <b>{html.escape(r['username'])}</b>\n"
            f"    Пол: {r['gender']} · Триал: {r['trial_used']}\n"
            f"    Источник: {r['source']}\n"
            f"    Контактов: {r['contacts_count']} · Сообщений: {r['messages_count']}\n"
            f"    Последнее действие: {r['_last_action_label']} · "
            f"Последнее сообщение от собеседника: {r['_last_incoming_label']}\n"
            f"    Статус: {r['_status_line']}{r['_premium_line']}"
        )

    blocks.append(
        f"📊 <b>Сводка</b>\n"
        f"Всего: {totals['total']}\n"
        f"С полом: {totals['with_gender']}\n"
        f"С контактом: {totals['with_contact']}\n"
        f"С активной реферальной Premium: {totals['with_ref_premium']}\n"
        f"Заблокировали бота: {totals['blocked']}\n"
        f"Отключили Автоматизацию чатов: {totals['automation_off']}\n"
        f"Неактивных (&gt;{_INACTIVE_AFTER_DAYS} дн.): {totals['inactive']}"
    )
    blocks.append(
        f"🧩 <b>Пользуются функциями</b>\n"
        f"Ответить за меня: {totals['used_reply']}\n"
        f"По скриншоту: {totals['used_screenshot']}\n"
        f"Ответить с CueMe (live): {totals['used_live']}\n"
        f"Анализ собеседника: {totals['used_deep_analysis']}\n"
        f"Активны за 7 дней: {totals['active_7d']}\n"
        f"Premium сейчас: {totals['premium_now']}"
    )
    return blocks


# Порядок столбцов CSV. Значения берутся из строк _collect_users_data по этим
# же ключам, служебные поля с "_" в выгрузку не идут.
_USERS_CSV_COLUMNS = [
    "username", "telegram_id", "gender", "source",
    "contacts_count", "messages_count", "blocked", "automation_off",
    "signup_date", "days_since_signup", "last_action_at", "last_incoming_at",
    "days_since_last_active", "active_last_7d",
    "uses_reply", "uses_screenshot", "uses_live",
    "used_deep_analysis", "has_style_card",
    "is_premium_now", "premium_source", "trial_used", "referrals_made",
]


def _build_users_csv(rows: list[dict]) -> bytes:
    """CSV для выгрузки в Excel/Sheets: utf-8-sig (иначе Excel ломает кириллицу)
    и разделитель ';'. Булевы — «да/нет», отсутствующие числа — пустая ячейка
    (чтобы «неизвестно» не считалось нулём при подсчёте средних)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(_USERS_CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            _yn(r[col]) if isinstance(r[col], bool)
            else ("" if r[col] is None else r[col])
            for col in _USERS_CSV_COLUMNS
        ])
    return buf.getvalue().encode("utf-8-sig")


@dp.message(Command("users"))
async def cmd_users(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    rows, totals = await _collect_users_data(bot)
    # В сообщении — только активные (не заблокировали бота, не отключали
    # Автоматизацию, не застойные >_INACTIVE_AFTER_DAYS дн.) — остальных
    # показываем только в CSV-файле, не хотим захламлять сообщение.
    active_rows = [r for r in rows if r["_status_line"] == "✅ Активен"]
    blocks = _build_users_report(active_rows, totals, hidden_count=len(rows) - len(active_rows))
    # Телеграм режет на 4096 символов — рубим ПО ГРАНИЦАМ блоков (не
    # посимвольно), иначе легко разрезать HTML-тег пополам и получить
    # ошибку парсинга у Telegram вместо отчёта.
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 3500:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    target_chat = int(ADMIN_GROUP_CHAT_ID) if ADMIN_GROUP_CHAT_ID else message.chat.id
    for chunk in chunks:
        try:
            await bot.send_message(target_chat, chunk, parse_mode="HTML")
        except Exception:
            logging.warning("cmd_users: send failed to %s", target_chat)
            await message.answer(chunk, parse_mode="HTML")

    # Тем же проходом — CSV со всеми метриками (в сообщениях выше только
    # обзор, в файле — полная таблица для Excel/Sheets).
    if not rows:
        return
    filename = f"cueme_users_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    document = BufferedInputFile(_build_users_csv(rows), filename=filename)
    try:
        await bot.send_document(target_chat, document)
    except Exception:
        logging.warning("cmd_users: csv send failed to %s", target_chat)
        await message.answer_document(document)


# ── /sources — статистика по источникам привлечения (только для админа) ────
# Считает acquisition_source (спрашивается только на Business-пути).

_SOURCE_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "tgchat": "С чата в Telegram",
    "friend": "От друга",
    "other": "Другое",
}


@dp.message(Command("sources"))
async def cmd_sources(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    users = list_all_users()
    counts: dict[str, int] = {}
    for u in users:
        source = u["acquisition_source"]
        counts[source] = counts.get(source, 0) + 1

    lines = ["📊 Источники (через Автоматизацию чатов):"]
    for key, label in _SOURCE_LABELS.items():
        lines.append(f"{label}: {counts.get(key, 0)}")
    lines.append(f"Не указано: {counts.get(None, 0)}")

    target_chat = int(ADMIN_GROUP_CHAT_ID) if ADMIN_GROUP_CHAT_ID else message.chat.id
    try:
        await bot.send_message(target_chat, "\n".join(lines))
    except Exception:
        logging.warning("cmd_sources: send failed to %s", target_chat)
        await message.answer("\n".join(lines))


# ── /suggestion_stats — использование подсказок CueMe (только для админа) ───
# Считает по каждому юзеру: сколько вариантов ответа бот показал, сколько из
# них реально ушло собеседнику — как есть (ratio>=0.85) и с правками
# (0.5-0.85), см. main._match_outgoing_to_suggestion / storage.suggestion_stats_by_user.

@dp.message(Command("suggestion_stats"))
async def cmd_suggestion_stats(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    rows = suggestion_stats_by_user()
    if not rows:
        await message.answer("Подсказок пока не показывали никому.")
        return

    lines = ["🤖 Использование подсказок CueMe:"]
    total_all = exact_all = edited_all = 0
    for r in rows:
        who = await _resolve_username(bot, r["telegram_id"])
        total, exact_n, edited_n = r["total"], r["exact_n"] or 0, r["edited_n"] or 0
        used = exact_n + edited_n
        pct = used / total if total else 0.0
        lines.append(
            f"{who}: {used}/{total} использовано ({pct:.0%}) — "
            f"как есть {exact_n}, с правками {edited_n}"
        )
        total_all += total
        exact_all += exact_n
        edited_all += edited_n

    used_all = exact_all + edited_all
    pct_all = used_all / total_all if total_all else 0.0
    lines.append("")
    lines.append(
        f"Итого: {used_all}/{total_all} использовано ({pct_all:.0%}) — "
        f"как есть {exact_all}, с правками {edited_all}"
    )

    target_chat = int(ADMIN_GROUP_CHAT_ID) if ADMIN_GROUP_CHAT_ID else message.chat.id
    try:
        await bot.send_message(target_chat, "\n".join(lines))
    except Exception:
        logging.warning("cmd_suggestion_stats: send failed to %s", target_chat)
        await message.answer("\n".join(lines))


# ── /export — выгрузка переписок юзера в .zip (только для админа) ───────────
# Обходит отсутствие SSH-доступа к серверу: файл прилетает прямо в Telegram.

@dp.message(Command("export"))
async def cmd_export(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    users = list_all_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return

    b = InlineKeyboardBuilder()
    for u in users:
        tid = u["telegram_id"]
        who = await _resolve_username(bot, tid)
        b.button(text=who, callback_data=f"export:{tid}")
    b.adjust(1)
    await message.answer("Кого экспортировать?", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("export:"))
async def cb_export_user(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    telegram_id = call.data.split(":", 1)[1]
    await call.answer()
    who = await _resolve_username(bot, telegram_id)

    contacts = list_contacts(telegram_id)
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in contacts:
            try:
                export = extract_conversation(c["id"])
            except ValueError:
                continue
            if not export["messages"]:
                continue
            safe_name = re.sub(r"[^\w\-]+", "_", export["contact_name"] or f"contact{c['id']}")
            zf.writestr(f"{safe_name}.json", json.dumps(export, ensure_ascii=False, indent=2))
            zf.writestr(f"{safe_name}.txt", to_text(export))
            zf.writestr(f"{safe_name}.html", to_html(export))
            added += 1

    if added == 0:
        await call.message.answer(f"У {who} нет сохранённых переписок с сообщениями.")
        return

    buf.seek(0)
    filename = f"export_{who.lstrip('@')}.zip"
    await call.message.answer_document(
        BufferedInputFile(buf.read(), filename=filename),
        caption=f"{who} — {added} перепис{'ка' if added == 1 else 'ки' if added < 5 else 'ок'}",
    )


# ── /provider — переключить LLM-провайдера (только для админа) ───────────────
# Меняет каскад ГЛОБАЛЬНО для всего бота (module-level _forced в llm.py), а не
# только для вызывающего — поэтому доступ только админам (ADMIN_TELEGRAM_IDS).

@dp.message(Command("provider"))
async def cmd_provider(message: Message) -> None:
    if not _is_admin(message.from_user.id):
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
        await _send_no_contacts_hint(message)
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
    lines = [f"• {_contact_name(c)}" for c in contacts]
    await message.answer("Загруженные чаты:\n" + "\n".join(lines))


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


# Кнопка «📝 Переписать» и /rewrite убраны совсем — сценарий (черновик без
# привязки к входящему) теперь закрывает «💫 Новый диалог». _style_for_rewrite
# выше не удалена — общий хелпер, используется reply/screenshot тоже.


# ── 💬 Ответить за меня ───────────────────────────────────────────────────────

async def _start_reply(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await _send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card       = await _style_for_rewrite(telegram_id, c["id"])
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ — займёт ~20 секунд...")
            try:
                if not interaction_card:
                    interaction_card = await _gen_interaction_card(c["id"], telegram_id)
                if not style_card:
                    style_card = await _gen_style_card(telegram_id)
            except RateLimitError:
                await message.answer("Лимит LLM исчерпан, попробуй позже.")
                return
            except Exception:
                logging.exception("_start_reply: ошибка генерации карточек")
                await message.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
                return
        # Данных ещё нет (например контакт только что автосоздался от одного
        # исходящего сообщения, входящих ещё не было) — не тупик, отвечаем
        # нейтрально по смыслу, как и в холодном старте «Живого диалога».
        style_card = style_card or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
        interaction_card = interaction_card or _NEUTRAL_INTERACTION_PLACEHOLDER
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


async def _ensure_reply_cards(
    target: Message, telegram_id: str, contact_id: int, edit: bool,
) -> tuple[str, str] | None:
    """style_card/interaction_card для «Ответить за меня» — генерирует
    недостающие. None при ошибке (сообщение об ошибке уже отправлено
    вызывающему). Нет данных не значит тупик — фолбэк на нейтральные
    плейсхолдеры, как в холодном старте «Живого диалога»."""
    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        msg_fn = target.edit_text if edit else target.answer
        await msg_fn("Генерирую анализ — займёт ~20 секунд...")
        try:
            if not interaction_card:
                interaction_card = await _gen_interaction_card(contact_id, telegram_id)
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
        except RateLimitError:
            await msg_fn("Лимит LLM исчерпан, попробуй позже.")
            return None
        except Exception:
            logging.exception("_ensure_reply_cards: ошибка генерации карточек")
            await msg_fn("Не удалось сгенерировать анализ — попробуй ещё раз.")
            return None

    return (
        style_card or _LIVE_NEUTRAL_STYLE_PLACEHOLDER,
        interaction_card or _NEUTRAL_INTERACTION_PLACEHOLDER,
    )


@dp.callback_query(F.data.startswith("reply:"))
async def cb_reply_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()

    cards = await _ensure_reply_cards(call.message, telegram_id, contact_id, edit=True)
    if cards is None:
        return
    style_card, interaction_card = cards

    await state.update_data(
        style_card=style_card, interaction_card=interaction_card, contact_id=contact_id
    )
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(
        f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
    )


# ── 💬 Ответ с CueMe (единая точка входа вместо Скриншот/Ответить/Новый диалог) ──
# Фото/текст/форвард → определение контакта → существующий пайплайн:
# ReplyHelp для выбранного контакта, LiveDialogue для нового.

async def _start_unified_reply(message: Message, state: FSMContext) -> None:
    await state.set_state(UnifiedReply.waiting_for_input)
    await message.answer("Пришли скриншот переписки, перешли сообщение или просто вставь текст")


def unified_contacts_kb(contacts: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        b.button(text=_contact_name(c), callback_data=f"unified_contact:{c['id']}")
    b.button(text="➕ Другой человек", callback_data="unified_contact:new")
    b.adjust(1)
    return b.as_markup()


@dp.message(UnifiedReply.waiting_for_input, _not_command)
async def handle_unified_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.photo:
        await message.answer("Читаю скриншот...")
        try:
            buf = await bot.download(message.photo[-1])
            incoming = await extract_chat_from_image(buf.read())
        except Exception:
            logging.exception("unified: не удалось скачать/распознать скриншот")
            incoming = ""
        if not incoming or incoming.strip() == ILLEGIBLE_MARKER:
            await message.answer("Не смог прочитать скриншот — пришли текст переписки сообщением.")
            return  # остаёмся в UnifiedReply.waiting_for_input
    else:
        txt, _ = await _message_text(bot, message)
        incoming = (txt or "").strip()
        if not incoming:
            await message.answer("Пришли скриншот, перешли сообщение или вставь текст.")
            return

    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    await state.update_data(pending_text=incoming)

    if not contacts:
        await state.set_state(UnifiedReply.waiting_for_name)
        await message.answer(
            "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов."
        )
        return

    await message.answer("Кому отвечаем?", reply_markup=unified_contacts_kb(contacts))


@dp.message(UnifiedReply.waiting_for_name)
async def handle_unified_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пришли имя текстом.")
        return

    data = await state.get_data()
    pending_text = data.get("pending_text")
    if not pending_text:
        await message.answer("Контекст устарел — начни заново через «💬 Ответ с CueMe».")
        await state.clear()
        return

    telegram_id = str(message.from_user.id)
    upsert_user(telegram_id, f"user{telegram_id}")
    contact_id = get_or_create_contact(telegram_id, f"live_{uuid.uuid4().hex}", name)

    await state.set_state(LiveDialogue.waiting_for_incoming)
    await state.update_data(contact_id=contact_id, dialogue_history=[])
    await message.answer(f"Готово — «{name}».")
    try:
        await _process_live_incoming(message, state, bot, pending_text, message.from_user.id)
    except Exception:
        # Страховка для новой автообработки первого сообщения (раньше юзер
        # мог остаться без ответа и без объяснения — см. фикс в
        # _process_live_incoming). Состояние уже LiveDialogue.waiting_for_incoming,
        # так что просто переслать сообщение ещё раз тоже сработает.
        logging.exception("handle_unified_name: сбой автообработки первого сообщения")
        await message.answer(
            "Не получилось обработать первое сообщение — пришли его ещё раз."
        )


@dp.callback_query(F.data.startswith("unified_contact:"))
async def cb_unified_contact(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    raw_id = call.data.split(":", 1)[1]
    data = await state.get_data()
    pending_text = data.get("pending_text")
    if not pending_text:
        await call.answer("Контекст устарел — начни заново через «💬 Ответ с CueMe».", show_alert=True)
        return
    await call.answer()

    if raw_id == "new":
        await state.set_state(UnifiedReply.waiting_for_name)
        await call.message.edit_text(
            "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов."
        )
        return

    contact_id = int(raw_id)
    telegram_id = str(call.from_user.id)
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    cards = await _ensure_reply_cards(call.message, telegram_id, contact_id, edit=True)
    if cards is None:
        return
    style_card, interaction_card = cards

    await state.update_data(
        style_card=style_card, interaction_card=interaction_card, contact_id=contact_id
    )
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(f"Обрабатываю сообщение от {name}...")
    try:
        await _process_reply_incoming(call.message, state, bot, pending_text, call.from_user.id)
    except Exception:
        logging.exception("cb_unified_contact: сбой автообработки первого сообщения")
        await call.message.answer(
            "Не получилось обработать первое сообщение — пришли его ещё раз."
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
    """HTML: текст каждого варианта в <code> — в Telegram такой блок копируется
    по одному тапу, без отдельной кнопки «Скопировать» на каждый вариант."""
    blocks = []
    for i, (name, text) in enumerate(variants):
        letter = _VARIANT_LETTERS[i] if i < len(_VARIANT_LETTERS) else str(i + 1)
        blocks.append(
            f"<b>Вариант {letter}: {html.escape(name)}</b>\n"
            f"<code>{html.escape(text)}</code>"
        )
    return "Вот несколько вариантов — выбирай или комбинируй.\n\n" + "\n\n".join(blocks)


def _save_shown_suggestions(
    telegram_id: str, contact_id: int | None, kind: str, variants: list[tuple[str, str]],
) -> None:
    """Сохраняет тексты всех показанных пользователю вариантов — ДО того, как
    известно, использует он что-то из них (кандидаты на сопоставление с
    реальными исходящими, см. _match_outgoing_to_suggestion). Вызывать сразу
    при показе, для КАЖДОГО показа (включая «Другие варианты» — это тоже
    реально увиденные подсказки, могут быть использованы так же, как первые)."""
    try:
        save_suggestions(telegram_id, contact_id, kind, [text for _, text in variants])
    except Exception:
        logging.exception("suggestions: не удалось сохранить показанные варианты")


# _VARIANT_KINDS — какие ctx["kind"] поддерживают вариантную генерацию.
# «🎯 Другой тон» (точечный выбор одного стиля) убран — оставлена только
# перегенерация; вместе с ней ушла и старая style_pick_kb-инфраструктура.
_VARIANT_KINDS = ("reply", "screenshot")


def variants_result_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"varregen:{action_id}")
    return b.as_markup()


async def _run_variants_generation(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str,
    state: FSMContext | None = None, force_fresh: bool = False,
) -> None:
    """Общий шаг генерации нескольких именованных вариантов ОДНИМ вызовом LLM —
    для «Ответить за меня» / «По скриншоту». Диспетчер по ctx["kind"] зовёт
    нужную из *_variants функций. Гейт и списание триала — один раз за вызов
    (не за каждый вариант), т.к. это один вызов LLM."""
    kind = ctx.get("kind")
    text = ctx.get("text") if kind == "reply" else ctx.get("chat_text")
    if text is None:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card, interaction_card = ctx["style_card"], ctx["interaction_card"]
    signals = ctx.get("data_signals")
    winning = ctx.get("winning")
    gender = get_gender(str(telegram_id))
    cache_key = _style_cache_key(f"{kind}_variants", "", text, style_card, interaction_card, extra=gender or "")

    variants = None
    if not force_fresh:
        cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
        if cached:
            try:
                variants = [tuple(v) for v in json.loads(cached)]
                logging.info("%s-variants: cache hit", kind)
            except (ValueError, TypeError):
                variants = None

    if variants is None:
        # Реальный вызов LLM — здесь и только здесь гейт + списание.
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        prev = ctx.get("variants") if force_fresh else None
        try:
            if kind == "reply":
                variants = await suggest_reply_variants(
                    text, style_card, interaction_card,
                    data_signals=signals, previous_variants=prev, winning_examples=winning,
                    user_gender=gender,
                )
            else:  # screenshot
                variants = await screenshot_variants(
                    text, style_card, interaction_card,
                    previous_variants=prev, data_signals=signals, winning_examples=winning,
                    user_gender=gender,
                )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("%s-variants: ошибка генерации", kind)
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return

        # Успех — списываем ОДНУ попытку (не за каждый вариант — это один вызов
        # LLM) и кэшируем, даже если разбор дал меньше вариантов, чем просили.
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps(variants, ensure_ascii=False))
        try:
            record_event(str(telegram_id), f"gen_{kind}_variants", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие генерации вариантов")

    if not variants:
        await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
        return

    _save_shown_suggestions(str(telegram_id), ctx.get("contact_id"), kind, variants)
    ctx["variants"] = variants
    await _answer_long(
        target, _format_variants(variants), reply_markup=variants_result_kb(action_id), parse_mode="HTML",
    )

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


@dp.callback_query(F.data.startswith("varregen:"))
async def cb_variants_regen(call: CallbackQuery, state: FSMContext) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") not in _VARIANT_KINDS:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer("Подбираю другие варианты...")
    await _run_variants_generation(call.message, ctx, call.from_user.id, call.bot, action_id, state, force_fresh=True)


async def _process_reply_incoming(
    message: Message, state: FSMContext, bot: Bot, incoming: str, user_id: int,
) -> None:
    """Общий хвост «Ответить за меня»: сборка ctx и генерация вариантов.
    user_id — ОТДЕЛЬНЫМ параметром (не message.from_user.id) — при вызове
    из callback-контекста message может быть call.message, чей .from_user
    это бот, не юзер (стандартная ловушка aiogram, см. _prompt_screenshot_style)."""
    telegram_id = str(user_id)
    data = await state.get_data()
    # Состояние НЕ сбрасываем — иначе следующее сообщение улетит в общий
    # авто-режим («Переписать») вместо продолжения «Ответить за меня».
    # Выйти из режима — любая кнопка меню (handle_menu_button сбрасывает state).

    contact_id = data.get("contact_id")
    if not await _quota_gate(bot, message, telegram_id):
        return

    # «Разбор переписки» (_send_reply_analysis) здесь отключён намеренно:
    # пользователь ждёт просто ответ, а не аналитику перед каждым ответом.
    # Вернуть — один вызов: await _send_reply_analysis(message, contact_id, incoming)
    samples = get_message_samples(contact_id) if contact_id else None
    ctx = {
        "kind": "reply", "text": incoming, "result": None, "style": None,
        "contact_id": contact_id,
        "style_card": data["style_card"], "interaction_card": data["interaction_card"],
        "data_signals": _reply_data_signals(samples, incoming),
        "winning": _winning_for_contact(telegram_id, contact_id),
    }
    action_id = _new_action(user_id, ctx)
    await _run_variants_generation(message, ctx, user_id, bot, action_id, state)


@dp.message(ReplyHelp.waiting_for_incoming, _not_command)
async def handle_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await message.answer("Пришли сообщение собеседника текстом или голосовым.")
        return
    await _process_reply_incoming(message, state, bot, incoming, message.from_user.id)


@dp.message(Command("reply"))
async def cmd_reply(message: Message, state: FSMContext) -> None:
    await _start_reply(message, state)


# ── 💫 Живой диалог с нуля (холодный старт, без порога накопления) ───────────

_LIVE_NEUTRAL_STYLE_PLACEHOLDER = (
    "Данных о твоём стиле письма пока нет — пиши так, как типично пишут в "
    "дейтинг-переписке в этом возрасте (18-30): на «ты», без канцелярита и "
    "лишней вежливости, чаще со строчной буквы в начале сообщения и без "
    "строгой пунктуации, разговорной длиной. Без домыслов о привычках автора "
    "сверх этого. Как только появятся другие данные (JSON-экспорт, другие "
    "переписки), стиль подключится сам и станет точнее."
)

# Тот же холодный старт, но для карточки собеседника — контакт мог
# автосоздаться от ОДНОГО исходящего business-сообщения (ещё до первого
# ответа от собеседника), тогда входящих сэмплов для интеракшн-карточки
# просто ещё нет. Раньше в этом случае «Ответить за меня» упирался в тупик
# («Не удалось сгенерировать анализ.») — теперь отвечаем нейтрально по
# смыслу присланного сообщения, без домыслов о манере письма собеседника.
_NEUTRAL_INTERACTION_PLACEHOLDER = (
    "Данных о стиле переписки собеседника пока нет (это первое сообщение с "
    "ним) — отвечай по смыслу присланного текста, без домыслов о его манере "
    "письма. Как только накопится история, бот подстроится точнее."
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


# ── Готовые фразы (статичные скрипты, без LLM и без квоты) ────────────────────
# «Новый диалог» — развилка: 🎯 живой коучинг (существующий флоу) или 🎲 готовые
# открывашки (OPENERS_FOR_HER/HIM). «🔥 Скрипты общения» — отдельная кнопка меню
# с универсальными вопросами (REVIVE_QUESTIONS), работает для любого разговора.
# Показанные варианты в рамках сессии не повторяются (трекинг через FSM data,
# сбрасывается при исчерпании списка).

async def _pick_no_repeat(state: FSMContext, key: str, items: list[str]) -> str:
    """Случайный элемент items, не повторяющий уже показанные в этой сессии
    (индексы в FSM data[key]). Когда весь список исчерпан — начинает заново."""
    data = await state.get_data()
    shown = data.get(key) or []
    remaining = [i for i in range(len(items)) if i not in shown]
    if not remaining:
        remaining = list(range(len(items)))
        shown = []
    idx = random.choice(remaining)
    await state.update_data(**{key: shown + [idx]})
    return items[idx]


def _copy_block(intro: str, phrase: str, kb: InlineKeyboardMarkup) -> tuple[str, dict]:
    """Одна фраза tap-to-copy (HTML <code>) + интро + кнопка «Другой вариант»."""
    text = f"{intro}\n\n<code>{html.escape(phrase)}</code>"
    return text, {"reply_markup": kb, "parse_mode": "HTML"}


# --- Новый диалог: развилка коучинг / готовые фразы ---

def live_start_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Живой коучинг", callback_data="live:coach")
    b.button(text="🎲 Готовые фразы для начала", callback_data="live:phrases")
    b.adjust(1)
    return b.as_markup()


async def _show_live_start(message: Message) -> None:
    await message.answer("Как начнём?", reply_markup=live_start_kb())


@dp.callback_query(F.data == "live:coach")
async def cb_live_coach(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _start_live_dialogue(call.message, state)


def phrases_gender_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👩 Ей", callback_data="phrases:her")
    b.button(text="👨 Ему", callback_data="phrases:him")
    b.adjust(2)
    return b.as_markup()


def phrase_next_kb(target: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data=f"phrase_next:{target}")
    return b.as_markup()


async def _send_opener(msg: Message, state: FSMContext, target: str) -> None:
    items = OPENERS_FOR_HER if target == "her" else OPENERS_FOR_HIM
    phrase = await _pick_no_repeat(state, f"opener_shown_{target}", items)
    intro = "Держи заход (тапни, чтобы скопировать):"
    if "[" in phrase:
        intro += " замени [то, что в скобках] на реальную деталь из анкеты."
    text, kw = _copy_block(intro, phrase, phrase_next_kb(target))
    await msg.answer(text, **kw)


@dp.callback_query(F.data == "live:phrases")
async def cb_live_phrases(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer("Кому пишешь?", reply_markup=phrases_gender_kb())


@dp.callback_query(F.data.startswith("phrases:"))
async def cb_phrases_gender(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]  # her | him
    await call.answer()
    await _send_opener(call.message, state, target)


@dp.callback_query(F.data.startswith("phrase_next:"))
async def cb_phrase_next(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]
    await call.answer("Другой вариант")
    await _send_opener(call.message, state, target)


# --- 🔥 Скрипты общения (универсальные вопросы, отдельная кнопка) ---

def revive_next_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data="revive_next")
    return b.as_markup()


async def _send_revive(msg: Message, state: FSMContext) -> None:
    q = await _pick_no_repeat(state, "revive_shown", REVIVE_QUESTIONS)
    text, kw = _copy_block("Вот что может оживить разговор:", q, revive_next_kb())
    await msg.answer(text, **kw)


async def _show_revive(message: Message, state: FSMContext) -> None:
    await _send_revive(message, state)


@dp.callback_query(F.data == "revive_next")
async def cb_revive_next(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("Другой вариант")
    await _send_revive(call.message, state)


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
    gender = get_gender(telegram_id)
    _, pron = _contact_words(gender)
    await message.answer(
        f"Готово — «{name}». Присылай {pron} сообщения по одному, на каждое сразу дам "
        "несколько вариантов ответа. Чтобы выйти из режима — нажми любую кнопку меню."
    )


async def _process_live_incoming(
    message: Message, state: FSMContext, bot: Bot, incoming: str, user_id: int,
) -> None:
    """Общий хвост «Живого диалога»: сборка ctx и live-коучинг. user_id —
    отдельным параметром, см. комментарий в _process_reply_incoming."""
    telegram_id = str(user_id)
    data = await state.get_data()
    # Состояние НЕ сбрасываем — можно форвардить сообщения одно за другим без
    # повторного нажатия кнопки. Выйти из режима — любая кнопка меню.

    if not await _quota_gate(bot, message, telegram_id):
        return

    contact_id = data.get("contact_id")
    if not contact_id:
        await message.answer("Контекст диалога потерян — начни заново через «💬 Ответ с CueMe».")
        return

    # В отличие от остальных LLM-вызовов в файле, раньше был без try/except —
    # сбой провайдера (рейтлимит/таймаут) тут тихо убивал автообработку
    # первого сообщения сразу после создания контакта: юзер видел "Готово —
    # «имя»." и дальше тишину, без единого намёка на ошибку.
    try:
        style_card = await _gen_style_card(telegram_id) or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
    except RateLimitError:
        await message.answer("Лимит запросов исчерпан — попробуй через пару минут.")
        return
    except Exception:
        logging.exception("_process_live_incoming: не удалось получить стиль")
        await message.answer("Сервис сейчас перегружен — попробуй чуть позже.")
        return
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
    action_id = _new_action(user_id, ctx)

    # Короткая история диалога — эфемерно, в FSM; долгая память — running_notes в БД.
    new_history = (dialogue_history + [incoming])[-8:]
    await state.update_data(dialogue_history=new_history)

    await _run_live_coach_step(message, ctx, user_id, bot, action_id)


@dp.message(LiveDialogue.waiting_for_incoming, _not_command)
async def handle_live_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        contact_gen, _ = _contact_words(get_gender(str(message.from_user.id)))
        await message.answer(f"Пришли сообщение {contact_gen} текстом или голосовым.")
        return
    await _process_live_incoming(message, state, bot, incoming, message.from_user.id)


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
    gender = get_gender(str(telegram_id))

    if force_fresh:
        if not await _quota_gate(bot, target, str(telegram_id)):

             
            return
        try:
            variants = await suggest_reply_variants(
                text, style_card, running_notes, previous_variants=ctx.get("variants"),
                user_gender=gender,
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
        _save_shown_suggestions(str(telegram_id), contact_id, "live", variants)
        ctx["variants"] = variants
        await _answer_long(
            target, _format_variants(variants), reply_markup=live_variants_kb(action_id), parse_mode="HTML",
        )
        return

    cache_key = _style_cache_key("live", "", text, style_card, running_notes, extra=gender or "")
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
                user_gender=gender,
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

    _save_shown_suggestions(str(telegram_id), contact_id, "live", variants)
    ctx["variants"] = variants
    ctx["running_notes"] = updated_notes
    await _answer_long(
        target, _format_variants(variants), reply_markup=live_variants_kb(action_id), parse_mode="HTML",
    )

    message_count = ctx.get("message_count", 0)
    if updated_notes and (message_count == 1 or message_count % LIVE_NOTES_SUMMARY_EVERY == 0):
        preview = _running_notes_preview(updated_notes)
        if preview:
            await target.answer(f"Что я уже понял:\n{preview}")

    contact_gen, _ = _contact_words(gender)
    await target.answer(
        f"Пришли следующее сообщение {contact_gen} — отвечу и на него. "
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
        await _send_no_contacts_hint(message)
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
async def cb_screenshot_contact(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
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
        await _prompt_screenshot_style_no_contact(bot, call.message, call.from_user.id, telegram_id, ctx["chat_text"], state, edit=True)
        return

    contact_id = int(raw_id)
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()
    await _prompt_screenshot_style(bot, call.message, call.from_user.id, telegram_id, contact_id, ctx["chat_text"], state, edit=True)


async def _prompt_screenshot_style(
    bot: Bot, target: Message, user_id: int, telegram_id: str, contact_id: int, chat_text: str,
    state: FSMContext, edit: bool = False,
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
    ctx = {
        "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
        "contact_id": contact_id,
        "style_card": style_card, "interaction_card": interaction_card,
        "data_signals": _reply_data_signals(samples, _last_incoming_line(chat_text)),
        "winning": _winning_for_contact(telegram_id, contact_id),
    }
    action_id = _new_action(user_id, ctx)
    if edit:
        await target.edit_text("Генерирую варианты...")
    else:
        await target.answer("Генерирую варианты...")
    await _run_variants_generation(target, ctx, user_id, bot, action_id, state)


async def _prompt_screenshot_style_no_contact(
    bot: Bot, target: Message, user_id: int, telegram_id: str, chat_text: str,
    state: FSMContext, edit: bool = False,
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

    ctx = {
        "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
        "style_card": style_card, "interaction_card": "",
        "data_signals": _reply_data_signals(None, _last_incoming_line(chat_text)),
    }
    action_id = _new_action(user_id, ctx)
    if edit:
        await target.edit_text("Генерирую варианты...")
    else:
        await target.answer("Генерирую варианты...")
    await _run_variants_generation(target, ctx, user_id, bot, action_id, state)


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


# ── /progress — прогресс накопления по каждому реальному контакту ────────────

def _progress_line(name: str, done: int, threshold: int, is_first: bool) -> str:
    done = min(done, threshold)
    suffix = ("почти готово" if done >= threshold * 0.7 else "готовится") if is_first \
        else "до обновления"
    return f"▪️ <b>{html.escape(name)}</b> — {done}/{threshold} · {suffix}"


@dp.message(Command("progress"))
async def cmd_progress(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await message.answer(
            "Пока нет реальных контактов для отслеживания прогресса — подключи "
            "Автоматизацию чатов (/connect) или загрузи JSON-экспорт."
        )
        return

    lines = ["📊 <b>Прогресс по разбору стиля:</b>\n"]
    for c in contacts:
        name = _contact_name(c)
        total = count_biz_messages_for_contact(telegram_id, c["id"])
        is_first = get_my_style_per_contact(c["id"]) is None
        if is_first:
            done = total + count_imported_messages(c["id"])
            lines.append(_progress_line(name, done, FIRST_BUILD_THRESHOLD, is_first=True))
        else:
            last = get_my_style_last_rebuild_count(c["id"])
            done = max(total - last, 0)
            lines.append(_progress_line(name, done, REBUILD_THRESHOLD, is_first=False))

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── /help ────────────────────────────────────────────────────────────────────

async def _show_help(message: Message) -> None:
    await message.answer(
        "Вот что я умею. На главном экране — кнопка «💬 Ответ с CueMe» плюс "
        "«🔬 Анализ собеседника», «💐 Идеальное свидание» и «👑 Подписка»:\n\n"
        "💬 Ответ с CueMe — пришли скриншот переписки, перешли сообщение или "
        "вставь текст: если контакт уже есть — несколько вариантов ответа "
        "(Флирт/Дружески/Уверенно и т.п.); если нет — заведём новый диалог "
        "(живой коучинг с нуля)\n"
        "/reply — ответить на его сообщение\n"
        "/screenshot — ответить по скриншоту переписки (можно слать скриншоты "
        "один за другим)\n\n"
        "<b>🔬 Анализ собеседника</b> (кнопка в меню)\n"
        "/deep_analysis — совместимость, как писать этому человеку, стиль и "
        "флаги, готовое сообщение\n\n"
        "<b>💐 Идеальное свидание</b> (кнопка в меню) — идея свидания и подарков под человека\n\n"
        "<b>👑 Подписка</b> (кнопка в меню) — статус подписки + "
        f"🎁 Пригласить друга (/invite) — получить свой код, за друга по коду дадим "
        f"{REFERRAL_REWARD_DAYS} дня Premium подписки\n\n"
        "<b>⚙️ Аккаунт</b>\n"
        "/contacts — список загруженных чатов\n"
        "/connect — как подключить Автоматизацию чатов (живой поток переписки)\n"
        "/progress — сколько накопилось до разбора/следующего обновления\n"
        "/redeem — ввести код друга\n"
        "/myref — сколько друзей привёл и активна ли награда\n"
        "/premium — статус подписки\n"
        "/rebuild — принудительно пересобрать все карточки заново\n"
        "/delete — удалить свои данные\n\n"
        "<b>🎬 Остальное</b>\n"
        "/start — начало работы\n"
        "/help — это сообщение\n\n"
        f"💎 {FREE_TRIAL_REQUESTS} бесплатных попыток на ответ/скриншот, "
        "дальше и остальные функции — по подписке. Статус — /premium.",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show_help(message)


# ── /premium — статус подписки ────────────────────────────────────────────────

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


def _premium_expiry_line(telegram_id: str) -> str:
    """Строка «сколько осталось + способ оплаты» для активного Premium, или
    "" если срок неизвестен (Tribute — см. ниже). Порядок проверки — тот же
    приоритет, что уже использует _is_premium (реферал → промо-канал →
    Stars → членство в канале Tribute), поэтому если ни один источник с
    известной датой не сработал, а _is_premium всё равно True — остаётся
    только Tribute."""
    now = datetime.now(timezone.utc)

    until = get_deep_analysis_free_until(telegram_id)
    if until and until > now:
        return (
            f"🎁 Реферальная награда — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )

    until = get_promo_channel_premium_until(telegram_id)
    if until and until > now:
        return (
            f"📢 Награда за подписку на канал — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )

    until = get_stars_premium_until(telegram_id)
    if until and until > now:
        payment = get_latest_star_payment(telegram_id)
        if payment and payment["is_subscription"]:
            return (
                f"⭐ Stars-подписка (автопродление) — следующее списание "
                f"{_format_until(until)} (через {_format_remaining(until - now)}). "
                "Отменить — в Telegram: Настройки → Мои подписки."
            )
        return (
            f"⭐ Оплачено Stars — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )

    # Ни одного источника с известной датой — по приоритету _is_premium
    # остаётся только членство в приватном канале Tribute. САМОГО срока
    # окончания бот не знает: Tribute продлевает/отменяет подписку на своей
    # стороне, боту доступен только текущий факт членства (get_chat_member).
    return (
        "💎 Подписка оформлена через Tribute — продление и отмена на их "
        "стороне, точную дату окончания бот не знает."
    )


async def _premium_status_text(bot: Bot, telegram_id: str) -> str:
    if await _is_premium(bot, telegram_id):
        expiry_line = _premium_expiry_line(telegram_id)
        return f"👑 Подписка:\n\n✅ Активна — весь функционал CueMe без ограничений.\n\n{expiry_line}"

    used = get_trial_used(telegram_id)
    left = max(0, FREE_TRIAL_REQUESTS - used)
    if left == 0:
        return (
            "👑 Подписка:\n\n"
            "❌ Не активна\n\n"
            "⏳ Бесплатные попытки закончились — но, похоже, тебе заходит 😏\n"
            "Дальше по подписке — весь функционал плюс полный разбор собеседника с подарками.\n\n"
            "Чтобы получить БЕСПЛАТНУЮ подписку перейдите в раздел 🎁 Пригласи друга\n\n"
            "Оплатили, но бот не видит подписку? Подождите пару минут и снова наберите /premium."
        )
    return (
        "👑 Подписка:\n\n"
        "❌ Не активна\n\n"
        f"⏳ Бесплатных попыток осталось: {left} из {FREE_TRIAL_REQUESTS} \n\n"
        "Чтобы получить БЕСПЛАТНУЮ подписку перейдите в раздел 🎁 Пригласи друга\n\n"
        "Оплатили, но бот не видит подписку? Подождите пару минут и снова наберите /premium."
    )


@dp.message(Command("premium"))
async def cmd_premium(message: Message, bot: Bot) -> None:
    text = await _premium_status_text(bot, str(message.from_user.id))
    await message.answer(text, reply_markup=paywall_kb())


async def _show_premium_screen(target: Message, bot: Bot, telegram_id: str) -> None:
    text = await _premium_status_text(bot, telegram_id)
    await target.answer(text, reply_markup=premium_menu_kb())


@dp.callback_query(F.data == "show_premium")
async def cb_show_premium(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_premium_screen(call.message, bot, str(call.from_user.id))


@dp.callback_query(F.data == "show_invite")
async def cb_show_invite(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_invite(call.message, bot, str(call.from_user.id))


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


async def _resolve_target_id(
    message: Message, bot: Bot, command: str,
) -> tuple[str, str] | None:
    """Разбирает "<telegram_id>" или "@username" из текста команды — общий
    парсинг для /wipe и /inspect. Возвращает (target_id, arg_display) или
    None (сообщение об ошибке уже отправлено)."""
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) == 2 else ""
    usage = f"Использование: {command} <telegram_id> или {command} @username"
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


# ── /inspect — диагностика конкретного юзера (только для админа) ────────────
# Отличить "этот собеседник просто замолчал" от "Business-соединение реально
# отвалилось" — история подключений + по каждому контакту отдельно последние
# входящие/исходящие даты.

@dp.message(Command("inspect"))
async def cmd_inspect(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    resolved = await _resolve_target_id(message, bot, "/inspect")
    if resolved is None:
        return
    target_id, _ = resolved

    who = await _resolve_username(bot, target_id)
    now = datetime.now(timezone.utc)
    lines = [f"🔎 <b>{html.escape(who)}</b> (id{target_id})\n"]

    conns = get_business_connections_history(target_id)
    if not conns:
        lines.append("<b>Business API:</b> подключения не было ни разу.")
    else:
        lines.append("<b>История подключений Business API:</b>")
        for c in conns:
            status = "🟢 включено" if c["is_enabled"] else "🔴 отключено"
            lines.append(f"  {status} · с {c['created_at'][:16]} (conn {c['connection_id'][:12]}…)")

    contacts = list_contacts(target_id)
    lines.append(f"\n<b>Контакты ({len(contacts)}):</b>")
    if not contacts:
        lines.append("  нет ни одного контакта")
    for c in contacts:
        name = _contact_name(c)
        spans = get_contact_last_messages(target_id, c["id"])
        last_in = _relative_label(spans["last_in"], now)
        last_out = _relative_label(spans["last_out"], now)
        lines.append(f"  • {html.escape(name)} — входящие: {last_in} · исходящие: {last_out}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── /wipe — стереть данные ПРОИЗВОЛЬНОГО пользователя (только для админа) ────
# В отличие от /delete (только свои данные), берёт telegram_id аргументом —
# для тестовых аккаунтов разработчика, чтобы проверять онбординг/рефералку
# с чистого листа, «как будто пользователь никогда не пользовался ботом».

@dp.message(Command("wipe"))
async def cmd_wipe(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    resolved = await _resolve_target_id(message, bot, "/wipe")
    if resolved is None:
        return
    target_id, arg = resolved

    b = InlineKeyboardBuilder()
    b.button(text=f"‼️ Да, стереть {target_id}", callback_data=f"wipeyes:{target_id}")
    b.button(text="Отмена", callback_data="wipeno")
    b.adjust(1)
    await message.answer(
        f"Стереть ВСЕ данные пользователя {arg} — как будто он никогда не "
        "пользовался ботом? Необратимо.",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("wipeyes:"))
async def cb_wipe_confirm(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    target_id = call.data.split(":", 1)[1]
    delete_all_user_data(target_id)
    await call.answer("Стёрто")
    await call.message.edit_text(f"✓ Все данные пользователя {target_id} удалены.")


@dp.callback_query(F.data == "wipeno")
async def cb_wipe_cancel(call: CallbackQuery) -> None:
    await call.answer("Отменено")
    await call.message.edit_text("Отменено.")


# /auto и auto_rewrite_handler (catch-all авто-переписка) убраны вместе с
# «Переписать» — тот же сценарий (черновик без привязки к входящему) теперь
# закрывает «💫 Новый диалог». get_auto_mode/set_auto_mode/auto_contact_id в
# storage.py не тронуты (неиспользуемые, но безвредные) — не было смысла
# трогать схему БД ради этого.


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
        BotCommand(command="gender",      description="Сменить пол"),
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="connect",     description="Подключить Автоматизацию чатов"),
        BotCommand(command="me",          description="Мой стиль общения"),
        BotCommand(command="screenshot",  description="Ответить по скриншоту"),
        BotCommand(command="reply",       description="Помочь ответить собеседнику"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
        BotCommand(command="progress",    description="Прогресс накопления по контактам"),
        BotCommand(command="deep_analysis", description="Анализ собеседника"),
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
            "pre_checkout_query",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
