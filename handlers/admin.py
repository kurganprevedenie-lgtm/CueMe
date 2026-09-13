"""Админские команды: сводка по пользователям (/users), источники (/sources),
использование подсказок (/suggestion_stats), выгрузка переписок (/export),
переключение LLM-провайдера (/provider), диагностика и очистка конкретного
юзера (/inspect, /wipe), разовая рассылка про рефералку (/broadcast_invite)
и захват file_id фото-инструкции.

Все хендлеры молча выходят для неадминов (_is_admin) — это поведение
перенесено как есть.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
import asyncio
import csv
import html
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_GROUP_CHAT_ID
from handlers.common import (
    _GENDER_LABELS,
    _contact_name,
    _format_remaining,
    _is_admin,
    _is_premium,
    _premium_expiry_info,
)
from handlers.referral import _invite_text, invite_kb
from llm import PROVIDER_NAMES, get_forced_provider, get_provider_stats, set_forced_provider
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    delete_all_user_data,
    event_counts_by_user,
    get_business_connections_history,
    get_contact_last_messages,
    get_last_event_time,
    get_last_incoming_message_time,
    get_latest_business_connection,
    list_all_users,
    list_contacts,
    mark_bot_blocked,
    referral_counts_by_user,
    suggestion_stats_by_user,
    users_with_deep_analysis,
    users_with_style_card,
)
from tools.export import extract_conversation, to_html, to_text

router = Router(name="admin")


# ── /broadcast_invite — разовое напоминание про рефералку ВСЕМ (только админ) ─

@router.message(Command("broadcast_invite"))
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


@router.callback_query(F.data == "bcast:invite:confirm")
async def cb_broadcast_invite_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await call.message.edit_text("Рассылка началась в фоне — пришлю итоги, когда закончится.")
    asyncio.create_task(_run_broadcast_invite(bot, call.from_user.id))


@router.callback_query(F.data == "bcast:invite:cancel")
async def cb_broadcast_invite_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Отменено.")


# ── Захват file_id фото-инструкции (только для админа) ───────────────────────
# Разработчик присылает фото боту напрямую (просто как сообщение) — бот в
# ответ шлёт его file_id, который нужно прописать в ONBOARDING_PHOTO_FILE_ID
# (.env на сервере). Фото хранится на серверах Telegram, не в репозитории.

@router.message(F.photo)
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


@router.message(Command("users"))
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


@router.message(Command("sources"))
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

@router.message(Command("suggestion_stats"))
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

@router.message(Command("export"))
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


@router.callback_query(F.data.startswith("export:"))
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

@router.message(Command("provider"))
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

@router.message(Command("inspect"))
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

@router.message(Command("wipe"))
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


@router.callback_query(F.data.startswith("wipeyes:"))
async def cb_wipe_confirm(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    target_id = call.data.split(":", 1)[1]
    delete_all_user_data(target_id)
    await call.answer("Стёрто")
    await call.message.edit_text(f"✓ Все данные пользователя {target_id} удалены.")


@router.callback_query(F.data == "wipeno")
async def cb_wipe_cancel(call: CallbackQuery) -> None:
    await call.answer("Отменено")
    await call.message.edit_text("Отменено.")


# /auto и auto_rewrite_handler (catch-all авто-переписка) убраны вместе с
# «Переписать» — тот же сценарий (черновик без привязки к входящему) теперь
# закрывает «💫 Новый диалог». get_auto_mode/set_auto_mode/auto_contact_id в
# storage.py не тронуты (неиспользуемые, но безвредные) — не было смысла
# трогать схему БД ради этого.
