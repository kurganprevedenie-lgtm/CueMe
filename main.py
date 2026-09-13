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




# main_kb() (persistent reply-клавиатура) убрана совсем по запросу — главное
# меню теперь то же самое, что «Подписка»: одно сообщение с inline-кнопками,
# которое редактируется при переходах, а не нижняя панель. Не удалена
# физически — на случай отката (но ReplyKeyboardRemove в местах, которые
# раньше её отправляли, теперь активно снимает эту клавиатуру у тех, у кого
# она ещё видна с более ранней версии бота — см. handle_business_connection/
# handle_document ниже).
# def main_kb() -> ReplyKeyboardMarkup:
#     b = ReplyKeyboardBuilder()
#     b.row(KeyboardButton(text=BTN_UNIFIED))
#     b.row(KeyboardButton(text=BTN_DEEP), KeyboardButton(text=BTN_DATE))
#     b.row(KeyboardButton(text=BTN_SUBSCRIPTION))
#     b.row(KeyboardButton(text=BTN_SUPPORT))
#     return b.as_markup(resize_keyboard=True)



@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await _send_main_menu(message)


@dp.callback_query(F.data.startswith("mm:"))
async def cb_main_menu_action(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«unified»/«deep»/«date» — одноразовые действия (новое сообщение/
    FSM-флоу), само сообщение главного меню не трогают. «support» — как
    «👑 Подписка» (см. main_menu_kb): вложенный edit-экран, «⬅️ Назад»
    (help_kb → callback_data="sub:to_menu") редактирует обратно в меню."""
    action = call.data.split(":", 1)[1]
    await call.answer()
    await state.clear()
    telegram_id = str(call.from_user.id)
    if action == "unified":
        await _start_unified_reply(call.message, state)
    elif action == "deep":
        await _show_deep_analysis(call.message, bot, telegram_id, edit=True)
    elif action == "date":
        await _show_ideal_date(call.message, bot, telegram_id, edit=True)
    elif action == "support":
        await _show_help(call.message, edit=True)



@dp.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(call: CallbackQuery) -> None:
    """Не редактирует сообщение с результатом (его контент остаётся в
    истории чата нетронутым) — присылает главное меню НОВЫМ сообщением,
    тем же способом, что /menu."""
    await call.answer()
    await _send_main_menu(call.message)


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
            await _finish_onboarding_chain(bot, call.message.chat.id, call.message.message_id)




dp.message.outer_middleware(GenderGateMiddleware())
dp.callback_query.outer_middleware(GenderGateMiddleware())






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
            # reply_markup=ReplyKeyboardRemove() — main_kb() (persistent
            # reply-клавиатура) убрана совсем, эта отправка на всякий случай
            # снимает её, если у юзера она ещё видна с более ранней версии
            # бота. Дальше вопрос про источник уходит ОТДЕЛЬНЫМ сообщением
            # (_maybe_prompt_source ниже) — а вот всё ПОСЛЕ него (источник →
            # пол → квикстарт → «кому бы написал») остаётся правками одного
            # и того же сообщения.
            await bot.send_message(
                event.user.id,
                "✅ Готово, бот подключён! CueMe готов помогать тебе в переписках )",
                reply_markup=ReplyKeyboardRemove(),
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
    photo_file_id: str | None = None,
) -> int | None:
    """Синхронная DB-часть обработки business-сообщения: сохранение + резолв контакта
    + троттлинг refresh + сопоставление с подсказками CueMe (для исходящих).
    Возвращает contact_id для пересборки (или None). Выполняется в
    asyncio.to_thread, чтобы не блокировать event loop на живом потоке."""
    message_id = save_business_message(
        connection_id=conn_id, owner_user_id=owner_id, chat_ref=chat_ref,
        direction=direction, text=text, date=date, tg_message_id=tg_message_id,
        raw_meta=_msg_meta(text, is_voice), photo_file_id=photo_file_id,
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
    # Самое большое разрешение — для просмотра в /export (см. tools/export.py).
    # event.caption уже попал в text через _message_text выше, если было.
    photo_file_id = event.photo[-1].file_id if event.photo else None

    # Синхронную DB-часть уводим в поток, чтобы не блокировать event loop.
    contact_id_for_rebuild = await asyncio.to_thread(
        _persist_business_message,
        conn_id=conn_id, owner_id=owner_id, chat_ref=chat_ref, direction=direction,
        text=text, is_voice=is_voice, date=date, tg_message_id=event.message_id,
        contact_tg_id=str(event.chat.id),
        chat_first_name=event.chat.first_name, chat_last_name=event.chat.last_name,
        chat_username=getattr(event.chat, "username", None),
        sender_username=event.from_user.username if event.from_user else None,
        photo_file_id=photo_file_id,
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
    elif message.text == BTN_SUPPORT:
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


_PREMIUM_SOURCE_LABELS = {
    "referral": "реферал", "promo_channel": "промо-канал",
    "stars": "Stars", "tribute": "Tribute", "нет": "—",
}


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
        # раз, а источник/срок доопределяем через _premium_expiry_info — та
        # же функция, что строит текст под кнопкой «Подписка» (раньше тут
        # был отдельный, более бедный расчёт: не проверял Stars вообще и всё,
        # что не реферал/промо-канал, молча относил к Tribute).
        is_premium_now = await _is_premium(bot, tid)
        premium_until: datetime | None = None
        premium_is_subscription = False
        if is_premium_now:
            premium_source, premium_until, premium_is_subscription = _premium_expiry_info(tid)
            totals["premium_now"] += 1
            if premium_source == "referral":
                totals["with_ref_premium"] += 1
        else:
            premium_source = "нет"

        premium_remaining = (
            _format_remaining(premium_until - now) if premium_until else ""
        )
        premium_line = ""
        if is_premium_now:
            label = _PREMIUM_SOURCE_LABELS.get(premium_source, premium_source)
            if premium_until:
                suffix = " (автопродление)" if premium_is_subscription else ""
                premium_line = (
                    f"\n    👑 Premium: {label}{suffix} до "
                    f"{premium_until.strftime('%d.%m %H:%M')} UTC (осталось {premium_remaining})"
                )
            else:
                premium_line = f"\n    👑 Premium: {label} (дата окончания неизвестна)"

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
            "premium_until": premium_until.isoformat() if premium_until else "",
            "premium_remaining": premium_remaining,
            "premium_auto_renew": premium_is_subscription if is_premium_now else "",
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
    "is_premium_now", "premium_source", "premium_until", "premium_remaining",
    "premium_auto_renew", "trial_used", "referrals_made",
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

            # Фото (с подписью или без) — скачиваем реальные байты (раз на
            # file_id, а не на сообщение) и встраиваем прямо в .html base64
            # (to_html), а не отдельными файлами рядом — так .html остаётся
            # самостоятельным файлом, который не потеряет фото, если его
            # скопировать/переслать отдельно от остального архива. Сбой
            # скачивания ОДНОГО фото не должен рушить весь экспорт — просто
            # останется плейсхолдер «📷 Фото» в .html для этого сообщения.
            photo_data: dict[str, bytes] = {}
            file_ids = {m["photo_file_id"] for m in export["messages"] if m.get("photo_file_id")}
            for file_id in file_ids:
                try:
                    photo_buf = await bot.download(file_id)
                except Exception:
                    logging.warning("export: не удалось скачать фото file_id=%s", file_id)
                    continue
                photo_data[file_id] = photo_buf.read()

            zf.writestr(f"{safe_name}.json", json.dumps(export, ensure_ascii=False, indent=2))
            zf.writestr(f"{safe_name}.txt", to_text(export))
            zf.writestr(f"{safe_name}.html", to_html(export, photo_data))
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
# Текст/форвард → определение контакта → существующий пайплайн: ReplyHelp
# для выбранного контакта, LiveDialogue для нового. Фото (скриншот
# переписки) как вход убран вместе с функцией «скриншот → ответ» — см.
# пометку у handle_unified_input и у секции «Ответить по скриншоту» ниже.
#
# Вся фаза настройки (запрос текста → выбор/имя контакта → статус
# генерации) — ОДНО редактируемое сообщение, а не серия новых: message_id
# первого сообщения сохраняется в FSM (setup_chat_id/setup_message_id) и
# переиспользуется через bot.edit_message_text в handle_unified_input/
# handle_unified_name (это обычные message-хендлеры, не callback — своего
# "call.message" для edit у них нет). cb_unified_contact — callback на этом
# же сообщении, там подходит обычный call.message.edit_text. Итоговое
# сообщение с вариантами ответа (после настройки) — НОВОЕ, самостоятельное,
# см. _run_variants_generation/_run_live_coach_step.

async def _start_unified_reply(message: Message, state: FSMContext) -> None:
    await state.set_state(UnifiedReply.waiting_for_input)
    sent = await message.answer("Перешли сообщение или просто вставь текст переписки")
    await state.update_data(setup_chat_id=sent.chat.id, setup_message_id=sent.message_id)


def unified_contacts_kb(contacts: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        b.button(text=_contact_name(c), callback_data=f"unified_contact:{c['id']}")
    b.button(text="➕ Другой человек", callback_data="unified_contact:new")
    b.adjust(1)
    return b.as_markup()


async def _edit_setup_message(
    state: FSMContext, bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует сообщение фазы настройки «Ответ с CueMe» (setup_chat_id/
    setup_message_id из FSM, см. _start_unified_reply) — общий хелпер для
    handle_unified_input/handle_unified_name, которые получают обычное
    message-событие (не callback), так что своего call.message для edit нет."""
    data = await state.get_data()
    chat_id, message_id = data.get("setup_chat_id"), data.get("setup_message_id")
    if chat_id is None or message_id is None:
        return  # не должно происходить — на всякий случай не роняем хендлер
    await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)


