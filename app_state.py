import asyncio
import contextvars
import csv
import difflib
import hashlib
import hmac
import html
import io
import itertools
import json
import logging
import random
import re
import sqlite3
import string
import tempfile
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery, ChatMemberUpdated, CopyTextButton, Document, ErrorEvent, FSInputFile,
    InputMediaDocument, InputRichMessage, LinkPreviewOptions, Message,
    InputMediaAudio, InputMediaPhoto, InputMediaVideo, MessageEntity,
    InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery,
    MessageOriginUser, MessageOriginHiddenUser, MessageOriginChat, MessageOriginChannel,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import (
    ADMIN_GROUP_CHAT_ID,
    ADMIN_TELEGRAM_IDS,
    APP_NAME,
    BOT_TOKEN,
    FIRST_BUILD_THRESHOLD,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENROUTER_API_KEY,
    PREMIUM_CACHE_TTL,
    PREMIUM_NEG_CACHE_TTL,
    CHURN_HOURS,
    PREMIUM_CHANNEL_ID,
    PREMIUM_SUBSCRIBE_URL,
    TRIBUTE_API_KEY,
    TRIBUTE_WEBHOOK_PATH,
    TRIBUTE_WEBHOOK_PORT,
    TRIBUTE_CHANNEL_ID,
    LLM_CACHE_TTL_SEC,
    ONBOARDING_PHOTO_FILE_ID,
    ONBOARDING_PHOTO_PATH,
    MAIN_MENU_LOGO_FILE_ID,
    MAIN_MENU_LOGO_PATH,
    CONNECT_EFFECT_ID,
    PROGRESS_MIN_SHOW_SECONDS,
    ONBOARDING_JSON_POST_URL,
    OPENERS_FOR_HER,
    OPENERS_FOR_HIM,
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REPLY_BONUS,
    PROMO_CHANNEL_ANALYSIS_BONUS,
    PROMO_CHANNEL_DATE_BONUS,
    REBUILD_THRESHOLD,
    REFERRAL_REPLY_BONUS,
    REFERRAL_ANALYSIS_BONUS,
    REFERRAL_DATE_BONUS,
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
    get_invalid_output_counts,
    set_invalid_output_hook,
    # ILLEGIBLE_MARKER, extract_chat_from_image, screenshot_variants — были
    # нужны только функции «скриншот переписки → ответ», убранной целиком
    # (см. пометки у Screenshot/handle_unified_input/секции «Ответить по
    # скриншоту» ниже) — сами функции остались в llm.py на случай отката.
    PROVIDER_NAMES,
    RateLimitError,
    build_compatibility_interpretation,
    build_ideal_date,
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
    analyze_reply_dynamics,
    get_forced_provider,
    get_provider_stats,
    get_provider_stats_snapshot,
    get_groq_model_stats,
    KNOWN_DAILY_LIMITS,
    live_coach_step,
    make_features_summary,
    NEUTRAL_INTERACTION_PLACEHOLDER,
    sample_texts,
    set_forced_provider,
    suggest_reply_variants,
    transcribe_audio,
)
from compatibility_metrics import compute_all as compute_compat_metrics, MOSCOW_TZ
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
    find_unlinked_manual_contact_by_username,
    get_all_dated_messages,
    get_all_dated_my_messages,
    get_all_per_contact_style_cards,
    get_any_user_samples,
    get_biz_messages_for_contact,
    get_business_connection,
    get_business_message_by_tg_id,
    get_business_message_by_row_id,
    create_deleted_reveal,
    get_deleted_reveal,
    mark_deleted_reveal_revealed,
    get_unrevealed_deleted_reveals,
    get_contact_id_for_chat_ref,
    get_business_connections_history,
    get_contact_last_messages,
    get_recent_unmatched_suggestions,
    get_latest_business_connection,
    get_contact_by_id,
    get_analysis_trial_count,
    get_date_trial_count,
    add_reply_trial_bonus,
    get_reply_trial_bonus,
    add_analysis_trial_bonus,
    get_analysis_trial_bonus,
    add_date_trial_bonus,
    get_date_trial_bonus,
    get_deep_analysis,
    get_acquisition_source,
    get_deep_analysis_free_until,
    get_gender,
    get_promo_channel_premium_until,
    get_promo_channel_pause,
    get_users_with_active_promo_premium,
    get_users_with_known_premium_expiry,
    get_premium_expiry_reminder_until,
    set_premium_expiry_reminder_until,
    pause_promo_channel_premium,
    resume_promo_channel_premium,
    get_ideal_date,
    get_last_event_time,
    set_last_action,
    get_last_gate_event,
    gate_events_by_user,
    gate_check_fail_without_ok,
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
    get_tribute_premium_until,
    set_tribute_premium_until,
    record_tribute_payment,
    get_trial_used,
    get_user,
    increment_trial_used,
    mark_analysis_trial_used,
    mark_bot_blocked,
    mark_bot_unblocked,
    is_legacy_kb_cleared,
    mark_legacy_kb_cleared,
    get_users_without_deleted_messages_broadcast,
    mark_deleted_messages_broadcast_sent,
    get_users_without_deleted_paywall_broadcast,
    mark_deleted_paywall_broadcast_sent,
    get_users_without_price_drop_broadcast,
    get_users_for_funnel_scan,
    set_funnel_reminder_state,
    count_business_messages_for_owner,
    get_best_active_contact_for_funnel,
    get_user_typical_active_hour_utc,
    mark_price_drop_broadcast_sent,
    mark_date_trial_used,
    mark_referral_credited,
    merge_manual_contact_into,
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
    claim_saved_ephemeral,
    save_pending_unlock,
    get_pending_unlock,
    delete_pending_unlock,
    update_business_message_text,
    update_business_message_content,
    get_business_media_group,
    get_watch_setting,
    toggle_watch_setting,
    WATCH_SETTINGS,
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
    record_gift_premium,
    record_star_payment,
    set_acquisition_source,
    set_deep_analysis_free_until,
    set_gender,
    set_promo_channel_reward,
    mark_promo_reward_claimed,
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


