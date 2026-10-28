"""Business API: поток сообщений, подсказки, исчезающие/изменённые/удалённые сообщения.

Вынесено из main.py без изменения логики (строки 5089–6314 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import difflib
import json
import logging
import re
import string
from datetime import datetime, timedelta, timezone
from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BufferedInputFile,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InputMediaDocument,
    Message,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    MessageEntity,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import CONNECT_EFFECT_ID
from storage import (
    find_contact_by_original_id,
    find_unlinked_manual_contact_by_username,
    get_business_connection,
    get_business_message_by_tg_id,
    get_business_message_by_row_id,
    create_deleted_reveal,
    get_deleted_reveal,
    mark_deleted_reveal_revealed,
    get_unrevealed_deleted_reveals,
    get_contact_id_for_chat_ref,
    get_recent_unmatched_suggestions,
    get_contact_by_id,
    mark_bot_blocked,
    merge_manual_contact_into,
    get_or_create_contact,
    mark_suggestion_matched,
    save_business_message,
    claim_saved_ephemeral,
    update_business_message_text,
    update_business_message_content,
    get_business_media_group,
    get_watch_setting,
    update_contact_username,
    upsert_business_connection,
    upsert_chat_ref_mapping,
    upsert_user,
)
from app_state import TELEGRAM_MAX_LEN, _contact_name, dp
from handlers.subscription import _is_premium
from handlers.referrals_gate import _credit_referral_if_pending
from handlers.profile import (
    _chat_ref,
    _maybe_prompt_gender,
    _maybe_prompt_source,
    _message_text,
    _msg_meta,
)
from handlers.cards import _maybe_rebuild, _refresh_samples, _should_refresh_samples
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── Business API ──────────────────────────────────────────────────────────────

@dp.business_connection()
async def handle_business_connection(event: BusinessConnection, bot: Bot) -> None:
    # Апдейт приходит не только при подключении, но и при смене прав —
    # подтверждение шлём только при переходе «не было активного подключения →
    # включено» (раньше слалось на каждый такой апдейт и дублировалось).
    prev = get_business_connection(event.id)
    just_connected = event.is_enabled and not (prev and prev["is_enabled"])
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
        # Было: то же сообщение без эффекта, на КАЖДЫЙ апдейт с is_enabled
        # (дублировалось при смене прав). Теперь — только при подключении и
        # с конфетти (CONNECT_EFFECT_ID); если эффект не принят — без него.
        # try:
        #     await bot.send_message(
        #         event.user.id,
        #         "✅ Готово, бот подключён! CueMe готов помогать тебе в переписках )",
        #         reply_markup=ReplyKeyboardRemove(),
        #     )
        # except TelegramForbiddenError:
        #     mark_bot_blocked(owner_id)
        #     return
        # except Exception:
        #     logging.warning("business-connect notify failed: owner=%s", event.user.id)
        # await asyncio.sleep(3)
        if just_connected:
            # reply_markup=ReplyKeyboardRemove() — main_kb() (persistent
            # reply-клавиатура) убрана совсем, эта отправка на всякий случай
            # снимает её, если у юзера она ещё видна с более ранней версии
            # бота. Дальше вопрос про источник уходит ОТДЕЛЬНЫМ сообщением
            # (_maybe_prompt_source ниже) — а вот всё ПОСЛЕ него (источник →
            # пол → квикстарт → «кому бы написал») остаётся правками одного
            # и того же сообщения.
            text = "✅ Готово, бот подключён! CueMe готов помогать тебе в переписках )"
            try:
                try:
                    await bot.send_message(
                        event.user.id, text, reply_markup=ReplyKeyboardRemove(),
                        message_effect_id=CONNECT_EFFECT_ID or None,
                    )
                except TelegramBadRequest as e:
                    logging.warning("business-connect: эффект не принят (%s), шлю без него", e)
                    await bot.send_message(event.user.id, text, reply_markup=ReplyKeyboardRemove())
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


# ── Полное описание business-сообщения любого типа ────────────────────────────
# Раньше сохранялись только текст и file_id четырёх медиа (фото/видео/кружок/
# голосовое) — стикер, файл, гифка, геопозиция, контакт, опрос и т.д. при
# удалении показывались как «текст недоступен». Теперь тип и метаданные
# пишутся в business_messages.content_type/media_json/entities_json (байты
# файлов не храним — только file_id, его хватает для повторной отправки).
# text по-прежнему только текст/подпись/расшифровка голоса — стикеры и
# файлы в анализ и message_samples не попадают.

_FILE_META_FIELDS = ("file_id", "file_unique_id", "file_size", "file_name", "mime_type", "duration", "width", "height")

# Типы, которые Telegram присылает, но содержимое которых бот повторно
# отправить не может (см. _send_deleted_content) — запоминаем хотя бы вид.
_OTHER_CONTENT_KINDS = ("story", "game", "paid_media", "checklist", "giveaway", "giveaway_winners", "invoice")


def _file_meta(obj, *extra: str) -> dict:
    out = {}
    for field in _FILE_META_FIELDS + extra:
        value = getattr(obj, field, None)
        if value is not None:
            out[field] = value
    return out


def _extract_business_content(msg: Message) -> dict:
    """Тип сообщения и метаданные для business_messages. Порядок проверок
    важен: у гифки Telegram заполняет и animation, и document; у места
    (venue) — и venue, и location."""
    media: dict | None = None
    if msg.sticker:
        content_type = "sticker"
        media = _file_meta(msg.sticker, "emoji", "set_name", "is_animated", "is_video", "type", "custom_emoji_id")
    elif msg.animation:
        content_type, media = "animation", _file_meta(msg.animation)
    elif msg.photo:
        content_type, media = "photo", _file_meta(msg.photo[-1])
    elif msg.video:
        content_type, media = "video", _file_meta(msg.video)
    elif msg.video_note:
        content_type, media = "video_note", _file_meta(msg.video_note, "length")
    elif msg.voice:
        content_type, media = "voice", _file_meta(msg.voice)
    elif msg.audio:
        content_type, media = "audio", _file_meta(msg.audio, "performer", "title")
    elif msg.document:
        content_type, media = "document", _file_meta(msg.document)
    elif msg.venue:
        v = msg.venue
        content_type = "venue"
        media = {"latitude": v.location.latitude, "longitude": v.location.longitude,
                 "title": v.title, "address": v.address}
        for field in ("foursquare_id", "foursquare_type", "google_place_id", "google_place_type"):
            if getattr(v, field, None):
                media[field] = getattr(v, field)
    elif msg.location:
        loc = msg.location
        content_type = "location"
        media = {"latitude": loc.latitude, "longitude": loc.longitude}
        for field in ("horizontal_accuracy", "live_period", "heading"):
            if getattr(loc, field, None) is not None:
                media[field] = getattr(loc, field)
    elif msg.contact:
        c = msg.contact
        content_type = "contact"
        media = {k: getattr(c, k) for k in ("phone_number", "first_name", "last_name", "user_id", "vcard")
                 if getattr(c, k, None) is not None}
    elif msg.poll:
        p = msg.poll
        content_type = "poll"
        media = {"question": p.question, "options": [o.text for o in p.options],
                 "type": p.type, "is_anonymous": p.is_anonymous,
                 "allows_multiple_answers": p.allows_multiple_answers}
    elif msg.dice:
        content_type, media = "dice", {"emoji": msg.dice.emoji, "value": msg.dice.value}
    elif msg.text:
        content_type = "text"
    else:
        content_type = "other"
        kind = next((k for k in _OTHER_CONTENT_KINDS if getattr(msg, k, None)), None)
        media = {"kind": kind} if kind else None
        if kind == "checklist":
            # Повторно отправить чек-лист бот не может (только от имени
            # бизнес-аккаунта), но показать текстом — может.
            cl = msg.checklist
            media.update({"title": cl.title, "tasks": [
                {"text": t.text, "done": t.completion_date is not None} for t in cl.tasks
            ]})

    if media is not None and content_type in ("photo", "video", "animation") and msg.has_media_spoiler:
        media["has_spoiler"] = True

    raw_entities = msg.entities or msg.caption_entities
    return {
        "content_type": content_type,
        "media": media,
        "entities": [e.model_dump(mode="json", exclude_none=True) for e in raw_entities] if raw_entities else None,
        "reply_to_tg_message_id": msg.reply_to_message.message_id if msg.reply_to_message else None,
        "media_group_id": msg.media_group_id,
        "forward_info": msg.forward_origin.model_dump(mode="json", exclude_none=True) if msg.forward_origin else None,
    }


def _persist_business_message(
    *, conn_id: str, owner_id: str, chat_ref: str, direction: str,
    text: str | None, is_voice: bool, date: str, tg_message_id: int,
    contact_tg_id: str, chat_first_name: str | None, chat_last_name: str | None,
    chat_username: str | None, sender_username: str | None,
    photo_file_id: str | None = None,
    video_file_id: str | None = None, video_note_file_id: str | None = None,
    voice_file_id: str | None = None,
    content: dict | None = None,
) -> tuple[int | None, tuple[str, str] | None]:
    """Синхронная DB-часть обработки business-сообщения: сохранение + резолв контакта
    + троттлинг refresh + сопоставление с подсказками CueMe (для исходящих).
    Возвращает (contact_id для пересборки, merge_notice) — merge_notice это
    (название ручного диалога, username), если этот контакт только что
    автоматически слился с ранее созданным вручную диалогом (см.
    merge_manual_contact_into), иначе None. Выполняется в asyncio.to_thread,
    чтобы не блокировать event loop на живом потоке."""
    message_id = save_business_message(
        connection_id=conn_id, owner_user_id=owner_id, chat_ref=chat_ref,
        direction=direction, text=text, date=date, tg_message_id=tg_message_id,
        raw_meta=_msg_meta(text, is_voice), photo_file_id=photo_file_id,
        video_file_id=video_file_id, video_note_file_id=video_note_file_id,
        voice_file_id=voice_file_id,
        **(content or {}),
    )
    if message_id is None:
        # Повторная доставка того же сообщения — не триггерим пересборку.
        logging.info(
            "business_message дубль пропущен: conn=%s chat_ref=%s msg_id=%s",
            conn_id, chat_ref, tg_message_id,
        )
        return None, None
    logging.info(
        "business_message saved: conn=%s chat_ref=%s direction=%s",
        conn_id, chat_ref, direction,
    )
    upsert_user(owner_id, f"user{owner_id}")

    # Для приватного чата contact_tg_id всегда равен ID собеседника
    if contact_tg_id == owner_id:
        return None, None  # edge-case: не создаём контакт «сам с собой»
    original_id = f"user{contact_tg_id}"

    contact_row = find_contact_by_original_id(owner_id, original_id)
    merge_notice: tuple[str, str] | None = None
    if not contact_row:
        # Контакт ещё не создан — создаём автоматически из данных чата
        display_name = " ".join(
            p for p in (chat_first_name or "", chat_last_name or "") if p
        ).strip()
        cid = get_or_create_contact(owner_id, original_id, display_name)
        if chat_username:
            update_contact_username(cid, chat_username)
            # Ручной диалог («💬 Ответ с CueMe») с тем же username, ещё без
            # реального telegram_id — автослияние: переносим накопленные
            # данные на этот только что созданный реальный контакт и
            # архивируем исходную запись (main._persist_business_message
            # вызывается один раз на новый контакт — при первом сообщении
            # именно от него, так что повторной проверки на дальнейших
            # сообщениях не нужно).
            manual = find_unlinked_manual_contact_by_username(owner_id, chat_username)
            if manual and manual["id"] != cid:
                merge_manual_contact_into(manual["id"], cid)
                merge_notice = (manual["display_name"] or "без названия", chat_username.lstrip("@"))
                logging.info(
                    "manual contact auto-merged: manual_id=%s → real_id=%s username=%s",
                    manual["id"], cid, chat_username,
                )
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
    return cid, merge_notice


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
    # То же самое для остальных типов медиа — см. save_business_message в
    # storage.py и handle_deleted_business_messages ниже (пересылка владельцу
    # при удалении собеседником). voice_file_id — сырое аудио, независимо от
    # уже расшифрованного text (is_voice/_message_text выше).
    video_file_id = event.video.file_id if event.video else None
    video_note_file_id = event.video_note.file_id if event.video_note else None
    voice_file_id = event.voice.file_id if event.voice else None

    # Временные диагностические логи RAW MEDIA MESSAGE / RAW REPLY MEDIA —
    # выясняли, получает ли бот исчезающее медиа. Итог проверки (2026-10-08):
    # само исчезающее сообщение не приходит вообще, но когда владелец на него
    # отвечает, оно приходит внутри reply_to_message с рабочим file_id и
    # has_protected_content=true (у обычного медиа этого поля нет) — на этом
    # построен _save_ephemeral_reply_media ниже. Логи писали полный дамп
    # чужой переписки в journal, поэтому выключены; не удалены — на случай,
    # если Telegram поменяет поведение и придётся проверять заново.
    # if event.photo or event.video:
    #     try:
    #         logging.info("RAW MEDIA MESSAGE: %s", event.model_dump_json(exclude_none=True)[:3000])
    #     except Exception:
    #         logging.exception("RAW MEDIA MESSAGE: не удалось сериализовать")
    #
    # reply = event.reply_to_message
    # if reply and (reply.photo or reply.video or reply.video_note or reply.voice or reply.audio or reply.document):
    #     try:
    #         logging.info("RAW REPLY MEDIA: %s", reply.model_dump_json(exclude_none=True)[:3000])
    #     except Exception:
    #         logging.exception("RAW REPLY MEDIA: не удалось сериализовать")

    # Синхронную DB-часть уводим в поток, чтобы не блокировать event loop.
    contact_id_for_rebuild, merge_notice = await asyncio.to_thread(
        _persist_business_message,
        conn_id=conn_id, owner_id=owner_id, chat_ref=chat_ref, direction=direction,
        text=text, is_voice=is_voice, date=date, tg_message_id=event.message_id,
        contact_tg_id=str(event.chat.id),
        chat_first_name=event.chat.first_name, chat_last_name=event.chat.last_name,
        chat_username=getattr(event.chat, "username", None),
        sender_username=event.from_user.username if event.from_user else None,
        photo_file_id=photo_file_id,
        video_file_id=video_file_id, video_note_file_id=video_note_file_id,
        voice_file_id=voice_file_id,
        content=_extract_business_content(event),
    )

    if contact_id_for_rebuild:
        # Друг подключил Business и пошёл живой поток — засчитываем реферала
        # (идемпотентно: после первого зачёта get_pending_referral вернёт None)
        # и спрашиваем пол (тоже идемпотентно — no-op, если уже выбран).
        await _credit_referral_if_pending(bot, owner_id)
        await _maybe_prompt_gender(bot, owner_id)
        asyncio.create_task(_maybe_rebuild(owner_id, contact_id_for_rebuild, bot))

    if merge_notice:
        manual_name, username = merge_notice
        try:
            await bot.send_message(
                owner_id,
                f"Заметил совпадение — объединил диалог «{manual_name}» с контактом "
                f"@{username}, вся история теперь в одном месте.",
            )
        except Exception:
            logging.exception("не удалось отправить уведомление об автослиянии контакта")

    if direction == "out" and event.reply_to_message:
        try:
            await _save_ephemeral_reply_media(bot, event.reply_to_message, conn_id, owner_id, chat_ref)
        except Exception:
            logging.exception("ephemeral: сбой обработки ответа owner=%s", owner_id)


# ── Исчезающие медиа (view-once / с таймером) ─────────────────────────────────
# Само такое сообщение бот через Business API не получает. Но когда ВЛАДЕЛЕЦ
# отвечает на него, оно целиком приходит внутри reply_to_message — с file_id
# и has_protected_content=true (проверено вживую 2026-10-08 на фото, видео,
# кружке и голосовом). Ловим это и шлём владельцу копию в чат с ботом.
# Ограничение: работает ТОЛЬКО по ответу владельца — без ответа бот про
# такое сообщение не узнаёт вообще.

_EPHEMERAL_KIND_LABEL = {
    "photo": "исчезающее фото", "video": "исчезающее видео",
    "video_note": "исчезающий кружок", "voice": "исчезающее голосовое",
}
_EPHEMERAL_FILENAME = {
    "photo": "photo.jpg", "video": "video.mp4", "video_note": "video_note.mp4", "voice": "voice.ogg",
}


def _premium_only_teaser_kb() -> InlineKeyboardMarkup:
    """Кнопка под тизерами Premium-фич (исчезающие, изменённые сообщения).
    delrev_premium, а не show_premium: тот отредактировал бы сам тизер в
    экран подписки, этот шлёт экран подписки отдельным сообщением."""
    b = InlineKeyboardBuilder()
    b.button(text="👑 Оформить Premium", callback_data="delrev_premium")
    return b.as_markup()


def _ephemeral_media(reply: Message) -> tuple[str, str] | None:
    """(тип, file_id) исчезающего медиа в сообщении или None. Признак
    исчезающего — has_protected_content: у обычного медиа его нет, так что
    ответ на обычное фото копию не присылает."""
    if not reply.has_protected_content:
        return None
    if reply.photo:
        return "photo", reply.photo[-1].file_id
    for kind in ("video", "video_note", "voice"):
        media = getattr(reply, kind, None)
        if media:
            return kind, media.file_id
    return None


async def _save_ephemeral_reply_media(
    bot: Bot, reply: Message, conn_id: str, owner_id: str, chat_ref: str,
) -> None:
    """Владелец ответил на сообщение — если это исчезающее медиа
    собеседника, скачиваем сразу (file_id такого медиа может быстро
    перестать работать) и шлём копию байтами, не по file_id."""
    if not reply.from_user or str(reply.from_user.id) == owner_id:
        return
    found = _ephemeral_media(reply)
    if not found:
        return
    kind, file_id = found

    # Как обычное медиа — чтобы подхватили /export и пересылка при удалении.
    # Если строка с этим tg_message_id уже есть, save_business_message
    # просто вернёт None (уникальный ключ), дубля не будет.
    await asyncio.to_thread(
        save_business_message,
        connection_id=conn_id, owner_user_id=owner_id, chat_ref=chat_ref, direction="in",
        text=reply.caption, date=reply.date.isoformat(), tg_message_id=reply.message_id,
        raw_meta={"ephemeral": True},
        **{f"{kind}_file_id": file_id},
        **_extract_business_content(reply),
    )
    if not await asyncio.to_thread(get_watch_setting, owner_id, "watch_ephemeral"):
        return  # выключено в «🗑 Удалённые сообщения»

    contact_id = await asyncio.to_thread(get_contact_id_for_chat_ref, owner_id, chat_ref)
    contact = await asyncio.to_thread(get_contact_by_id, contact_id) if contact_id else None

    # Premium-фича (как и удалённые сообщения). Без Premium — тизер, копию
    # НЕ помечаем сохранённой: оформив Premium, можно ответить на то же
    # сообщение ещё раз, и копия придёт (если файл ещё не исчез).
    if not await _is_premium(bot, owner_id):
        who = _contact_name(contact) if contact else "Собеседник"
        try:
            await bot.send_message(
                int(owner_id),
                f"💾 {who} прислал(а) {_EPHEMERAL_KIND_LABEL[kind]}\n\n"
                "🔒 Сохранять копии исчезающих — с Premium. Оформи и ответь на "
                "это сообщение ещё раз, пока оно не исчезло.",
                reply_markup=_premium_only_teaser_kb(),
            )
        except Exception:
            logging.warning("ephemeral: не удалось отправить тизер owner=%s", owner_id)
        return

    if not await asyncio.to_thread(claim_saved_ephemeral, conn_id, chat_ref, reply.message_id):
        return

    name = _contact_name(contact) if contact else "собеседника"
    caption = f"💾 Сохранил {_EPHEMERAL_KIND_LABEL[kind]} от {name}"

    try:
        data = (await bot.download(file_id)).read()
    except Exception:
        logging.exception("ephemeral: не удалось скачать %s owner=%s", kind, owner_id)
        try:
            await bot.send_message(int(owner_id), "Не удалось сохранить, файл уже недоступен")
        except Exception:
            logging.warning("ephemeral: не удалось отправить сообщение о сбое owner=%s", owner_id)
        return

    file = BufferedInputFile(data, filename=_EPHEMERAL_FILENAME[kind])
    try:
        if kind == "photo":
            await bot.send_photo(int(owner_id), file, caption=caption)
        elif kind == "video":
            await bot.send_video(int(owner_id), file, caption=caption)
        elif kind == "voice":
            await bot.send_voice(int(owner_id), file, caption=caption)
        else:
            # У кружка подписи нет вообще — текст отдельным сообщением следом,
            # как в _send_deleted_content.
            await bot.send_video_note(int(owner_id), file)
            await bot.send_message(int(owner_id), caption)
    except Exception:
        logging.exception("ephemeral: не удалось отправить копию %s owner=%s", kind, owner_id)


# ── Изменённые собеседником сообщения ─────────────────────────────────────────
# Старая версия берётся из business_messages (там уже лежит текст каждого
# сообщения), новая — из события правки. Если старой версии в базе нет
# (сообщение пришло до подключения бота) — молчим: сказать «было» нечего.
# Правки самого владельца не присылаем, но текст в базе обновляем и для
# них — карточки стиля должны видеть итоговую версию.

_EDIT_QUOTE_LIMIT = 1800  # «Было» + «Стало» вместе должны влезть в 4096


def _edit_quote(text: str | None) -> str:
    if not text:
        return "(без текста)"
    text = text if len(text) <= _EDIT_QUOTE_LIMIT else text[:_EDIT_QUOTE_LIMIT] + "…"
    return f"«{text}»"


@dp.edited_business_message()
async def handle_edited_business_message(event: Message, bot: Bot) -> None:
    conn_id = event.business_connection_id
    if not conn_id or not event.from_user:
        return
    conn_row = await asyncio.to_thread(get_business_connection, conn_id)
    if not conn_row:
        return
    owner_id = conn_row["owner_user_id"]
    chat_ref = _chat_ref(event.chat.id)

    row = await asyncio.to_thread(get_business_message_by_tg_id, conn_id, chat_ref, event.message_id)
    if not row:
        return
    # Новое медиа/подпись/форматирование — в базу сразу, до проверки текста:
    # иначе при удалении показали бы старый файл, а форматирование съехало
    # бы относительно нового текста. Уведомление «Было/Стало» — как раньше,
    # только по тексту.
    content = _extract_business_content(event)
    await asyncio.to_thread(
        update_business_message_content, row["id"],
        content["content_type"], content["media"], content["entities"],
    )

    new_text = event.text or event.caption
    if not new_text:
        return
    old_text = row["text"]
    if old_text == new_text:
        return  # правка без смены текста (например, подгрузилось превью ссылки)
    await asyncio.to_thread(update_business_message_text, row["id"], new_text)

    if str(event.from_user.id) == owner_id or row["direction"] != "in":
        return
    if not await asyncio.to_thread(get_watch_setting, owner_id, "watch_edited"):
        return  # выключено в «🗑 Удалённые сообщения»
    if await _deleted_messages_excluded(bot, owner_id, event.chat.username):
        return

    contact_id = await asyncio.to_thread(get_contact_id_for_chat_ref, owner_id, chat_ref)
    contact = await asyncio.to_thread(get_contact_by_id, contact_id) if contact_id else None
    name = _contact_name(contact) if contact else "Собеседник"

    try:
        if await _is_premium(bot, owner_id):
            await bot.send_message(
                int(owner_id),
                f"✏️ {name} изменил(а) сообщение\n\n"
                f"Было: {_edit_quote(old_text)}\n"
                f"Стало: {_edit_quote(new_text)}",
            )
        else:
            label = f"@{event.chat.username}" if event.chat.username else name
            await bot.send_message(
                int(owner_id),
                f"✏️ {label} изменил(а) сообщение\n\n"
                "🔒 Что было написано до правки — видно с Premium.",
                reply_markup=_premium_only_teaser_kb(),
            )
    except Exception:
        logging.exception("edited_business_message: не удалось уведомить owner=%s", owner_id)


# ── Удаление business-сообщений собеседником ───────────────────────────────────
# Отдельного временного кэша текста не заводим: business_messages УЖЕ хранит
# текст каждого сообщения (caption/расшифровка голоса включены) с уникальным
# ключом (connection_id, chat_ref, tg_message_id) — тем же, что приходит в
# событии удаления. get_business_message_by_tg_id просто читает по этому
# ключу. TTL-очистка тоже не нужна: это постоянное хранилище проекта
# (участвует в семплах для карточек стиля), удалять из него по возрасту
# специально под эту фичу значило бы ломать существующую логику.
#
# Если сообщения в БД нет вовсе — направление неизвестно, молчим (не
# рискуем сказать «собеседник удалил» про сообщение, которое юзер удалил
# сам). Если сообщение точно входящее, но без текста (стикер и т.п.) —
# NOTIFY_DELETED_WITHOUT_TEXT (простой флаг) решает, слать ли уведомление
# без текста или пропускать молча.

NOTIFY_DELETED_WITHOUT_TEXT = True  # False — молча пропускать удаления без известного текста

# Точечные исключения: (username владельца, username собеседника) без @, в
# нижнем регистре — владелец НЕ получает уведомления об удалённых
# сообщениях этого собеседника. Матчинг по username, не по id: если
# кто-то из пары сменит username, исключение перестанет работать.
DELETED_MESSAGES_EXCLUSIONS: set[tuple[str, str]] = {
    ("furdokw", "lisik2038"),
    ("furdokw", "maryamul4"),
}


async def _deleted_messages_excluded(bot: Bot, owner_id: str, contact_username: str | None) -> bool:
    if not contact_username:
        return False
    contact_username = contact_username.lower()
    owners = {o for o, c in DELETED_MESSAGES_EXCLUSIONS if c == contact_username}
    if not owners:
        return False  # дешёвый выход без запроса к Telegram API
    try:
        owner_chat = await bot.get_chat(int(owner_id))
    except Exception:
        logging.warning("deleted_business_messages: не удалось получить username владельца %s", owner_id)
        return False
    return bool(owner_chat.username) and owner_chat.username.lower() in owners


# ── Содержимое удалённого сообщения: тип, подписи, отправка ──────────────────
# Было (до 2026-10-23): _send_deleted_content знал только фото/видео/кружок/
# голосовое по 4 колонкам *_file_id, всё остальное (стикер, файл, гифка,
# геопозиция, контакт, опрос…) уходило как «удалил(а) сообщение (текст
# недоступен)», форматирование текста терялось. Теперь — диспетчер по
# content_type (_extract_business_content), с форматированием и альбомами.
# Старые строки (content_type = NULL) работают как раньше: тип вычисляется
# по заполненной колонке *_file_id (_row_content_type). Прежняя версия
# функции — закомментирована ниже, оставлена для отката.

_LEGACY_TYPE_COLUMNS = {
    "photo": "photo_file_id", "video": "video_file_id",
    "video_note": "video_note_file_id", "voice": "voice_file_id",
}
# Типы, которые шлются одним методом с подписью: (метод Bot, имя аргумента файла).
_CAPTION_SENDERS = {
    "photo": ("send_photo", "photo"), "video": ("send_video", "video"),
    "animation": ("send_animation", "animation"), "document": ("send_document", "document"),
    "audio": ("send_audio", "audio"), "voice": ("send_voice", "voice"),
}
_FILE_CONTENT_TYPES = set(_CAPTION_SENDERS) | {"video_note", "sticker"}
_SPOILER_TYPES = ("photo", "video", "animation")
_DICE_LABELS = {"🎲": "кубик", "🎯": "дартс", "🏀": "баскетбол", "⚽": "футбол", "🎳": "боулинг", "🎰": "слот-машину"}
_OTHER_KIND_LABELS = {
    "story": "историю", "game": "игру", "paid_media": "платное медиа", "checklist": "список задач",
    "giveaway": "розыгрыш", "giveaway_winners": "итоги розыгрыша", "invoice": "счёт на оплату",
}
_CAPTION_LIMIT = 1024


def _row_get(row, key: str):
    return row[key] if key in row.keys() else None


def _row_content_type(row) -> str:
    """content_type строки; для старых строк (NULL) — по колонке *_file_id,
    как работало раньше. 'unknown' — старая строка без текста и без медиа."""
    ct = _row_get(row, "content_type")
    if ct:
        return ct
    for kind, col in _LEGACY_TYPE_COLUMNS.items():
        if _row_get(row, col):
            return kind
    return "text" if row["text"] else "unknown"


def _row_media(row) -> dict:
    raw = _row_get(row, "media_json")
    if raw:
        try:
            return json.loads(raw) or {}
        except ValueError:
            logging.warning("business_messages.media_json не читается, id=%s", _row_get(row, "id"))
    col = _LEGACY_TYPE_COLUMNS.get(_row_content_type(row))
    file_id = _row_get(row, col) if col else None
    return {"file_id": file_id} if file_id else {}


def _row_entities(row) -> list | None:
    raw = _row_get(row, "entities_json")
    if not raw:
        return None
    try:
        return json.loads(raw) or None
    except ValueError:
        return None


def _content_label(ct: str, media: dict, detailed: bool = True) -> str:
    """Что удалили, в винительном падеже («удалил(а) …»). detailed=False —
    без подробностей (имя файла, эмодзи стикера): для тизера без Premium,
    где само содержимое не раскрываем."""
    if ct == "sticker":
        emoji = media.get("emoji")
        return f"стикер {emoji}" if detailed and emoji else "стикер"
    if ct == "animation":
        return "гифку"
    if ct == "photo":
        return "фото"
    if ct == "video":
        return "видео"
    if ct == "video_note":
        return "видеосообщение (кружок)"
    if ct == "voice":
        return "голосовое"
    if ct == "audio":
        title = " — ".join(p for p in (media.get("performer"), media.get("title")) if p)
        return f"аудио «{title}»" if detailed and title else "аудио"
    if ct == "document":
        name = media.get("file_name")
        return f"файл «{name}»" if detailed and name else "файл"
    if ct == "location":
        return "геопозицию"
    if ct == "venue":
        title = media.get("title")
        return f"место «{title}»" if detailed and title else "место"
    if ct == "contact":
        return "контакт"
    if ct == "poll":
        return "опрос"
    if ct == "dice":
        return _DICE_LABELS.get(media.get("emoji"), "кубик")
    if ct == "other":
        return _OTHER_KIND_LABELS.get(media.get("kind"), "сообщение")
    return "сообщение"


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _compose_with_quote(header: str, text: str | None, entities: list | None, limit: int) -> tuple[str, list | None]:
    """«{header}:\\n«{text}»» с форматированием исходного текста. Смещения
    сущностей Telegram считает в UTF-16 — сдвигаем на длину префикса. Не
    влезает в лимит — режем текст и отдаём без форматирования (обрезанная
    сущность сломала бы отправку)."""
    if not text:
        return header, None
    prefix = f"{header}:\n«"
    full = f"{prefix}{text}»"
    if len(full) > limit:
        room = max(0, limit - len(prefix) - 2)
        return f"{prefix}{text[:room]}…»", None
    if not entities:
        return full, None
    shift = _utf16_len(prefix)
    return full, [{**e, "offset": e["offset"] + shift} for e in entities]


def _entity_objects(entities: list | None) -> list[MessageEntity] | None:
    if not entities:
        return None
    try:
        return [MessageEntity.model_validate(e) for e in entities]
    except Exception:
        logging.warning("deleted: не удалось собрать форматирование, шлю без него")
        return None


async def _send_with_entities(send, text_arg: str, text: str, entities: list | None, **kwargs):
    """Отправка с форматированием; если Telegram его отклонил (обычно
    custom_emoji — бот не всегда вправе ими пользоваться), повторяем без
    форматирования обычным текстом. Ошибка самой отправки (протухший
    file_id и т.п.) после повтора пробрасывается вызывающему."""
    ents_arg = "entities" if text_arg == "text" else "caption_entities"
    objs = _entity_objects(entities)
    if objs:
        try:
            return await send(**{text_arg: text, ents_arg: objs}, **kwargs)
        except TelegramBadRequest as e:
            logging.warning("deleted: Telegram отклонил форматирование (%s) — повтор без него", e)
    return await send(**{text_arg: text}, **kwargs)


async def _send_deleted_content(bot: Bot, owner_id: str, name: str, row) -> None:
    """Содержимое одного удалённого входящего сообщения владельцу — общий код
    для Premium-юзеров (сразу при удалении) и для кнопки «👀 Показать» на
    тизере (cb_deleted_reveal), чтобы после оплаты показывалось ровно то
    же, что видят премиум-юзеры. Файлы шлются по сохранённому file_id; если
    Telegram его уже не отдаёт (старое сообщение, файл протух) — не молчим,
    откатываемся на текстовое уведомление с указанием типа."""
    chat_id = int(owner_id)
    ct = _row_content_type(row)
    media = _row_media(row)
    text = row["text"]
    entities = _row_entities(row)
    header = f"🗑 {name} удалил(а) {_content_label(ct, media)}"
    file_id = media.get("file_id")

    try:
        if ct in _CAPTION_SENDERS and file_id:
            method, file_arg = _CAPTION_SENDERS[ct]
            caption, cap_entities = _compose_with_quote(header, text, entities, _CAPTION_LIMIT)
            extra = {"has_spoiler": True} if ct in _SPOILER_TYPES and media.get("has_spoiler") else {}
            await _send_with_entities(
                getattr(bot, method), "caption", caption, cap_entities,
                chat_id=chat_id, **{file_arg: file_id}, **extra,
            )
            return
        if ct == "video_note" and file_id:
            # У кружка нет подписи вообще (ограничение Bot API) — текст следом.
            await bot.send_video_note(chat_id, file_id)
            await bot.send_message(chat_id, header)
            return
        if ct == "sticker" and file_id:
            # У стикера тоже нет подписи — текст следом.
            await bot.send_sticker(chat_id, file_id)
            await bot.send_message(chat_id, header)
            return
        if ct == "location" and "latitude" in media:
            await bot.send_location(chat_id, latitude=media["latitude"], longitude=media["longitude"])
            await bot.send_message(chat_id, header)
            return
        if ct == "venue" and "latitude" in media:
            await bot.send_venue(
                chat_id, latitude=media["latitude"], longitude=media["longitude"],
                title=media.get("title") or "Место", address=media.get("address") or "",
                foursquare_id=media.get("foursquare_id"), foursquare_type=media.get("foursquare_type"),
                google_place_id=media.get("google_place_id"), google_place_type=media.get("google_place_type"),
            )
            await bot.send_message(chat_id, header)
            return
        if ct == "contact" and media.get("phone_number"):
            await bot.send_contact(
                chat_id, phone_number=media["phone_number"],
                first_name=media.get("first_name") or "Контакт",
                last_name=media.get("last_name"), vcard=media.get("vcard"),
            )
            await bot.send_message(chat_id, header)
            return
    except Exception:
        logging.exception(
            "deleted_business_messages: не удалось переслать %s (file_id=%s) owner=%s — откат на текст",
            ct, file_id, owner_id,
        )

    if ct == "poll" and media.get("question"):
        options = "\n".join(f"• {o}" for o in media.get("options") or [])
        notice, notice_entities = f"{header}: «{media['question']}»" + (f"\n{options}" if options else ""), None
    elif ct == "dice" and media.get("value") is not None:
        notice, notice_entities = f"{header} {media.get('emoji', '')}: выпало {media['value']}", None
    elif ct == "other" and media.get("kind") == "checklist" and media.get("title"):
        tasks = "\n".join(f"{'✅' if t.get('done') else '⬜'} {t.get('text', '')}" for t in media.get("tasks") or [])
        notice, notice_entities = f"{header}: «{media['title']}»" + (f"\n{tasks}" if tasks else ""), None
    elif ct == "other":
        notice, notice_entities = f"{header} — показать его содержимое бот не может", None
    elif ct == "unknown":
        # Старая строка без текста и без медиа — как было раньше.
        if not NOTIFY_DELETED_WITHOUT_TEXT:
            return
        notice, notice_entities = f"🗑 {name} удалил(а) сообщение (текст недоступен)", None
    else:
        # Текст, а также откат для файла, который Telegram уже не отдаёт.
        notice, notice_entities = _compose_with_quote(header, text, entities, TELEGRAM_MAX_LEN)
        if ct in _FILE_CONTENT_TYPES or ct in ("location", "venue", "contact"):
            notice += "\n(сам файл уже недоступен)" if ct in _FILE_CONTENT_TYPES else "\n(показать не получилось)"

    try:
        await _send_with_entities(bot.send_message, "text", notice, notice_entities, chat_id=chat_id)
    except Exception:
        logging.exception("deleted_business_messages: не удалось уведомить owner=%s", owner_id)


# ── Альбомы ───────────────────────────────────────────────────────────────────
# Удалённый целиком альбом показываем одним альбомом (send_media_group), а
# не пачкой отдельных сообщений. Telegram разрешает в альбоме фото+видео
# вместе, документы — только с документами, аудио — только с аудио.

_ALBUM_INPUT = {"photo": InputMediaPhoto, "video": InputMediaVideo,
                "document": InputMediaDocument, "audio": InputMediaAudio}
_ALBUM_COMPATIBLE = ({"photo", "video"}, {"photo"}, {"video"}, {"document"}, {"audio"})


async def _send_deleted_album(bot: Bot, owner_id: str, name: str, rows: list) -> bool:
    """True — альбом ушёл. False — собрать не получилось (неподходящие
    типы, нет file_id, Telegram отказал) — тогда вызывающий шлёт по одному."""
    kinds = [_row_content_type(r) for r in rows]
    if not 2 <= len(rows) <= 10 or set(kinds) not in _ALBUM_COMPATIBLE:
        return False
    medias = [_row_media(r) for r in rows]
    if not all(m.get("file_id") for m in medias):
        return False

    header = f"🗑 {name} удалил(а) альбом ({len(rows)})"

    def build(with_entities: bool) -> list:
        items = []
        for i, (row, kind, media) in enumerate(zip(rows, kinds, medias)):
            text, entities = row["text"], _row_entities(row)
            if i == 0:
                caption, cap_entities = _compose_with_quote(header, text, entities, _CAPTION_LIMIT)
            else:
                caption = (text or "")[:_CAPTION_LIMIT] or None
                cap_entities = entities if caption and len(text) <= _CAPTION_LIMIT else None
            kwargs = {"media": media["file_id"], "caption": caption}
            objs = _entity_objects(cap_entities) if with_entities else None
            if objs:
                kwargs["caption_entities"] = objs
            if kind in _SPOILER_TYPES and media.get("has_spoiler"):
                kwargs["has_spoiler"] = True
            items.append(_ALBUM_INPUT[kind](**kwargs))
        return items

    for with_entities in (True, False):
        try:
            await bot.send_media_group(int(owner_id), media=build(with_entities))
            return True
        except TelegramBadRequest as e:
            if with_entities:
                logging.warning("deleted album: Telegram отказал (%s) — повтор без форматирования", e)
                continue
            logging.warning("deleted album: не удалось отправить альбом (%s) — шлю по одному", e)
        except Exception:
            logging.exception("deleted album: сбой отправки альбома owner=%s — шлю по одному", owner_id)
            break
    return False


async def _send_deleted_batch(bot: Bot, owner_id: str, name: str, rows: list) -> None:
    """Все удалённые сообщения одного события (или одного тизера) по
    порядку; альбом, удалённый ЦЕЛИКОМ (все его сохранённые сообщения есть
    в rows), — одним альбомом, иначе каждое сообщение отдельно."""
    deleted_ids = {r["id"] for r in rows}
    done: set[int] = set()
    for row in rows:
        if row["id"] in done:
            continue
        group_id = _row_get(row, "media_group_id")
        if group_id:
            group_rows = [r for r in rows if _row_get(r, "media_group_id") == group_id]
            members = await asyncio.to_thread(
                get_business_media_group, row["connection_id"], row["chat_ref"], group_id,
            )
            if len(group_rows) >= 2 and {m["id"] for m in members} <= deleted_ids:
                done.update(r["id"] for r in group_rows)
                if not await _send_deleted_album(bot, owner_id, name, group_rows):
                    for r in group_rows:
                        await _send_deleted_content(bot, owner_id, name, r)
                continue
        done.add(row["id"])
        await _send_deleted_content(bot, owner_id, name, row)


def _has_deleted_content(row) -> bool:
    """Есть ли что показать по удалённому сообщению. Строки без содержимого
    (старые без текста и медиа, или виды, которые бот повторить не может —
    история, игра и т.п.) в тизер для не-Premium не берём: после оплаты
    показать по ним было бы нечего."""
    if row["text"]:
        return True
    ct = _row_content_type(row)
    if ct == "other":
        return _row_media(row).get("kind") == "checklist"
    if ct in ("unknown", "text"):
        return False
    return bool(_row_media(row))


# Прежняя версия — только 4 типа медиа по колонкам *_file_id, оставлена для отката:
#
# def _has_deleted_content(row) -> bool:
#     if row["text"]:
#         return True
#     return any(
#         k in row.keys() and row[k]
#         for k in ("photo_file_id", "video_file_id", "video_note_file_id", "voice_file_id")
#     )
#
# async def _send_deleted_content(bot: Bot, owner_id: str, name: str, row) -> None:
#     text = row["text"]
#     photo_file_id = row["photo_file_id"] if "photo_file_id" in row.keys() else None
#     video_file_id = row["video_file_id"] if "video_file_id" in row.keys() else None
#     video_note_file_id = row["video_note_file_id"] if "video_note_file_id" in row.keys() else None
#     voice_file_id = row["voice_file_id"] if "voice_file_id" in row.keys() else None
#     if photo_file_id:
#         caption = f"🗑 {name} удалил(а) фото" + (f":\n«{text}»" if text else "")
#         try:
#             await bot.send_photo(int(owner_id), photo_file_id, caption=caption[:1024])
#             return
#         except Exception:
#             logging.exception("... не удалось переслать фото — откат на текст")
#     elif video_file_id:
#         caption = f"🗑 {name} удалил(а) видео" + (f":\n«{text}»" if text else "")
#         try:
#             await bot.send_video(int(owner_id), video_file_id, caption=caption[:1024])
#             return
#         except Exception:
#             logging.exception("... не удалось переслать видео — откат на текст")
#     elif video_note_file_id:
#         try:
#             await bot.send_video_note(int(owner_id), video_note_file_id)
#             await bot.send_message(int(owner_id), f"🗑 {name} удалил(а) видеосообщение (кружок)")
#             return
#         except Exception:
#             logging.exception("... не удалось переслать кружок — откат на текст")
#     elif voice_file_id:
#         caption = f"🗑 {name} удалил(а) голосовое" + (f":\n«{text}»" if text else "")
#         try:
#             await bot.send_voice(int(owner_id), voice_file_id, caption=caption[:1024])
#             return
#         except Exception:
#             logging.exception("... не удалось переслать голосовое — откат на текст")
#     if text:
#         notice = f"🗑 {name} удалил(а) сообщение:\n«{text}»"
#     elif NOTIFY_DELETED_WITHOUT_TEXT:
#         notice = f"🗑 {name} удалил(а) сообщение (текст недоступен)"
#     else:
#         return
#     try:
#         await bot.send_message(int(owner_id), notice)
#     except Exception:
#         logging.exception("deleted_business_messages: не удалось уведомить owner=%s", owner_id)


def _deleted_teaser_kb(reveal_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👀 Показать", callback_data=f"delrev:{reveal_id}")
    # НЕ show_premium: тот редактирует текущее сообщение в экран подписки
    # (edit=True) — тизер вместе с кнопкой «👀 Показать» затирался, и
    # после оплаты вернуться к удалённому было не к чему. delrev_premium
    # шлёт экран подписки НОВЫМ сообщением, тизер остаётся.
    b.button(text="👑 Оформить Premium", callback_data="delrev_premium")
    b.adjust(1)
    return b.as_markup()


def _teaser_kinds(rows: list) -> list[str]:
    """Типы удалённого для тизера — без подробностей (имя файла, эмодзи
    стикера), содержимое без Premium не раскрываем."""
    return [_content_label(_row_content_type(r), _row_media(r), detailed=False) for r in rows]


def _deleted_teaser_text(label: str, count: int, kinds: list[str] | None = None) -> str:
    """kinds — типы удалённого (_teaser_kinds). Без них — как раньше:
    «сообщение»/«сообщения (N)»."""
    if count == 1:
        what = kinds[0] if kinds else "сообщение"
    else:
        what = f"сообщения ({count})"
        distinct = list(dict.fromkeys(k if k != "сообщение" else "текст" for k in (kinds or [])))
        if distinct and distinct != ["текст"]:
            what += ": " + ", ".join(distinct)
    return (
        f"🗑 {label} удалил(а) {what}\n\n"
        "🔒 Чтобы посмотреть, что там было, — оформи Premium. После оплаты "
        "нажми «👀 Показать» — откроется именно это удалённое."
    )


@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot) -> None:
    conn_id = event.business_connection_id
    conn_row = await asyncio.to_thread(get_business_connection, conn_id)
    if not conn_row:
        logging.warning("deleted_business_messages: unknown connection %s", conn_id)
        return
    owner_id = conn_row["owner_user_id"]
    chat_ref = _chat_ref(event.chat.id)
    if not await asyncio.to_thread(get_watch_setting, owner_id, "watch_deleted"):
        return  # выключено в «🗑 Удалённые сообщения»

    if await _deleted_messages_excluded(bot, owner_id, event.chat.username):
        return

    contact_id = await asyncio.to_thread(get_contact_id_for_chat_ref, owner_id, chat_ref)
    contact = await asyncio.to_thread(get_contact_by_id, contact_id) if contact_id else None
    name = _contact_name(contact) if contact else "Собеседник"

    # Реагируем ТОЛЬКО на удаление ВХОДЯЩИХ (direction тот же принцип,
    # что в handle_business_message: "in" = прислал собеседник, не
    # владелец). Если строки нет вообще — направление НЕИЗВЕСТНО, и
    # молчим: рисковать написать юзеру "собеседник удалил", когда на
    # самом деле он сам удалил своё же сообщение, хуже, чем пропустить
    # уведомление (NOTIFY_DELETED_WITHOUT_TEXT относится только к
    # случаю "точно входящее, но текста нет" — сообщение БЕЗ текста/
    # caption, не к "сообщения нет в базе вовсе").
    rows = []
    for tg_message_id in event.message_ids:
        row = await asyncio.to_thread(get_business_message_by_tg_id, conn_id, chat_ref, tg_message_id)
        if row and row["direction"] == "in":
            rows.append(row)
    if not rows:
        return

    if await _is_premium(bot, owner_id):
        await _send_deleted_batch(bot, owner_id, name, rows)
        return

    # Без Premium (бесплатный период закончился, см. /broadcast_deleted_paywall)
    # содержимое не показываем — только тизер: КТО удалил (@username, если
    # есть, иначе имя контакта) и сколько. Одно сообщение на событие, даже
    # если удалено сразу несколько. Id строк запоминаем в deleted_reveals —
    # после оплаты кнопка «👀 Показать» откроет ровно их.
    revealable = [r for r in rows if _has_deleted_content(r)]
    if not revealable:
        return
    label = f"@{event.chat.username}" if event.chat.username else name
    reveal_id = await asyncio.to_thread(
        create_deleted_reveal, owner_id, label, [r["id"] for r in revealable],
    )
    try:
        await bot.send_message(
            int(owner_id), _deleted_teaser_text(label, len(revealable), _teaser_kinds(revealable)),
            reply_markup=_deleted_teaser_kb(reveal_id),
        )
    except Exception:
        logging.exception("deleted_business_messages: не удалось отправить тизер owner=%s", owner_id)


@dp.callback_query(F.data.startswith("delrev:"))
async def cb_deleted_reveal(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    try:
        reveal_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    reveal = await asyncio.to_thread(get_deleted_reveal, reveal_id)
    if not reveal or reveal["owner_user_id"] != telegram_id:
        await call.answer("Не нашёл это сообщение.", show_alert=True)
        return

    if not await _is_premium(bot, telegram_id):
        await call.answer(
            "🔒 Удалённое доступно только с Premium — оформи подписку и нажми «👀 Показать» ещё раз.",
            show_alert=True,
        )
        return

    await call.answer()
    if not await _reveal_deleted(bot, telegram_id, reveal):
        await call.message.answer("Не получилось достать это сообщение — его уже нет в базе.")
    # Убираем кнопки с тизера — уже показано, повторно жать незачем.
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _reveal_deleted(bot: Bot, telegram_id: str, reveal) -> int:
    """Показывает содержимое одного скрытого удаления и помечает его
    показанным. Возвращает, сколько сообщений удалось достать из базы."""
    rows = []
    for row_id in json.loads(reveal["message_row_ids"]):
        row = await asyncio.to_thread(get_business_message_by_row_id, row_id)
        if row:
            rows.append(row)
    # Через _send_deleted_batch, как у Premium сразу при удалении — альбом
    # и после оплаты приходит альбомом.
    await _send_deleted_batch(bot, telegram_id, reveal["contact_label"], rows)
    await asyncio.to_thread(mark_deleted_reveal_revealed, reveal["id"])
    return len(rows)


@dp.callback_query(F.data == "delrev_premium")
async def cb_deleted_reveal_premium(call: CallbackQuery, bot: Bot) -> None:
    """Экран подписки с тизера удалённого — НОВЫМ сообщением (edit=False),
    чтобы тизер с кнопкой «👀 Показать» не затёрся (см. _deleted_teaser_kb)."""
    await call.answer()
    await main._show_premium_screen(call.message, bot, str(call.from_user.id), edit=False)


async def _offer_pending_reveals(bot: Bot, telegram_id: str) -> None:
    """Подстраховка на случай, если тизер с кнопкой «👀 Показать» потерялся
    (затёрт, удалён, ушёл далеко вверх): как только у юзера появился
    Premium — сами предлагаем показать всё, что было скрыто. Вызывается из
    всех мест выдачи Premium (промо-канал, Stars, Tribute, реферал) и с
    экрана «👑 Подписка», если Premium уже активен. Молча ничего не делает,
    если скрытого нет или Premium всё-таки не активен."""
    try:
        pending = await asyncio.to_thread(get_unrevealed_deleted_reveals, telegram_id)
        if not pending or not await _is_premium(bot, telegram_id):
            return
        count = sum(len(json.loads(r["message_row_ids"])) for r in pending)
        b = InlineKeyboardBuilder()
        b.button(text="👀 Показать", callback_data="delrev_all")
        await bot.send_message(
            int(telegram_id),
            f"🔓 Premium активен — теперь можно посмотреть удалённые сообщения, "
            f"которые были скрыты ({count}).",
            reply_markup=b.as_markup(),
        )
    except Exception:
        logging.exception("offer pending reveals failed: telegram_id=%s", telegram_id)


@dp.callback_query(F.data == "delrev_all")
async def cb_deleted_reveal_all(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    if not await _is_premium(bot, telegram_id):
        await call.answer("🔒 Удалённое доступно только с Premium.", show_alert=True)
        return
    await call.answer()
    pending = await asyncio.to_thread(get_unrevealed_deleted_reveals, telegram_id)
    shown = 0
    for reveal in pending:
        shown += await _reveal_deleted(bot, telegram_id, reveal)
    if not shown:
        await call.message.answer("Скрытых удалённых сообщений больше нет — всё уже показано.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
