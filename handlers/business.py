"""Telegram Business API: подключение Автоматизации чатов и приём живого
потока сообщений (сохранение в business_messages, авто-создание контакта,
сопоставление исходящих с показанными подсказками CueMe, триггер пересборки
карточек по накоплению).

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
import asyncio
import difflib
import logging
import re
import string
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BusinessConnection, Message, ReplyKeyboardRemove

from handlers.common import _chat_ref, _message_text, _msg_meta
from handlers.onboarding import _maybe_prompt_gender, _maybe_prompt_source
from handlers.referral import _credit_referral_if_pending
from services.cards import _maybe_rebuild, _refresh_samples, _should_refresh_samples
from storage import (
    find_contact_by_original_id,
    get_business_connection,
    get_or_create_contact,
    get_recent_unmatched_suggestions,
    mark_bot_blocked,
    mark_suggestion_matched,
    save_business_message,
    update_contact_username,
    upsert_business_connection,
    upsert_chat_ref_mapping,
    upsert_user,
)

router = Router(name="business")


# ── Business API ──────────────────────────────────────────────────────────────

@router.business_connection()
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


@router.business_message()
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