# ── Устойчивость к задержкам (инцидент 2026-10-12) ───────────────────────────
# Если бот обработал нажатие кнопки позже ~15 с, Telegram отвечает на
# call.answer() «query is too old», и обработчик падал целиком — юзер видел
# «Что-то пошло не так» вместо результата, хотя само действие можно было
# доделать. Теперь эта конкретная ошибка у answer() глушится (спиннер у кнопки
# просто не погаснет), остальные — как раньше.
_orig_callback_answer = CallbackQuery.answer
_STALE_QUERY_HINTS = ("query is too old", "query id is invalid")


def _safe_callback_answer(self, *args, **kwargs):
    method = _orig_callback_answer(self, *args, **kwargs)

    async def run():
        try:
            return await method
        except TelegramBadRequest as e:
            if any(h in str(e).lower() for h in _STALE_QUERY_HINTS):
                logging.warning("callback answer опоздал (бот был занят), продолжаю: %s", self.data)
                return False
            raise
    return run()


CallbackQuery.answer = _safe_callback_answer


async def _edit_or_keep(target: Message, text: str) -> Message:
    """edit_text, который не падает на «message is not modified» (повторное
    нажатие, пока висит тот же кадр анимации) — тогда просто берём сообщение
    как есть."""
    try:
        return await target.edit_text(text)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return target
        raise


# Датчик зависаний: раз в секунду проверяет, не опоздал ли сам цикл бота.
# Опоздание > 2 с значит, что какой-то код держит цикл (тяжёлый синхронный
# запрос к базе, расчёт и т.п.) — все нажатия в это время ждут.
_LOOP_LAG_WARN_SECONDS = 2.0


async def _watch_event_loop_lag() -> None:
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - t0 - 1.0
        if lag > _LOOP_LAG_WARN_SECONDS:
            logging.warning("event loop lag %.1f с — бот был занят и не обрабатывал нажатия", lag)


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
BTN_WATCH         = "🗑 Удалённые сообщения"
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

# Защита от параллельных пересборок одного контакта
_rebuilding: set[int] = set()
# Лавина пересборок (инцидент 2026-10-12): после перезапуска бота (каждый
# деплой) пауза пересборок обнулялась, и на первое же сообщение каждой
# переписки с накопленными 50+ новыми стартовала своя пересборка — ~40 за
# 20 с. Каждая синхронно, прямо в цикле бота, пересчитывала выборку
# сообщений → «event loop lag» до 14 с, нажатия не обрабатывались. Теперь —
# не больше _REBUILD_CONCURRENCY одновременно (остальные ждут), при очереди
# больше _REBUILD_MAX_QUEUED новая пропускается (запустится на следующем
# сообщении этой переписки), а работа с базой — в отдельном потоке.
_REBUILD_CONCURRENCY = 2
_REBUILD_MAX_QUEUED = 10
_rebuild_slots = asyncio.Semaphore(_REBUILD_CONCURRENCY)
_rebuild_checking: set[int] = set()  # переписки, по которым сейчас считаем, нужна ли пересборка

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