@dp.message(UnifiedReply.waiting_for_input, _not_command)
async def handle_unified_input(message: Message, state: FSMContext, bot: Bot) -> None:
    # Функция «скриншот переписки → ответ» убрана целиком (по запросу) —
    # фото в этом состоянии больше не читаем через Vision, просто просим
    # текст. Раньше здесь был branch на message.photo → extract_chat_from_image.
    if message.photo:
        await _edit_setup_message(
            state, bot, "Скриншоты сейчас не поддерживаются — перешли сообщение или вставь текст.",
        )
        return

    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await _edit_setup_message(state, bot, "Перешли сообщение или вставь текст.")
        return

    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    await state.update_data(pending_text=incoming)

    if not contacts:
        await state.set_state(UnifiedReply.waiting_for_name)
        await _edit_setup_message(
            state, bot,
            "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов.",
        )
        return

    await _edit_setup_message(state, bot, "Кому отвечаем?", reply_markup=unified_contacts_kb(contacts))


@dp.message(UnifiedReply.waiting_for_name)
async def handle_unified_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = (message.text or "").strip()
    if not name:
        await _edit_setup_message(state, bot, "Пришли имя текстом.")
        return

    data = await state.get_data()
    pending_text = data.get("pending_text")
    if not pending_text:
        await _edit_setup_message(state, bot, "Контекст устарел — начни заново через «💬 Ответ с CueMe».")
        await state.clear()
        return

    telegram_id = str(message.from_user.id)
    upsert_user(telegram_id, f"user{telegram_id}")
    contact_id = get_or_create_contact(telegram_id, f"live_{uuid.uuid4().hex}", name)

    await state.set_state(LiveDialogue.waiting_for_incoming)
    await state.update_data(contact_id=contact_id, dialogue_history=[])
    # "Готово — «name»." — часть той же редактируемой фазы настройки, не
    # отдельное сообщение (см. _start_unified_reply). Итог с вариантами
    # ответа дальше в _process_live_incoming — уже НОВОЕ сообщение.
    await _edit_setup_message(state, bot, f"Готово — «{name}». Генерирую варианты...")
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
        await _process_reply_incoming(call.message, state, bot, pending_text, call.from_user.id, edit=True)
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


