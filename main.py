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
    CallbackQuery, ChatMemberUpdated, CopyTextButton, Document, ErrorEvent, FSInputFile,
    InputRichMessage, LinkPreviewOptions, Message,
    InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
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
    live_coach_step,
    make_features_summary,
    sample_texts,
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
    get_promo_channel_pause,
    get_users_with_active_promo_premium,
    pause_promo_channel_premium,
    resume_promo_channel_premium,
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

from handlers.common import (
    BTN_DATE,
    BTN_DEEP,
    BTN_HELP,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_UNIFIED,
    GenderGateMiddleware,
    LiveDialogue,
    ReplyHelp,
    Setup,
    TELEGRAM_MAX_LEN,
    UnifiedReply,
    _ACTION_TTL_SEC,
    _ALL_BTNS,
    _BACK_TO_MENU_BUTTON,
    _EMOJI_RE,
    _GENDER_LABELS,
    _GENDER_PROMPT_TEXT,
    _RU_MONTHS_GEN,
    _action_seq,
    _answer_long,
    _charge_trial_if_needed,
    _chat_ref,
    _contact_name,
    _contact_words,
    _edit_or_answer_long,
    _format_remaining,
    _format_until,
    _get_action,
    _has_promo_channel_premium,
    _has_quota,
    _has_referral_premium,
    _has_stars_premium,
    _is_admin,
    _is_premium,
    _last_action,
    _message_text,
    _msg_meta,
    _new_action,
    _no_contacts_kb,
    _not_command,
    _premium_cache,
    _premium_expiry_info,
    _quota_gate,
    _require_premium,
    _ru_days_word,
    _send_no_contacts_hint,
    _send_paywall,
    _split_long_text,
    _style_cache_key,
    _with_back_to_menu,
    contacts_kb,
    gender_kb,
    paywall_kb,
    premium_menu_kb,
    stars_tariff_kb,
)
from services.cards import (
    _gen_interaction_card,
    _gen_my_style_per_contact,
    _gen_style_card,
    _get_rebuild_sample,
    _maybe_rebuild,
    _quick_stats,
    _rebuild_contact,
    _refresh_samples,
    _should_refresh_samples,
)
from handlers.analysis import (
    DEEP_ANALYSIS_MIN_MSGS,
    _gen_deep_analysis,
    _run_deep_analysis,
    _show_deep_analysis,
    router as analysis_router,
)
from handlers.date_ideas import (
    _run_ideal_date,
    _show_ideal_date,
    router as date_ideas_router,
)
from handlers.referral import (
    _credit_referral_if_pending,
    _invite_text,
    _show_invite,
    invite_kb,
    router as referral_router,
)
from handlers.subscription import (
    _premium_status_text,
    _reconcile_promo_channel_premium,
    _show_premium_screen,
    payments_router,
    router as subscription_router,
)
from handlers.support import _show_help, router as support_router, support_kb
from handlers.reply_flow import (
    _pick_no_repeat,
    _process_live_incoming,
    _process_reply_incoming,
    _show_live_start,
    _show_revive,
    _start_live_dialogue,
    _start_reply,
    _start_unified_reply,
    _style_for_rewrite,
    router as reply_flow_router,
)
from handlers.onboarding import (
    _business_connect_text,
    _finish_onboarding_chain,
    _maybe_prompt_gender,
    _maybe_prompt_source,
    _send_business_connect_prompt,
    _send_start_menu,
    router as onboarding_router,
)
from handlers.business import _persist_business_message, router as business_router
from handlers.main_menu import router as main_menu_router
from handlers.admin import router as admin_router
from handlers.account import router as account_router

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




































dp.message.outer_middleware(GenderGateMiddleware())
dp.callback_query.outer_middleware(GenderGateMiddleware())























# ── Роутеры вынесенных модулей ───────────────────────────────────────────────
# Порядок include_router = порядок резолва апдейтов после хендлеров самого dp
# (см. комментарий в конце файла о сохранении исходного порядка).
dp.include_router(payments_router)
dp.include_router(referral_router)
dp.include_router(subscription_router)
dp.include_router(analysis_router)
dp.include_router(date_ideas_router)
dp.include_router(support_router)
dp.include_router(main_menu_router)
dp.include_router(admin_router)
dp.include_router(onboarding_router)
dp.include_router(business_router)
dp.include_router(reply_flow_router)
dp.include_router(account_router)

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
            "GROQ_API_KEY не задан — распознавание голоса пойдёт только "
            "через Gemini-fallback."
        )
    logging.info("Конфиг проверен. Доступные LLM-ключи: %s", ", ".join(present))


async def main() -> None:
    _validate_startup_config()
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start",       description="Начало работы"),
        BotCommand(command="menu",        description="Главное меню"),
        BotCommand(command="gender",      description="Сменить пол"),
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="connect",     description="Подключить Автоматизацию чатов"),
        BotCommand(command="me",          description="Мой стиль общения"),
        # BotCommand("screenshot", ...) убрана вместе с функцией «скриншот
        # переписки → ответ» — команда /screenshot закомментирована в коде.
        BotCommand(command="reply",       description="Помочь ответить собеседнику"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
        BotCommand(command="progress",    description="Прогресс накопления по контактам"),
        BotCommand(command="deep_analysis", description="Анализ собеседника"),
        BotCommand(command="premium",     description="Статус подписки"),
        BotCommand(command="delete",      description="Удалить свои данные"),
        BotCommand(command="rebuild",     description="Пересобрать все карточки"),
    ])
    asyncio.create_task(_reconcile_promo_channel_premium(bot))
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
            "chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