# ── Индикатор ожидания LLM-генерации (indeterminate прогресс-бар) ────────────
# Настоящий процент готовности ответа LLM заранее не известен (это не загрузка
# файла известного размера) — бар нарочно ЗАЦИКЛЕННЫЙ, не изображает честный
# прогресс. Общий для трёх LLM-флоу: «Ответ с CueMe» (оба внутренних пути —
# _run_variants_generation для выбранного контакта и _run_live_coach_step для
# холодного старта «Живого диалога»), «Анализ собеседника» (_run_deep_analysis),
# «Идеальное свидание» (_run_ideal_date).
#
# «Ответ с CueMe» — единственный флоу с двумя РАЗНЫМИ режимами показа:
# - первая генерация в диалоге (выбрал контакт / начал новый диалог) —
#   прогресс-бар, тот же 3-секундный круг, что и у остальных двух флоу
#   (_REPLY_PROGRESS_CYCLE_SECONDS == _PROGRESS_CYCLE_SECONDS, см.
#   with_progress_animation) — раньше был короче отдельно (5с вместо 10с),
#   но после общего сокращения до 3с разница пропала, константы оставлены
#   раздельными на случай, если разница понадобится снова.
# - «Другие варианты» БЕЗ выхода из диалога — вообще БЕЗ прогресс-бара,
#   результат сразу по готовности LLM, без ожидания цикла (см.
#   _run_variants_generation/_run_live_coach_step: force_fresh ветка).
# «Анализ собеседника»/«Идеальное свидание» — единый 3-секундный цикл на
# каждый вызов, включая «Обновить»/«Другая идея».

_PROGRESS_FRAMES = [
    "⏳ Генерирую ▓░░░░░░░░░",
    "⏳ Генерирую ▓▓░░░░░░░░",
    "⏳ Генерирую ▓▓▓░░░░░░░",
    "⏳ Генерирую ░▓▓▓░░░░░░",
    "⏳ Генерирую ░░▓▓▓░░░░░",
    "⏳ Генерирую ░░░▓▓▓░░░░",
    "⏳ Генерирую ░░░░▓▓▓░░░",
    "⏳ Генерирую ░░░░░▓▓▓░░",
    "⏳ Генерирую ░░░░░░▓▓▓░",
    "⏳ Генерирую ░░░░░░░▓▓▓",
]
_PROGRESS_CYCLE_SECONDS = 3.0  # Анализ собеседника, Идеальное свидание
_REPLY_PROGRESS_CYCLE_SECONDS = 3.0  # Ответ с CueMe, первая генерация в диалоге
_PROGRESS_FRAME_COUNT = 10  # кадров в одном круге — при 3с круге кадр идёт каждые 0.3с


# Было: _run_progress_cycle + with_progress_animation, отдававшая результат
# ТОЛЬКО на границе целого круга (cycle_seconds), с правкой кадра каждые
# cycle_seconds/10 = 0.3 с через await. Каждая правка — сетевой запрос к
# Telegram, поэтому «3-секундный» круг на деле длился дольше (замер: при
# правке за 0.25 с ответ LLM за 0.8 с показывался через 5.7 с), а частые
# правки упираются в лимиты Telegram. Заменено 2026-10-09 (см. ниже).
# Оставлено для отката.
# async def _run_progress_cycle(bot: Bot, chat_id: int, message_id: int, cycle_seconds: float) -> None:
#     """Один полный круг анимации ровно cycle_seconds секунд, _PROGRESS_FRAME_COUNT
#     кадров равномерно внутри него — короче круг, быстрее сменяются кадры."""
#     frame_interval = cycle_seconds / _PROGRESS_FRAME_COUNT
#     for frame in _PROGRESS_FRAMES[:_PROGRESS_FRAME_COUNT]:
#         try:
#             await bot.edit_message_text(frame, chat_id=chat_id, message_id=message_id)
#         except Exception:
#             pass  # гонки/rate limit одного кадра — не роняем задачу анимации
#         await asyncio.sleep(frame_interval)
#
#
# async def with_progress_animation(
#     bot: Bot, chat_id: int, message_id: int, coro, cycle_seconds: float = _PROGRESS_CYCLE_SECONDS,
# ):
#     """Крутит анимацию на message_id ЦЕЛЫМИ cycle_seconds-секундными кругами,
#     пока выполняется coro (реальный вызов LLM) — результат отдаётся ТОЛЬКО на
#     границе круга, никогда раньше. Если LLM ответила быстрее — юзер всё
#     равно видит анимацию весь текущий круг; если LLM не успела — крутим ещё
#     один круг целиком и снова проверяем на его границе. Исключение из coro
#     пробрасывается вызывающему коду сразу по завершении того круга, на
#     котором coro упала, — тот сам решает, как отредактировать message_id в
#     сообщение об ошибке."""
#     task = asyncio.ensure_future(coro)
#     while True:
#         await _run_progress_cycle(bot, chat_id, message_id, cycle_seconds)
#         if task.done():
#             return task.result()  # исключение из coro пробрасывается здесь же