# _last_incoming_line — использовалась только функцией «скриншот переписки →
# ответ» (распознанный OCR-текст скриншота, приближение последней реплики
# собеседника). Убрана вместе с ней, оставлена закомментированной на случай
# отката (см. секцию «Ответить по скриншоту» выше).
# def _last_incoming_line(chat_text: str) -> str:
#     """Последняя непустая строка распознанной переписки — приближение последней
#     реплики собеседника для ситуативной эвристики (скриншот/OCR). Если OCR
#     сохранил роли, пропускаем строки автора («Я: ...») и берём последнюю чужую."""
#     lines = [line.strip() for line in (chat_text or "").splitlines() if line.strip()]
#     if not lines:
#         return ""
#     self_re = re.compile(r"^(я|me|you)\s*[:：-]", re.IGNORECASE)
#     other_re = re.compile(r"^(собеседник|он|она|они|контакт|не я)\s*[:：-]", re.IGNORECASE)
#
#     for s in reversed(lines):
#         if other_re.match(s):
#             return s
#     for s in reversed(lines):
#         if not self_re.match(s):
#             return s
#     return lines[-1]


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
    по одному тапу, без отдельной кнопки «Скопировать» на каждый вариант.
    Вступительная строка «Вот несколько вариантов...» убрана по запросу —
    варианты идут сразу; инсайт/инструкция про продолжение сессии теперь
    отдельным блоком снизу, см. _variants_footer."""
    blocks = []
    for i, (name, text) in enumerate(variants):
        letter = _VARIANT_LETTERS[i] if i < len(_VARIANT_LETTERS) else str(i + 1)
        blocks.append(
            f"<b>Вариант {letter}: {html.escape(name)}</b>\n"
            f"<code>{html.escape(text)}</code>"
        )
    return "\n\n".join(blocks)


def _variants_footer(insight: str | None, continuation: str) -> str:
    """Цитата (blockquote) внизу сообщения с вариантами — инсайт про
    собеседника (если есть — только у «живого» диалога, см. _run_live_coach_step)
    и инструкция про продолжение сессии. Строится ОДИН раз при первой
    генерации и сохраняется в ctx["footer_html"] — «Другие варианты»
    (force_fresh) переиспользует её как есть, меняются только сами варианты."""
    parts = []
    if insight:
        parts.append(html.escape(insight))
    parts.append(html.escape(continuation))
    return "<blockquote>" + "\n\n".join(parts) + "</blockquote>"


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
# "screenshot" убран вместе с функцией «скриншот переписки → ответ» (см.
# закомментированную секцию «Ответить по скриншоту» выше).
_VARIANT_KINDS = ("reply",)


def variants_result_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"varregen:{action_id}")
    return _with_back_to_menu(b.as_markup())


async def _run_variants_generation(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str,
    state: FSMContext | None = None, force_fresh: bool = False,
) -> None:
    """Общий шаг генерации нескольких именованных вариантов ОДНИМ вызовом LLM
    для «Ответ с CueMe» (kind всегда "reply" — kind="screenshot" убран вместе
    с функцией «скриншот переписки → ответ»). Гейт и списание триала — один
    раз за вызов (не за каждый вариант), т.к. это один вызов LLM."""
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
            # kind всегда "reply" — "screenshot" убран вместе с функцией
            # «скриншот переписки → ответ» (screenshot_variants в llm.py
            # остался нетронутым, просто больше никем не вызывается).
            variants = await suggest_reply_variants(
                text, style_card, interaction_card,
                data_signals=signals, previous_variants=prev, winning_examples=winning,
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

    # footer_html строится ОДИН раз (при первой генерации) и живёт в ctx —
    # «Другие варианты» (force_fresh) переиспользует его как есть, см.
    # _variants_footer. Результат — самостоятельное сообщение (не edit фазы
    # настройки, см. _start_unified_reply): первый раз answer (новое),
    # «Другие варианты» — edit того же результата.
    footer = ctx.get("footer_html")
    if footer is None:
        contact_gen, _ = _contact_words(gender)
        continuation = (
            f"Пришли следующее сообщение {contact_gen}, чтобы ответить и на него. "
            "Чтобы выйти из режима — нажми любую кнопку меню."
        )
        footer = _variants_footer(None, continuation)
        ctx["footer_html"] = footer

    text_out = f"{_format_variants(variants)}\n\n{footer}"
    if force_fresh:
        await _edit_or_answer_long(
            target, text_out, reply_markup=variants_result_kb(action_id), parse_mode="HTML",
        )
    else:
        await _answer_long(
            target, text_out, reply_markup=variants_result_kb(action_id), parse_mode="HTML",
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
    edit: bool = False,
) -> None:
    """Общий хвост «Ответить за меня»: сборка ctx и генерация вариантов.
    user_id — ОТДЕЛЬНЫМ параметром (не message.from_user.id) — при вызове
    из callback-контекста message может быть call.message, чей .from_user
    это бот, не юзер (стандартная ловушка aiogram). edit=True — только из
    cb_unified_contact (message это call.message, только что отредактированное
    "Обрабатываю сообщение от {name}..." — сообщение БОТА): если триал уже
    исчерпан, пейволл редактирует его же, а не шлёт новое. edit=False (из
    handle_incoming, по умолчанию) — message это реальное пересланное
    сообщение от юзера, редактировать его нельзя."""
    telegram_id = str(user_id)
    data = await state.get_data()
    # Состояние НЕ сбрасываем — иначе следующее сообщение улетит в общий
    # авто-режим («Переписать») вместо продолжения «Ответить за меня».
    # Выйти из режима — любая кнопка меню (handle_menu_button сбрасывает state).

    contact_id = data.get("contact_id")
    if not await _quota_gate(bot, message, telegram_id, edit=edit):
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
    return _with_back_to_menu(b.as_markup())


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
        # footer_html уже посчитан при первой генерации (см. конец функции) и
        # хранится в ctx — «Другие варианты» его не пересчитывает, инсайт и
        # инструкция остаются как были, меняются только сами варианты. Editим
        # ЭТО ЖЕ сообщение (target = call.message из cb_live_regen), а не
        # шлём новое.
        footer = ctx.get("footer_html", "")
        await _edit_or_answer_long(
            target, f"{_format_variants(variants)}\n\n{footer}",
            reply_markup=live_variants_kb(action_id), parse_mode="HTML",
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

    # Инсайт («Что я уже понял») и инструкция про продолжение сессии — ОДНОЙ
    # цитатой внизу итогового сообщения (не отдельными message), см.
    # _variants_footer. footer_html сохраняется в ctx — «Другие варианты»
    # (force_fresh, ветка выше) переиспользует его без изменений.
    message_count = ctx.get("message_count", 0)
    insight = None
    if updated_notes and (message_count == 1 or message_count % LIVE_NOTES_SUMMARY_EVERY == 0):
        preview = _running_notes_preview(updated_notes)
        if preview:
            insight = f"Что я уже понял:\n{preview}"

    contact_gen, _ = _contact_words(gender)
    continuation = (
        f"Пришли следующее сообщение {contact_gen} — отвечу и на него. "
        "Чтобы выйти из режима — нажми любую кнопку меню."
    )
    footer = _variants_footer(insight, continuation)
    ctx["footer_html"] = footer

    # Итог — САМОСТОЯТЕЛЬНОЕ сообщение (не edit фазы настройки, см.
    # _start_unified_reply/_edit_setup_message): answer шлёт НОВОЕ.
    await _answer_long(
        target, f"{_format_variants(variants)}\n\n{footer}",
        reply_markup=live_variants_kb(action_id), parse_mode="HTML",
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


# ── 📸 Ответить по скриншоту — УБРАНА ЦЕЛИКОМ по запросу пользователя ────────
# Отдельная команда /screenshot (в дополнение к общей точке входа «💬 Ответ
# с CueMe», которая тоже больше не принимает фото — см. handle_unified_input
# выше). Закомментирована целиком, не удалена физически — на случай отката.
# Использовала kind="screenshot" в _run_variants_generation (screenshot_variants
# в llm.py) — та ветка тоже закомментирована ниже, вместе с Screenshot(FSM).

# async def _start_screenshot(message: Message, state: FSMContext) -> None:
#     telegram_id = str(message.from_user.id)
#     if not list_contacts(telegram_id):
#         await _send_no_contacts_hint(message)
#         return
#     await state.set_state(Screenshot.waiting_for_image)
#     await message.answer("Пришли скриншот переписки (или вставь текст диалога), на который нужно ответить:")
#
#
# @dp.message(Command("screenshot"))
# async def cmd_screenshot(message: Message, state: FSMContext) -> None:
#     await _start_screenshot(message, state)
#
#
# @dp.message(Screenshot.waiting_for_image, F.photo)
# async def handle_screenshot_photo(message: Message, state: FSMContext, bot: Bot) -> None:
#     await message.answer("Читаю скриншот...")
#     try:
#         buf = await bot.download(message.photo[-1])
#         chat_text = await extract_chat_from_image(buf.read())
#     except Exception:
#         logging.exception("screenshot: не удалось скачать/распознать")
#         chat_text = ""
#
#     if not chat_text or chat_text.strip() == ILLEGIBLE_MARKER:
#         await message.answer("Не смог прочитать скриншот — пришли текст переписки сообщением.")
#         return  # остаёмся в Screenshot.waiting_for_image
#
#     await _proceed_screenshot_style_pick(message, state, chat_text)
#
#
# @dp.message(Screenshot.waiting_for_image, F.text)
# async def handle_screenshot_text(message: Message, state: FSMContext) -> None:
#     chat_text = (message.text or "").strip()
#     if not chat_text:
#         await message.answer("Пришли скриншот или текст переписки.")
#         return
#     await _proceed_screenshot_style_pick(message, state, chat_text)
#
#
# def screenshot_contact_pick_kb(contacts: list, action_id: str) -> InlineKeyboardMarkup:
#     """Как contacts_kb, но с кнопкой для человека, которого ещё нет в базе —
#     для него используется общий (агрегатный) стиль, без interaction_card."""
#     b = InlineKeyboardBuilder()
#     for c in contacts:
#         b.button(text=_contact_name(c), callback_data=f"shotcontact:{c['id']}:{action_id}")
#     b.button(text="🆕 Новый человек (нет в базе)", callback_data=f"shotcontact:new:{action_id}")
#     b.adjust(1)
#     return b.as_markup()
#
#
# async def _proceed_screenshot_style_pick(message: Message, state: FSMContext, chat_text: str) -> None:
#     await state.clear()
#     telegram_id = str(message.from_user.id)
#     contacts = list_contacts(telegram_id)
#
#     action_id = _new_action(message.from_user.id, {"kind": "screenshot_pending", "chat_text": chat_text})
#     await message.answer("Чья это переписка?", reply_markup=screenshot_contact_pick_kb(contacts, action_id))
#
#
# @dp.callback_query(F.data.startswith("shotcontact:"))
# async def cb_screenshot_contact(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
#     parts = call.data.split(":")
#     if len(parts) != 3:
#         await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
#         return
#     _, raw_id, action_id = parts
#     telegram_id = str(call.from_user.id)
#
#     ctx = _get_action(call.from_user.id, action_id)
#     if not ctx or ctx.get("kind") != "screenshot_pending":
#         await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
#         return
#
#     if raw_id == "new":
#         await call.answer()
#         await _prompt_screenshot_style_no_contact(bot, call.message, call.from_user.id, telegram_id, ctx["chat_text"], state, edit=True)
#         return
#
#     contact_id = int(raw_id)
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         await call.answer("Контакт не найден.")
#         return
#
#     await call.answer()
#     await _prompt_screenshot_style(bot, call.message, call.from_user.id, telegram_id, contact_id, ctx["chat_text"], state, edit=True)
#
#
# async def _prompt_screenshot_style(
#     bot: Bot, target: Message, user_id: int, telegram_id: str, contact_id: int, chat_text: str,
#     state: FSMContext, edit: bool = False,
# ) -> None:
#     # ВАЖНО: user_id передаётся отдельным параметром, а не берётся из
#     # target.from_user — при edit=True target это call.message, чей
#     # .from_user это БОТ, а не пользователь (стандартная ловушка aiogram).
#     if not await _quota_gate(bot, target, telegram_id):
#         return
#     # Генерация карточек ходит в LLM — без обработки ошибок сбой (лимит/провайдер
#     # недоступен) тихо убивал кнопку: спиннер гас, а сообщение не менялось.
#     try:
#         style_card = await _style_for_rewrite(telegram_id, contact_id)
#         interaction_card = (await _gen_interaction_card(contact_id, telegram_id) or "") if style_card else ""
#     except RateLimitError:
#         await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
#         return
#     except Exception:
#         logging.exception("screenshot: не удалось сгенерировать карточки")
#         await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
#         return
#     if not style_card:
#         text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#
#     samples = get_message_samples(contact_id)
#     ctx = {
#         "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
#         "contact_id": contact_id,
#         "style_card": style_card, "interaction_card": interaction_card,
#         "data_signals": _reply_data_signals(samples, _last_incoming_line(chat_text)),
#         "winning": _winning_for_contact(telegram_id, contact_id),
#     }
#     action_id = _new_action(user_id, ctx)
#     if edit:
#         await target.edit_text("Генерирую варианты...")
#     else:
#         await target.answer("Генерирую варианты...")
#     await _run_variants_generation(target, ctx, user_id, bot, action_id, state)
#
#
# async def _prompt_screenshot_style_no_contact(
#     bot: Bot, target: Message, user_id: int, telegram_id: str, chat_text: str,
#     state: FSMContext, edit: bool = False,
# ) -> None:
#     """Для человека, которого ещё нет в базе — общий (агрегатный) стиль автора,
#     без per-contact interaction_card (промпт сам подставит нейтральный фолбэк)."""
#     if not await _quota_gate(bot, target, telegram_id):
#         return
#     try:
#         style_card = await _gen_style_card(telegram_id)
#     except RateLimitError:
#         await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
#         return
#     except Exception:
#         logging.exception("screenshot(new): не удалось сгенерировать стиль")
#         await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
#         return
#     if not style_card:
#         text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#
#     ctx = {
#         "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
#         "style_card": style_card, "interaction_card": "",
#         "data_signals": _reply_data_signals(None, _last_incoming_line(chat_text)),
#     }
#     action_id = _new_action(user_id, ctx)
#     if edit:
#         await target.edit_text("Генерирую варианты...")
#     else:
#         await target.answer("Генерирую варианты...")
#     await _run_variants_generation(target, ctx, user_id, bot, action_id, state)


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



    return "tribute", None, False

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



# ── Роутеры вынесенных модулей ───────────────────────────────────────────────
# Порядок include_router = порядок резолва апдейтов после хендлеров самого dp
# (см. комментарий в конце файла о сохранении исходного порядка).
dp.include_router(payments_router)
dp.include_router(referral_router)
dp.include_router(subscription_router)
dp.include_router(analysis_router)
dp.include_router(date_ideas_router)
dp.include_router(support_router)

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