# Было 1.2 с; теперь константа в config.py (PROGRESS_MIN_SHOW_SECONDS, 0.8 с).
_PROGRESS_MIN_SHOW_SECONDS = PROGRESS_MIN_SHOW_SECONDS  # не раньше — иначе анимация успевает только мигнуть
_PROGRESS_MIN_EDIT_INTERVAL = 0.8  # не чаще раза в столько секунд (лимиты Telegram)


async def with_progress_animation(
    bot: Bot, chat_id: int, message_id: int, coro, cycle_seconds: float = _PROGRESS_CYCLE_SECONDS,
):
    """Крутит анимацию на message_id, пока выполняется coro (реальный вызов
    LLM), и отдаёт результат СРАЗУ, как только coro готова — но не раньше
    _PROGRESS_MIN_SHOW_SECONDS от старта. Кадр меняется раз в
    max(cycle_seconds / кадров, _PROGRESS_MIN_EDIT_INTERVAL) секунд (то есть
    cycle_seconds задаёт уже только скорость смены кадров), правка идёт в
    фоне и не задерживает ожидание; новая не стартует, пока не закончилась
    предыдущая. Перед возвратом незаконченная правка дожидается (не дольше
    секунды), чтобы запоздавший кадр не перезаписал уже показанный
    результат. Исключение из coro пробрасывается вызывающему коду."""
    task = asyncio.ensure_future(coro)
    started = time.monotonic()
    interval = max(cycle_seconds / _PROGRESS_FRAME_COUNT, _PROGRESS_MIN_EDIT_INTERVAL)
    frame_i = 0
    edit_task: asyncio.Task | None = None

    async def _edit(frame: str) -> None:
        try:
            await bot.edit_message_text(frame, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass  # гонки/лимит одного кадра — не роняем анимацию

    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                remaining = _PROGRESS_MIN_SHOW_SECONDS - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                return task.result()
            if edit_task is None or edit_task.done():
                frame_i = (frame_i + 1) % _PROGRESS_FRAME_COUNT
                edit_task = asyncio.create_task(_edit(_PROGRESS_FRAMES[frame_i]))
    finally:
        if edit_task is not None and not edit_task.done():
            try:
                await asyncio.wait_for(edit_task, timeout=1.0)
            except Exception:
                pass


# ВРЕМЕННЫЕ замеры скорости «Ответа с CueMe» (2026-10-09, убрать после
# анализа): _timed_llm меряет чистое время LLM, _log_reply_timing пишет
# одну строку REPLY-TIMING на генерацию — LLM, до показа результата, и
# сколько сверху добавила анимация (overhead).
async def _timed_llm(coro, box: list):
    t0 = time.monotonic()
    try:
        return await coro
    finally:
        box.append(time.monotonic() - t0)


def _log_reply_timing(kind: str, started: float, llm_box: list) -> None:
    shown = time.monotonic() - started
    llm = llm_box[0] if llm_box else 0.0
    logging.info("REPLY-TIMING kind=%s llm=%.2fs shown=%.2fs overhead=%.2fs", kind, llm, shown, shown - llm)


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


async def _finish_generation_message(
    progress_msg: Message | None, target: Message, force_fresh: bool, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None,
) -> None:
    """Единая точка «куда девать результат/ошибку» для _run_variants_generation
    и _run_live_coach_step («Ответ с CueMe»): progress_msg — крутилась
    анимация, редактируем ЕЁ; иначе force_fresh — «Другие варианты» без
    анимации, редактируем target (call.message с прошлыми вариантами) на
    месте; иначе — кэш-хит первой генерации, новое сообщение (target тут
    часто НЕ бот-владеемое — сообщение юзера, редактировать нельзя)."""
    if progress_msg:
        await _edit_or_answer_long(progress_msg, text, reply_markup=reply_markup, parse_mode=parse_mode)
    elif force_fresh:
        await _edit_or_answer_long(target, text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await _answer_long(target, text, reply_markup=reply_markup, parse_mode=parse_mode)
