"""Админка: /users, /sources, /suggestion_stats, /export, /provider, /apistatus.

Вынесено из main.py без изменения логики (строки 6931–8570 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import csv
import html
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from aiogram import Bot, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaDocument,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import ADMIN_GROUP_CHAT_ID, CHURN_HOURS, PROMO_CHANNEL_USERNAME
from llm import (
    get_invalid_output_counts,
    PROVIDER_NAMES,
    get_forced_provider,
    get_provider_stats,
    get_provider_stats_snapshot,
    get_groq_model_stats,
    KNOWN_DAILY_LIMITS,
    set_forced_provider,
)
from compatibility_metrics import MOSCOW_TZ
from tools.export import extract_conversation, to_html, to_text
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    event_counts_by_user,
    get_latest_business_connection,
    get_last_event_time,
    gate_events_by_user,
    gate_check_fail_without_ok,
    get_last_incoming_message_time,
    list_all_users,
    list_contacts,
    referral_counts_by_user,
    suggestion_stats_by_user,
    users_with_deep_analysis,
    users_with_style_card,
)
from app_state import _answer_long, _is_admin, dp
from handlers.subscription import _is_premium
from handlers.profile import _GENDER_LABELS, _action_label
import handlers  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


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


def _fmt_msk(iso_str: str | None) -> str:
    """Абсолютные дата+время в МСК с явной пометкой — для карточки /users
    («когда последний раз писал собеседник»). В отличие от _relative_label
    (относительное «N дней назад») и _csv_dt (остаётся в UTC, для CSV) — тут
    именно абсолютный момент, переведённый в МСК. «—», если даты нет."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M") + " (МСК)"


async def _is_subscribed_to_promo_channel(bot: Bot, telegram_id: str) -> bool:
    """Живой статус подписки на промо-канал (PROMO_CHANNEL_USERNAME) — для
    карточки /users, отдельный запрос на юзера (тот же профиль стоимости,
    что уже есть у _is_premium в этом же отчёте)."""
    try:
        member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def _yn(value: bool) -> str:
    return "да" if value else "нет"


_PREMIUM_SOURCE_LABELS = {
    "referral": "реферал", "promo_channel": "промо-канал",
    "stars": "Stars", "tribute": "Tribute", "gift": "подарок", "нет": "—",
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
    gate_by_user, gate_tracking_start = gate_events_by_user()
    gate_fail_no_ok = gate_check_fail_without_ok()

    rows: list[dict] = []
    totals = {
        "total": len(users), "with_gender": 0, "with_contact": 0,
        "with_ref_premium": 0, "blocked": 0, "automation_off": 0, "inactive": 0,
        "premium_now": 0, "used_reply": 0, "used_screenshot": 0, "used_live": 0,
        "used_deep_analysis": 0, "active_7d": 0, "subscribed_channel": 0,
        "trial_used": 0,
        "by_source": {},
        "premium_by_source": {},
    }

    for u in users:
        tid = u["telegram_id"]
        who = await _resolve_username(bot, tid)
        contacts = list_contacts(tid)

        if u["gender"]:
            totals["with_gender"] += 1
        if contacts:
            totals["with_contact"] += 1

        source_label = _SOURCE_LABELS.get(u["acquisition_source"], "не указан")
        trial_used_flag = bool(u["trial_used"])
        if trial_used_flag:
            totals["trial_used"] += 1
        source_bucket = totals["by_source"].setdefault(
            source_label, {"total": 0, "connected": 0, "trial_used": 0, "premium": 0}
        )
        source_bucket["total"] += 1
        if contacts:
            source_bucket["connected"] += 1
        if trial_used_flag:
            source_bucket["trial_used"] += 1

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

        # Точное время последнего действия в боте (LastActionMiddleware) — если
        # его ещё нет (юзер ничего не делал с момента появления колонки),
        # приблизительный бэкафилл: последнее событие из events (/start,
        # генерации, оплаты) — как и было здесь раньше. Само действие в таком
        # случае неизвестно ("—"), см. last_action ниже.
        last_action_raw = u["last_action_at"] or get_last_event_time(tid)
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
            premium_source, premium_until, premium_is_subscription = handlers.premium_screen._premium_expiry_info(tid)
            totals["premium_now"] += 1
            source_bucket["premium"] += 1
            totals["premium_by_source"][premium_source] = (
                totals["premium_by_source"].get(premium_source, 0) + 1
            )
            if premium_source == "referral":
                totals["with_ref_premium"] += 1
        else:
            premium_source = "нет"

        premium_remaining = (
            handlers.premium_screen._format_remaining(premium_until - now) if premium_until else ""
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

        # Живой статус подписки на промо-канал (для карточки /users) — тот же
        # профиль стоимости, что уже есть у _is_premium выше (один запрос к
        # Telegram на юзера), отдельного кэша/реконсиляции тут не переиспользуем
        # (та таблица покрывает только юзеров с активным промо-Premium).
        is_subscribed_channel = await _is_subscribed_to_promo_channel(bot, tid)
        if is_subscribed_channel:
            totals["subscribed_channel"] += 1

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

        connected = bool(latest_conn) and not automation_off

        rows.append({
            "username": who,
            "telegram_id": tid,
            "gender": _GENDER_LABELS.get(u["gender"], "?"),
            "source": source_label,
            "funnel_stage": _funnel_stage(connected, trial_used_flag, is_premium_now),
            "signup_week": _csv_dt(u["created_at"], "%G-W%V"),
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
            "_last_incoming_msk": _fmt_msk(last_incoming_raw),
            "_is_subscribed_channel": is_subscribed_channel,
            "_inactive": is_stale,
            **_gate_and_churn_fields(
                u, tid, last_action_raw, now, is_premium_now,
                gate_by_user.get(tid, set()), gate_tracking_start, tid in gate_fail_no_ok,
            ),
        })

    totals["gate_tracking_start"] = gate_tracking_start
    return rows, totals


def _users_totals(rows: list[dict], gate_tracking_start) -> dict:
    """Те же агрегаты, что копит _collect_users_data, но по любому подмножеству
    строк — для среза отчёта /users по полу (переключатель наверху страницы).
    Считается только из полей строк, без запросов. Совпадение с агрегатами
    цикла на всех строках проверяет tests/test_users_gender.py."""
    t = {
        "total": len(rows), "with_gender": 0, "with_contact": 0,
        "with_ref_premium": 0, "blocked": 0, "automation_off": 0, "inactive": 0,
        "premium_now": 0, "used_reply": 0, "used_screenshot": 0, "used_live": 0,
        "used_deep_analysis": 0, "active_7d": 0, "subscribed_channel": 0,
        "trial_used": 0, "by_source": {}, "premium_by_source": {},
        "gate_tracking_start": gate_tracking_start,
    }
    for r in rows:
        has_contacts = r["contacts_count"] > 0
        trial = bool(r["trial_used"])
        b = t["by_source"].setdefault(r["source"], {"total": 0, "connected": 0, "trial_used": 0, "premium": 0})
        b["total"] += 1
        b["connected"] += has_contacts
        b["trial_used"] += trial
        t["with_gender"] += r["gender"] != "?"
        t["with_contact"] += has_contacts
        t["trial_used"] += trial
        t["blocked"] += bool(r["blocked"])
        t["automation_off"] += bool(r["automation_off"])
        t["inactive"] += bool(r["_inactive"])
        t["used_reply"] += bool(r["uses_reply"])
        t["used_screenshot"] += bool(r["uses_screenshot"])
        t["used_live"] += bool(r["uses_live"])
        t["used_deep_analysis"] += bool(r["used_deep_analysis"])
        t["active_7d"] += bool(r["active_last_7d"])
        t["subscribed_channel"] += bool(r["_is_subscribed_channel"])
        if r["is_premium_now"]:
            t["premium_now"] += 1
            b["premium"] += 1
            src = r["premium_source"]
            t["premium_by_source"][src] = t["premium_by_source"].get(src, 0) + 1
            t["with_ref_premium"] += src == "referral"
    return t


def _hours_since(iso_str: str | None, now: datetime) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((now - dt).total_seconds() // 3600)


_GATE_STATUS_UNKNOWN = "—"


def _gate_and_churn_fields(
    u, tid: str, last_action_raw: str | None, now: datetime, is_premium_now: bool,
    gate_events: set[str], gate_tracking_start: str | None, gate_fail_no_ok: bool,
) -> dict:
    """Поля «Где уходят» для строки юзера в /users (CSV + дашборд).

    gate_status: «не видел» / «видел, не открыл» / «нажал подписался, не
    подписан» / «разблокировал». Пока события гейта не пишутся вообще, или
    юзер последний раз был в боте ДО их появления — «—»: видел ли он гейт
    раньше, неизвестно, а «не видел» было бы неправдой.
    _churned — без Premium и без открытого пула, неактивен дольше CHURN_HOURS."""
    unlocked = is_premium_now or bool(
        u["reply_trial_bonus"] or u["analysis_trial_bonus"] or u["date_trial_bonus"]
    )
    shown = "gate_shown" in gate_events
    if not shown:
        tracked = gate_tracking_start is not None and (last_action_raw or "") >= gate_tracking_start
        gate_status = "не видел" if tracked else _GATE_STATUS_UNKNOWN
    elif unlocked:
        gate_status = "разблокировал"
    elif gate_fail_no_ok:
        gate_status = "нажал подписался, не подписан"
    else:
        gate_status = "видел, не открыл"

    hours = _hours_since(last_action_raw, now)
    churned = not unlocked and (hours is None or hours >= CHURN_HOURS)
    return {
        "last_action": _action_label(u["last_action"]),
        "hours_since_last_action": hours if hours is not None else "—",
        "gate_status": gate_status,
        "_gate_events": gate_events,
        "_unlocked": unlocked,
        "_churned": churned,
    }


# _build_users_report — раньше рендерила /users как кучу текстовых сообщений
# (шапка + блок на каждого активного юзера + сводка), которые Telegram резал
# на чанки по 3500 символов. По прямому запросу — больше не шлём: только два
# файла (HTML + CSV, см. _build_users_html/_build_users_csv и cmd_users
# ниже). Не удалена физически — на случай отката.
# def _build_users_report(rows: list[dict], totals: dict, hidden_count: int = 0) -> list[str]:
#     """Возвращает список блоков (шапка, по одному на юзера, сводка) — НЕ
#     склеенную строку, чтобы cmd_users мог резать на чанки по границам
#     блоков, а не посреди HTML-тега.
#
#     rows — сюда передаются ТОЛЬКО активные (см. cmd_users: фильтр по
#     _status_line == "✅ Активен") — заблокировавших бота, отключивших
#     Автоматизацию и застойных юзеров в сообщение не выводим, они есть
#     только в CSV. hidden_count — сколько строк скрыто, для пометки в шапке."""
#     blocks = ["👥 <b>Пользователи CueMe</b>"]
#     if hidden_count:
#         blocks.append(
#             f"Показаны только активные — {hidden_count} неактивных/заблокировавших/"
#             "отключивших Автоматизацию скрыты из сообщения, они есть в CSV-файле ниже."
#         )
#
#     for r in rows:
#         blocks.append(
#             f"👤 <b>{html.escape(r['username'])}</b>\n"
#             f"    Пол: {r['gender']} · Триал: {r['trial_used']}\n"
#             f"    Источник: {r['source']}\n"
#             f"    Контактов: {r['contacts_count']} · Сообщений: {r['messages_count']}\n"
#             f"    Последнее действие: {r['_last_action_label']} · "
#             f"Последнее сообщение от собеседника: {r['_last_incoming_label']}\n"
#             f"    Статус: {r['_status_line']}{r['_premium_line']}"
#         )
#
#     blocks.append(
#         f"📊 <b>Сводка</b>\n"
#         f"Всего: {totals['total']}\n"
#         f"С полом: {totals['with_gender']}\n"
#         f"С контактом: {totals['with_contact']}\n"
#         f"С активной реферальной Premium: {totals['with_ref_premium']}\n"
#         f"Заблокировали бота: {totals['blocked']}\n"
#         f"Отключили Автоматизацию чатов: {totals['automation_off']}\n"
#         f"Неактивных (&gt;{_INACTIVE_AFTER_DAYS} дн.): {totals['inactive']}"
#     )
#     blocks.append(
#         f"🧩 <b>Пользуются функциями</b>\n"
#         f"Ответить за меня: {totals['used_reply']}\n"
#         f"По скриншоту: {totals['used_screenshot']}\n"
#         f"Ответить с CueMe (live): {totals['used_live']}\n"
#         f"Анализ собеседника: {totals['used_deep_analysis']}\n"
#         f"Активны за 7 дней: {totals['active_7d']}\n"
#         f"Premium сейчас: {totals['premium_now']}"
#     )
#     return blocks


# Порядок столбцов CSV. Значения берутся из строк _collect_users_data по этим
# же ключам, служебные поля с "_" в выгрузку не идут.
_USERS_CSV_COLUMNS = [
    "username", "telegram_id", "gender", "source",
    "funnel_stage", "gate_status", "signup_week",
    "contacts_count", "messages_count", "blocked", "automation_off",
    "signup_date", "days_since_signup",
    "last_action", "last_action_at", "hours_since_last_action", "last_incoming_at",
    "days_since_last_active", "active_last_7d",
    "uses_reply", "uses_screenshot", "uses_live",
    "used_deep_analysis",
    "is_premium_now", "premium_source", "premium_until",
    "premium_auto_renew", "trial_used", "referrals_made",
]


def _funnel_stage(connected: bool, trial_used_flag: bool, is_premium_now: bool) -> str:
    if is_premium_now:
        return "3_premium"
    if trial_used_flag:
        return "2_триал"
    if connected:
        return "1_подключил_автоматизацию"
    return "0_старт"


def _fmt_ru_date(value: str) -> str:
    """"2026-09-13" → "13.09.2026", "2026-09-13 14:30" → "13.09.2026 14:30".
    premium_until хранится в rows отдельно от остальных дат — не через
    _csv_dt, а как datetime.isoformat() (с "T" и смещением таймзоны, см.
    _collect_users_data) — тоже разбираем, через fromisoformat.
    Пусто/нераспарсенное — "—" (в CSV те же поля остаются пустой ячейкой,
    здесь так нагляднее в таблице)."""
    if not value:
        return "—"
    try:
        if len(value) > 10:
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
            except ValueError:
                dt = datetime.fromisoformat(value)
            return dt.strftime("%d.%m.%Y %H:%M")
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return value


# HTML-отчёт /users был таблицей (одна строка на юзера, все CSV-колонки,
# сортировка кликом по заголовку) — неудобно читать, особенно с телефона.
# Заменена на карточки (см. _build_users_html ниже): по прямому запросу
# показываем только 9 полей на юзера, не весь набор CSV-колонок. Старая
# версия оставлена закомментированной — на случай отката.
# _USERS_HTML_YES = '<span class="b-yes">✅</span>'
# _USERS_HTML_NO = '<span class="b-no">❌</span>'
#
# _USERS_HTML_COLUMNS = [
#     ("Пользователь", "username"),
#     ("Telegram ID", "telegram_id"),
#     ("Пол", "gender"),
#     ("Источник", "source"),
#     ("Регистрация", "signup_date"),
#     ("Дней с рег.", "days_since_signup"),
#     ("Контактов", "contacts_count"),
#     ("Сообщений", "messages_count"),
#     ("Последнее действие", "last_action_at"),
#     ("Собеседник писал", "last_incoming_at"),
#     ("Активен (7 дн.)", "active_last_7d"),
#     ("Заблокировал бота", "blocked"),
#     ("Автоматизация выкл.", "automation_off"),
#     ("«Ответить за меня»", "uses_reply"),
#     ("«По скриншоту»", "uses_screenshot"),
#     ("Live-диалог", "uses_live"),
#     ("Анализ собеседника", "used_deep_analysis"),
#     ("Карточка стиля", "has_style_card"),
#     ("Premium", "is_premium_now"),
#     ("Источник Premium", "premium_source"),
#     ("Premium до", "premium_until"),
#     ("Осталось Premium", "premium_remaining"),
#     ("Автопродление", "premium_auto_renew"),
#     ("Триал использован", "trial_used"),
#     ("Рефералов", "referrals_made"),
# ]
#
# _USERS_HTML_DATE_KEYS = {"signup_date", "last_action_at", "last_incoming_at", "premium_until"}
# _USERS_HTML_BOOL_KEYS = {
#     "active_last_7d", "blocked", "automation_off",
#     "used_deep_analysis", "has_style_card", "is_premium_now",
# }
# _USERS_HTML_NUM_KEYS = {
#     "telegram_id", "days_since_signup", "contacts_count", "messages_count",
#     "uses_reply", "uses_screenshot", "uses_live", "trial_used", "referrals_made",
# }
#
#
# def _users_html_cell(key: str, value) -> tuple[str, str]:
#     if key in _USERS_HTML_DATE_KEYS:
#         return _fmt_ru_date(value), (value or "")
#     if key in _USERS_HTML_BOOL_KEYS:
#         return (_USERS_HTML_YES if value else _USERS_HTML_NO), ("1" if value else "0")
#     if key == "premium_auto_renew":
#         if value == "":
#             return "—", "-1"
#         return (_USERS_HTML_YES if value else _USERS_HTML_NO), ("1" if value else "0")
#     if key in _USERS_HTML_NUM_KEYS:
#         return str(value), str(value)
#     if key == "premium_remaining":
#         text = html.escape(value) if value else "—"
#         return text, (value or "")
#     text = html.escape(str(value)) if value not in (None, "") else "—"
#     return text, (str(value).lower() if value else "")
#
#
# def _build_users_html_TABLE(rows: list[dict], totals: dict) -> bytes:
#     ordered = sorted(rows, key=lambda r: r["signup_date"] or "", reverse=True)
#     head_html = "".join(f"<th>{html.escape(label)}</th>" for label, _ in _USERS_HTML_COLUMNS)
#     body_rows = []
#     for r in ordered:
#         cells = []
#         for _, key in _USERS_HTML_COLUMNS:
#             display, sort_val = _users_html_cell(key, r[key])
#             cells.append(f'<td data-sort="{html.escape(sort_val, quote=True)}">{display}</td>')
#         row_class = ' class="row-premium"' if r["is_premium_now"] else ""
#         body_rows.append(f"<tr{row_class}>{''.join(cells)}</tr>")
#     return b""  # (полное тело — таблица + JS-сортировка — в истории git)


_USERS_CARD_BADGE_YES = 'badge-yes'
_USERS_CARD_BADGE_NO = 'badge-no'


_USERS_SORT_FIELDS = [
    # (ключ поля сортировки для JS, атрибут data-*, тип, подпись кнопки)
    ("signup", "signup", "str", "Дата регистрации"),
    ("contacts", "contacts", "num", "Контактов"),
    ("messages", "messages", "num", "Сообщений"),
    ("lastincoming", "lastincoming", "str", "Собеседник писал"),
    ("referrals", "referrals", "num", "Рефералов"),
]


def _pct(n: int, d: int) -> int:
    return round(n / d * 100) if d else 0


def _bar_row(label: str, value: int, total: int, *, tone: str = "neutral", note: str = "") -> str:
    pct = _pct(value, total)
    note_html = f' <span class="bar-note">{html.escape(note)}</span>' if note else ""
    return f"""<div class="bar-row">
  <div class="bar-head"><span>{html.escape(label)}</span><span class="bar-num">{value} <span class="bar-pct">({pct}%)</span></span></div>
  <div class="bar-track"><div class="bar-fill bar-{tone}" style="width:{pct}%"></div></div>{note_html}
</div>"""


def _build_users_dashboard_html(rows: list[dict], totals: dict) -> str:
    """Агрегированный срез НАД карточками по юзерам (воронка, источники,
    Premium-по-оплате, использование фич, здоровье базы) — отвечает на
    вопрос "всё ли в порядке с ботом", который список карточек не покрывает."""
    total = totals["total"]

    funnel_steps = [
        ("🚀 Старт", total, None),
        ("🔌 Подключили автоматизацию", totals["with_contact"], total),
        ("🧪 Воспользовались триалом", totals["trial_used"], totals["with_contact"]),
        ("👑 Купили Premium", totals["premium_now"], totals["trial_used"]),
    ]
    funnel_html = []
    for i, (label, value, prev) in enumerate(funnel_steps):
        of_total = _pct(value, total)
        step_conv = f' <span class="funnel-conv">из них {_pct(value, prev)}% с прошлого шага</span>' if prev else ""
        funnel_html.append(f"""<div class="funnel-step">
  <div class="funnel-label">{html.escape(label)}</div>
  <div class="funnel-value">{value}</div>
  <div class="funnel-sub">{of_total}% от всех{step_conv}</div>
</div>""")
        if i < len(funnel_steps) - 1:
            funnel_html.append('<div class="funnel-arrow">→</div>')

    channel_rows = []
    for label, b in sorted(totals["by_source"].items(), key=lambda kv: -kv[1]["total"]):
        conv = _pct(b["premium"], b["total"])
        tone = "good" if conv >= 5 else ("warn" if conv > 0 else "bad")
        channel_rows.append(f"""<tr>
  <td>{html.escape(label)}</td>
  <td>{b['total']} <span class="muted">({_pct(b['total'], total)}%)</span></td>
  <td>{b['connected']} <span class="muted">({_pct(b['connected'], b['total'])}%)</span></td>
  <td>{b['trial_used']} <span class="muted">({_pct(b['trial_used'], b['total'])}%)</span></td>
  <td>{b['premium']}</td>
  <td><span class="chip chip-{tone}">{conv}%</span></td>
</tr>""")

    premium_source_rows = []
    for key, count in sorted(totals["premium_by_source"].items(), key=lambda kv: -kv[1]):
        label = _PREMIUM_SOURCE_LABELS.get(key, key)
        premium_source_rows.append(_bar_row(label, count, totals["premium_now"] or 1, tone="good"))

    feature_rows = "".join([
        _bar_row("«Ответить за меня»", totals["used_reply"], total, tone="good"),
        _bar_row("«По скриншоту»", totals["used_screenshot"], total, tone="good"),
        _bar_row("Live-диалог", totals["used_live"], total, tone="good"),
        _bar_row(
            "Анализ собеседника", totals["used_deep_analysis"], total, tone="bad",
            note="флагманская платная фича — если тут почти 0, стоит разбираться, почему" if totals["used_deep_analysis"] <= total * 0.02 else "",
        ),
    ])

    health_rows = "".join([
        _bar_row("Активны за 7 дней", totals["active_7d"], total, tone="good"),
        _bar_row("Заблокировали бота", totals["blocked"], total, tone="bad"),
        _bar_row("Отключили автоматизацию", totals["automation_off"], total, tone="bad"),
        _bar_row(f"Неактивны >{_INACTIVE_AFTER_DAYS} дн.", totals["inactive"], total, tone="warn"),
    ])

    return f"""<section class="dash">
  <h2>📈 Воронка</h2>
  <div class="funnel-row">{"".join(funnel_html)}</div>
</section>

<section class="dash">
  <h2>📡 Источники — объём и конверсия</h2>
  <div class="table-wrap"><table class="channel-table">
    <thead><tr>
      <th>Источник</th><th>Пришло</th><th>Подключили</th><th>Триал</th><th>Premium</th><th>Конверсия</th>
    </tr></thead>
    <tbody>{"".join(channel_rows)}</tbody>
  </table></div>
</section>

<div class="dash-cols">
  <section class="dash dash-col">
    <h2>👑 Premium — по источнику оплаты</h2>
    <div class="dash-sub">Всего сейчас: <b>{totals['premium_now']}</b> ({_pct(totals['premium_now'], total)}% от всех)</div>
    {"".join(premium_source_rows) or '<div class="muted">Пока ни у кого нет Premium</div>'}
  </section>
  <section class="dash dash-col">
    <h2>🧩 Используют фичи</h2>
    {feature_rows}
  </section>
</div>

<section class="dash">
  <h2>❤️ Здоровье базы</h2>
  {health_rows}
</section>

{_build_churn_dashboard_html(rows, totals)}
"""


_GATE_MIN_SAMPLE = 30  # меньше показавших гейт — выводы по нему рано делать
_CHURN_TOP_ACTIONS = 10


def _build_churn_dashboard_html(rows: list[dict], totals: dict) -> str:
    """Блок «Где уходят»: воронка экрана-гейта «подпишись на канал», сколько
    ушло на нём и до него, и последнее действие у ушедших — чтобы по цифрам
    решить, оставлять ли обязательную подписку. События гейта и последнее
    действие копятся только с момента их появления — до этого «—»."""
    if not totals.get("gate_tracking_start"):
        return """<section class="dash">
  <h2>🚪 Где уходят</h2>
  <div class="muted">Данных ещё нет: события экрана подписки начали записываться только
  сейчас — блок заполнится, когда юзеры начнут на него попадать.</div>
</section>"""

    def has(r, ev):
        return ev in r["_gate_events"]

    shown = [r for r in rows if has(r, "gate_shown")]
    pressed = [r for r in shown if has(r, "gate_check_ok") or has(r, "gate_check_fail")]
    ok = [r for r in shown if has(r, "gate_check_ok")]
    revealed = [r for r in shown if has(r, "gate_reveal")]
    delivered = [r for r in shown if has(r, "gate_reveal_ok")]
    fail_no_sub = [r for r in shown if r["gate_status"] == "нажал подписался, не подписан"]
    m = len(shown)
    low_sample = (
        f'<div class="bar-note">мало данных, выводы рано делать (показали меньше {_GATE_MIN_SAMPLE} юзерам)</div>'
        if m < _GATE_MIN_SAMPLE else ""
    )

    steps = [
        ("🚪 Показали экран подписки", len(shown), None),
        ("✅ Нажали «Я подписался»", len(pressed), len(shown)),
        ("📢 Подписка подтверждена", len(ok), len(pressed)),
        ("👀 Нажали «Показать»", len(revealed), len(ok)),
        ("✅ Ответ дошёл", len(delivered), len(revealed)),
    ]
    funnel = []
    for i, (label, value, prev) in enumerate(steps):
        conv = (
            f' <span class="funnel-conv">{_pct(value, prev)}% с прошлого шага · {_pct(value, m)}% от показавших</span>'
            if prev is not None else ""
        )
        funnel.append(f"""<div class="funnel-step">
  <div class="funnel-label">{html.escape(label)}</div>
  <div class="funnel-value">{value}</div>
  <div class="funnel-sub">{conv}</div>
</div>""")
        if i < len(steps) - 1:
            funnel.append('<div class="funnel-arrow">→</div>')

    gone_on_gate = [r for r in shown if r["_churned"]]
    churned = [r for r in rows if r["_churned"]]
    not_seen_locked = [r for r in rows if r["gate_status"] == "не видел" and not r["_unlocked"]]
    gone_before = [r for r in not_seen_locked if r["_churned"]]
    unknown = sum(1 for r in rows if r["gate_status"] == _GATE_STATUS_UNKNOWN)

    by_action: dict[str, int] = {}
    for r in churned:
        by_action[r["last_action"]] = by_action.get(r["last_action"], 0) + 1
    top = sorted(by_action.items(), key=lambda kv: -kv[1])
    action_rows = [
        f"<tr><td>{html.escape(label)}</td><td>{n}</td><td>{_pct(n, len(churned))}%</td></tr>"
        for label, n in top[:_CHURN_TOP_ACTIONS]
    ]
    rest = sum(n for _, n in top[_CHURN_TOP_ACTIONS:])
    if rest:
        action_rows.append(f"<tr><td>остальное</td><td>{rest}</td><td>{_pct(rest, len(churned))}%</td></tr>")
    actions_html = (
        f"""<div class="table-wrap"><table class="channel-table">
    <thead><tr><th>Последнее действие</th><th>Юзеров</th><th>% от ушедших</th></tr></thead>
    <tbody>{"".join(action_rows)}</tbody>
  </table></div>
  <div class="muted">«—» — действие неизвестно: юзер ничего не делал с момента, как действия начали записываться.</div>"""
        if churned else '<div class="muted">Пока никто не ушёл.</div>'
    )

    compare = "".join([
        _bar_row(
            "Ушли, не дойдя до экрана подписки", len(gone_before), len(not_seen_locked), tone="warn",
            note=f"из {len(not_seen_locked)} без Premium и без открытых попыток, кто экран не видел",
        ),
        _bar_row(
            "Ушли на экране подписки", len(gone_on_gate), m, tone="bad",
            note=f"из {m} увидевших",
        ),
    ])
    unknown_note = (
        f'<div class="muted">Без данных о гейте: {unknown} — последний раз были в боте до того, как это начали записывать.</div>'
        if unknown else ""
    )

    return f"""<section class="dash">
  <h2>🚪 Где уходят — экран «подпишись на канал»</h2>
  <div class="funnel-row">{"".join(funnel)}</div>
  <div class="dash-sub" style="margin-top:10px">Нажали «Я подписался», но не подписались: <b>{len(fail_no_sub)}</b></div>
  {low_sample}
</section>

<section class="dash">
  <h2>🚪 Ушли на экране подписки</h2>
  <div class="funnel-value">{len(gone_on_gate)} из {m} ({_pct(len(gone_on_gate), m)}%)</div>
  <div class="dash-sub">увидели экран, не открыли попытки и не заходили в бот больше {CHURN_HOURS} ч.</div>
  {low_sample}
</section>

<section class="dash">
  <h2>⚖️ До экрана подписки или на нём</h2>
  {compare}
  {unknown_note}
</section>

<section class="dash">
  <h2>🔚 Последнее действие у ушедших</h2>
  <div class="dash-sub">Без Premium и открытых попыток, в боте не были больше {CHURN_HOURS} ч. Всего: <b>{len(churned)}</b></div>
  {actions_html}
</section>"""


def _build_users_html(rows: list[dict], totals: dict) -> bytes:
    """Самостоятельный HTML-файл (инлайн <style>/<script>, без внешних
    зависимостей и без CDN — открывается и работает офлайн) — карточки,
    одна на юзера, вместо таблицы. Ровно 9 полей на карточку: имя, бейджи
    (Premium/подписка на канал/рефералы), пол, источник, контактов,
    сообщений, когда последний раз писал собеседник (МСК), статус Premium.
    CSV (_build_users_csv) не затронут — та же полная выгрузка, что и раньше.

    Поиск/фильтры/сортировка — целиком на JS в браузере (данные лежат в
    data-* атрибутах карточек, см. цикл ниже), сервер тут ни при чём — файл
    статический. По умолчанию (до любого взаимодействия) — сортировка по
    дате регистрации, сначала новые: и в порядке карточек в HTML (на случай
    просмотра без JS), и как начальное состояние сортировки в JS."""
    ordered = sorted(rows, key=lambda r: r["signup_date"] or "", reverse=True)

    # Источник — динамический список уникальных значений, которые реально
    # встречаются в данных (не хардкод). Пол — та же логика, но секция
    # фильтра вообще не рендерится, если поле никому не заполнено (все "?").
    sources_present = sorted({r["source"] for r in rows})
    genders_present = sorted({r["gender"] for r in rows})
    show_gender_filter = any(r["gender"] != "?" for r in rows)
    gender_display = {"?": "не указан"}

    cards = []
    for r in ordered:
        premium_badge_class = _USERS_CARD_BADGE_YES if r["is_premium_now"] else _USERS_CARD_BADGE_NO
        premium_badge_text = "👑 Premium" if r["is_premium_now"] else "Без Premium"

        sub_badge_class = _USERS_CARD_BADGE_YES if r["_is_subscribed_channel"] else _USERS_CARD_BADGE_NO
        sub_badge_text = "📢 Подписан" if r["_is_subscribed_channel"] else "📢 Не подписан"

        ref_count = r["referrals_made"]
        ref_badge_class = _USERS_CARD_BADGE_YES if ref_count else "badge-neutral"

        if r["is_premium_now"]:
            label = _PREMIUM_SOURCE_LABELS.get(r["premium_source"], r["premium_source"])
            if r["premium_until"]:
                until_dt = datetime.fromisoformat(r["premium_until"])
                suffix = " (автопродление)" if r["premium_auto_renew"] else ""
                premium_detail = (
                    f"{label}{suffix}, до {until_dt.strftime('%d.%m.%Y %H:%M')} UTC "
                    f"(осталось {r['premium_remaining']})"
                )
            else:
                premium_detail = f"{label} (дата окончания неизвестна)"
        else:
            premium_detail = "не активен"

        data_attrs = (
            f'data-name="{html.escape(r["username"].lower(), quote=True)}" '
            f'data-premium="{1 if r["is_premium_now"] else 0}" '
            f'data-subscribed="{1 if r["_is_subscribed_channel"] else 0}" '
            f'data-referrals="{ref_count}" '
            f'data-source="{html.escape(r["source"], quote=True)}" '
            f'data-gender="{html.escape(r["gender"], quote=True)}" '
            f'data-signup="{html.escape(r["signup_date"] or "", quote=True)}" '
            f'data-contacts="{r["contacts_count"]}" '
            f'data-messages="{r["messages_count"]}" '
            f'data-lastincoming="{html.escape(r["last_incoming_at"] or "", quote=True)}"'
        )

        card = f"""<div class="card" {data_attrs}>
  <div class="name">{html.escape(r['username'])}</div>
  <div class="badges">
    <span class="badge {premium_badge_class}">{premium_badge_text}</span>
    <span class="badge {sub_badge_class}">{sub_badge_text}</span>
    <span class="badge {ref_badge_class}">🎁 {ref_count} реф.</span>
  </div>
  <div class="info">
    <div class="info-row"><span class="info-label">Пол</span><span>{html.escape(r['gender'])}</span></div>
    <div class="info-row"><span class="info-label">Источник</span><span>{html.escape(r['source'])}</span></div>
    <div class="info-row"><span class="info-label">Контактов</span><span>{r['contacts_count']}</span></div>
    <div class="info-row"><span class="info-label">Сообщений</span><span>{r['messages_count']}</span></div>
    <div class="info-row"><span class="info-label">Собеседник писал</span><span>{html.escape(r['_last_incoming_msk'])}</span></div>
    <div class="info-row"><span class="info-label">Premium</span><span>{html.escape(premium_detail)}</span></div>
  </div>
</div>"""
        cards.append(card)

    source_checkboxes = "".join(
        f'<label class="chk"><input type="checkbox" class="f-source" value="{html.escape(s, quote=True)}">'
        f'<span>{html.escape(s)}</span></label>'
        for s in sources_present
    )
    gender_section = ""
    if show_gender_filter:
        gender_checkboxes = "".join(
            f'<label class="chk"><input type="checkbox" class="f-gender" value="{html.escape(g, quote=True)}">'
            f'<span>{html.escape(gender_display.get(g, g))}</span></label>'
            for g in genders_present
        )
        gender_section = f"""<div class="filter-group">
  <div class="filter-group-title">Пол</div>
  <div class="chk-row">{gender_checkboxes}</div>
</div>"""

    sort_buttons = "".join(
        f'<button type="button" class="sort-btn" data-field="{field}" data-type="{typ}">{label}</button>'
        for field, _, typ, label in _USERS_SORT_FIELDS
    )

    generated_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    # Срез по полу: верхние блоки (воронка, источники, Premium, «Где уходят»)
    # рендерятся по варианту на каждый пол, переключатель наверху показывает
    # нужный и заодно ставит тот же фильтр на карточки. Было: фильтр по полу
    # только у карточек, в свёрнутых «Фильтрах», цифры сверху — всегда по всем.
    gender_variants = [("all", "Все", rows, totals)]
    if show_gender_filter:
        for g in genders_present:
            subset = [r for r in rows if r["gender"] == g]
            label = {"парень": "Парни", "девушка": "Девушки"}.get(g, gender_display.get(g, g).capitalize())
            gender_variants.append((g, f"{label} · {len(subset)}", subset,
                                    _users_totals(subset, totals.get("gate_tracking_start"))))
    dashboard_html = "".join(
        f'<div class="dash-variant" data-g="{html.escape(key, quote=True)}"{"" if key == "all" else " hidden"}>'
        f'{_build_users_dashboard_html(sub_rows, sub_totals)}</div>'
        for key, _, sub_rows, sub_totals in gender_variants
    )
    gender_switch = ""
    if show_gender_filter:
        gender_switch = '<div class="g-switch">' + "".join(
            f'<button type="button" class="g-btn{" active" if key == "all" else ""}" data-g="{html.escape(key, quote=True)}">'
            f'{html.escape(label)}</button>'
            for key, label, _, _ in gender_variants
        ) + "</div>"
    doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Пользователи CueMe</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 16px;
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f4f5f7; color: #1b1f24;
  }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  .meta {{ color: #666; font-size: 14px; margin-bottom: 4px; }}
  .meta b {{ color: #111; }}
  .summary {{
    display: flex; flex-wrap: wrap; gap: 8px 18px;
    background: #fff; border: 1px solid #e1e4e8; border-radius: 10px;
    padding: 12px 16px; margin: 12px 0 16px; font-size: 14px;
  }}
  .summary b {{ color: #111; }}

  .dash {{
    background: #fff; border: 1px solid #e1e4e8; border-radius: 14px;
    padding: 16px 18px; margin-bottom: 14px;
  }}
  .dash h2 {{ font-size: 16px; margin: 0 0 12px; }}
  .dash-sub {{ font-size: 14px; color: #6b7280; margin-bottom: 10px; }}
  .dash-cols {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; margin-bottom: 14px; }}
  .dash-col h2 {{ margin-bottom: 10px; }}
  .muted {{ color: #6b7280; font-size: 13px; }}
  .funnel-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }}
  .funnel-step {{ background: #f4f5f7; border-radius: 12px; padding: 10px 14px; min-width: 150px; flex: 1; }}
  .funnel-label {{ font-size: 13px; color: #6b7280; margin-bottom: 4px; }}
  .funnel-value {{ font-size: 22px; font-weight: 700; }}
  .funnel-sub {{ font-size: 12px; color: #6b7280; margin-top: 2px; }}
  .funnel-conv {{ color: #1a7f37; }}
  .funnel-arrow {{ font-size: 18px; color: #c2c8d0; padding: 0 4px; }}
  .table-wrap {{ overflow-x: auto; }}
  .channel-table {{ width: 100%; border-collapse: collapse; font-size: 14px; white-space: nowrap; }}
  .channel-table th {{ text-align: left; color: #6b7280; font-weight: 600; font-size: 13px; padding: 6px 10px; border-bottom: 2px solid #f0f1f3; }}
  .channel-table td {{ padding: 8px 10px; border-bottom: 1px solid #f0f1f3; }}
  .chip {{ display: inline-block; padding: 3px 10px; border-radius: 999px; font-weight: 700; font-size: 13px; }}
  .chip-good {{ background: #d9f2df; color: #1a7f37; }}
  .chip-warn {{ background: #fef3c7; color: #92400e; }}
  .chip-bad {{ background: #fee4e2; color: #b42318; }}
  .bar-row {{ margin-bottom: 12px; }}
  .bar-row:last-child {{ margin-bottom: 0; }}
  .bar-head {{ display: flex; justify-content: space-between; font-size: 14px; margin-bottom: 4px; }}
  .bar-num {{ font-weight: 700; }}
  .bar-pct {{ font-weight: 400; color: #6b7280; }}
  .bar-track {{ background: #f0f1f3; border-radius: 999px; height: 8px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 999px; }}
  .bar-good {{ background: #1a7f37; }}
  .bar-warn {{ background: #d97706; }}
  .bar-bad {{ background: #c2272d; }}
  .bar-neutral {{ background: #6b7280; }}
  .bar-note {{ font-size: 12px; color: #92400e; }}
  .roster-heading {{ font-size: 17px; margin: 4px 0 10px; }}

  .g-switch {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 14px; }}
  .g-btn {{
    border: 1px solid #d7dbe0; background: #fff; color: #1b1f24; border-radius: 999px;
    padding: 8px 16px; font-size: 15px; cursor: pointer;
  }}
  .g-btn.active {{ background: #1b1f24; color: #fff; border-color: #1b1f24; }}
  .search-box {{
    display: block; width: 100%; padding: 14px 16px; font-size: 17px;
    border: 1px solid #d7dbe0; border-radius: 12px; margin-bottom: 12px;
    background: #fff; color: #1b1f24;
  }}
  .search-box:focus {{ outline: 2px solid #6b8fff; border-color: transparent; }}

  details.filters {{
    background: #fff; border: 1px solid #e1e4e8; border-radius: 12px;
    margin-bottom: 12px; padding: 4px 16px;
  }}
  details.filters summary {{
    padding: 12px 0; font-size: 16px; font-weight: 600; cursor: pointer;
    user-select: none; list-style: none;
  }}
  details.filters summary::-webkit-details-marker {{ display: none; }}
  details.filters summary::before {{ content: "▸ "; }}
  details.filters[open] summary::before {{ content: "▾ "; }}
  .filter-group {{ padding: 6px 0 14px; border-top: 1px solid #f0f1f3; }}
  .filter-group:first-of-type {{ border-top: none; }}
  .filter-group-title {{ font-size: 14px; color: #6b7280; margin-bottom: 8px; font-weight: 600; }}
  .chk-row {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .chk {{
    display: flex; align-items: center; gap: 8px;
    background: #f4f5f7; border: 1px solid #e1e4e8; border-radius: 10px;
    padding: 10px 14px; font-size: 15px; cursor: pointer; min-height: 44px;
    user-select: none;
  }}
  .chk input {{ width: 20px; height: 20px; flex-shrink: 0; accent-color: #1a7f37; }}
  .chk:has(input:checked) {{ background: #d9f2df; border-color: #1a7f37; }}

  .sort-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  .sort-btn {{
    background: #fff; border: 1px solid #d7dbe0; border-radius: 999px;
    padding: 10px 16px; font-size: 15px; cursor: pointer; min-height: 44px;
    color: #1b1f24; font-family: inherit;
  }}
  .sort-btn.active {{ background: #1b1f24; color: #fff; border-color: #1b1f24; }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 14px;
  }}
  .card {{
    background: #fff; border: 1px solid #e1e4e8; border-radius: 14px;
    padding: 18px; min-width: 0;
  }}
  .name {{
    font-size: 20px; font-weight: 700; margin-bottom: 10px;
    word-break: break-word;
  }}
  .badges {{
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px;
  }}
  .badge {{
    display: inline-block; padding: 5px 12px; border-radius: 999px;
    font-size: 14px; font-weight: 600; white-space: nowrap;
  }}
  .badge-yes {{ background: #d9f2df; color: #1a7f37; }}
  .badge-no {{ background: #eceef1; color: #6b7280; }}
  .badge-neutral {{ background: #eceef1; color: #6b7280; }}
  .info-row {{
    display: flex; justify-content: space-between; gap: 12px;
    padding: 8px 0; border-top: 1px solid #f0f1f3;
    font-size: 16px; line-height: 1.4;
  }}
  .info-row:first-child {{ border-top: none; }}
  .info-label {{ color: #6b7280; flex-shrink: 0; }}
  .info-row span:last-child {{ text-align: right; word-break: break-word; }}
  .empty-state {{
    background: #fff; border: 1px dashed #d7dbe0; border-radius: 12px;
    padding: 32px 16px; text-align: center; color: #6b7280; font-size: 16px;
  }}
</style>
</head>
<body>
<h1>👥 Пользователи CueMe</h1>
<div class="meta">Сформировано {generated_at} · всего {totals['total']}</div>

{gender_switch}

{dashboard_html}

<h2 class="roster-heading">📋 Все пользователи <span class="muted">(показано <b id="visibleCount">{totals['total']}</b> из {totals['total']})</span></h2>

<input type="text" class="search-box" id="searchBox" placeholder="🔍 Поиск по имени или username…">

<details class="filters">
  <summary>Фильтры</summary>
  <div class="filter-group">
    <div class="filter-group-title">Быстрые фильтры</div>
    <div class="chk-row">
      <label class="chk"><input type="checkbox" id="f-premium"><span>👑 Только Premium</span></label>
      <label class="chk"><input type="checkbox" id="f-subscribed"><span>📢 Только подписанные на канал</span></label>
      <label class="chk"><input type="checkbox" id="f-referrals"><span>🎁 Только с рефералами</span></label>
    </div>
  </div>
  <div class="filter-group">
    <div class="filter-group-title">Источник</div>
    <div class="chk-row">{source_checkboxes}</div>
  </div>
  {gender_section}
</details>

<div class="sort-row">{sort_buttons}</div>

<div class="grid" id="grid">
{"".join(cards)}
</div>
<div class="empty-state" id="emptyState" hidden>Совпадений не найдено</div>

<script>
(function () {{
  var grid = document.getElementById("grid");
  var cards = Array.prototype.slice.call(grid.querySelectorAll(".card"));
  var searchBox = document.getElementById("searchBox");
  var fPremium = document.getElementById("f-premium");
  var fSubscribed = document.getElementById("f-subscribed");
  var fReferrals = document.getElementById("f-referrals");
  var sourceBoxes = Array.prototype.slice.call(document.querySelectorAll(".f-source"));
  var genderBoxes = Array.prototype.slice.call(document.querySelectorAll(".f-gender"));
  var sortButtons = Array.prototype.slice.call(document.querySelectorAll(".sort-btn"));
  var visibleCountEl = document.getElementById("visibleCount");
  var emptyStateEl = document.getElementById("emptyState");

  var sortField = "signup";
  var sortType = "str";
  var sortAsc = false;  // по умолчанию — сначала новые (убывание)

  function checkedValues(boxes) {{
    return boxes.filter(function (b) {{ return b.checked; }}).map(function (b) {{ return b.value; }});
  }}

  function applyFilters() {{
    var query = searchBox.value.trim().toLowerCase();
    var sources = checkedValues(sourceBoxes);
    var genders = checkedValues(genderBoxes);
    var visible = 0;

    cards.forEach(function (card) {{
      var ok = true;
      if (query && card.dataset.name.indexOf(query) === -1) ok = false;
      if (ok && fPremium.checked && card.dataset.premium !== "1") ok = false;
      if (ok && fSubscribed.checked && card.dataset.subscribed !== "1") ok = false;
      if (ok && fReferrals.checked && Number(card.dataset.referrals) <= 0) ok = false;
      if (ok && sources.length && sources.indexOf(card.dataset.source) === -1) ok = false;
      if (ok && genders.length && genders.indexOf(card.dataset.gender) === -1) ok = false;
      card.hidden = !ok;
      if (ok) visible++;
    }});

    visibleCountEl.textContent = visible;
    emptyStateEl.hidden = visible !== 0;
  }}

  function applySort() {{
    cards.sort(function (a, b) {{
      var va = a.dataset[sortField] || "";
      var vb = b.dataset[sortField] || "";
      var cmp;
      if (sortType === "num") {{
        cmp = Number(va) - Number(vb);
      }} else {{
        // Пустая дата — всегда в самый конец, независимо от направления.
        if (va === "" && vb === "") cmp = 0;
        else if (va === "") cmp = 1;
        else if (vb === "") cmp = -1;
        else cmp = va < vb ? -1 : (va > vb ? 1 : 0);
      }}
      return sortAsc ? cmp : -cmp;
    }});
    cards.forEach(function (card) {{ grid.appendChild(card); }});
  }}

  function updateSortButtons() {{
    sortButtons.forEach(function (btn) {{
      var active = btn.dataset.field === sortField;
      btn.classList.toggle("active", active);
      var base = btn.textContent.replace(/ [▲▼]$/, "");
      btn.textContent = active ? base + (sortAsc ? " ▲" : " ▼") : base;
    }});
  }}

  searchBox.addEventListener("input", applyFilters);
  [fPremium, fSubscribed, fReferrals].concat(sourceBoxes, genderBoxes).forEach(function (el) {{
    el.addEventListener("change", applyFilters);
  }});
  sortButtons.forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      var field = btn.dataset.field;
      if (field === sortField) {{
        sortAsc = !sortAsc;
      }} else {{
        sortField = field;
        sortType = btn.dataset.type;
        sortAsc = false;
      }}
      updateSortButtons();
      applySort();
    }});
  }});

  // Переключатель пола наверху: показывает нужный вариант верхних блоков и
  // ставит тот же фильтр на карточки (галочки в «Фильтрах» можно менять и дальше).
  var gButtons = Array.prototype.slice.call(document.querySelectorAll(".g-btn"));
  var dashVariants = Array.prototype.slice.call(document.querySelectorAll(".dash-variant"));
  gButtons.forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      var g = btn.dataset.g;
      gButtons.forEach(function (b) {{ b.classList.toggle("active", b === btn); }});
      dashVariants.forEach(function (d) {{ d.hidden = d.dataset.g !== g; }});
      genderBoxes.forEach(function (box) {{ box.checked = g !== "all" && box.value === g; }});
      applyFilters();
    }});
  }});

  updateSortButtons();
  applyFilters();
}})();
</script>
</body>
</html>
"""
    return doc.encode("utf-8")


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
    """Раньше слал ещё и текстовую сводку кучей отдельных сообщений (по
    юзеру + шапка/сводка, порезанные на чанки по 3500 символов) — убрано по
    прямому запросу, теперь только два файла: читаемая HTML-таблица и
    подробный CSV, см. _build_users_html/_build_users_csv."""
    if not _is_admin(message.from_user.id):
        return
    rows, totals = await _collect_users_data(bot)
    if not rows:
        return

    target_chat = int(ADMIN_GROUP_CHAT_ID) if ADMIN_GROUP_CHAT_ID else message.chat.id
    # Оба файла — ВСЕ пользователи (не только активные, как раньше в тексте):
    # send_media_group шлёт их одним альбомом, одним ответом, а не двумя
    # отдельными сообщениями.
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    html_name = f"cueme_users_{date_tag}.html"
    csv_name = f"cueme_users_{date_tag}.csv"
    html_bytes = _build_users_html(rows, totals)
    csv_bytes = _build_users_csv(rows)
    try:
        await bot.send_media_group(target_chat, [
            InputMediaDocument(
                media=BufferedInputFile(html_bytes, filename=html_name),
                caption="👥 Читаемая таблица (открыть в браузере) + подробный CSV",
            ),
            InputMediaDocument(media=BufferedInputFile(csv_bytes, filename=csv_name)),
        ])
    except Exception:
        logging.warning("cmd_users: media group send failed to %s, retrying separately", target_chat)
        try:
            await bot.send_document(
                target_chat, BufferedInputFile(html_bytes, filename=html_name),
                caption="👥 Читаемая таблица (открыть в браузере)",
            )
            await bot.send_document(target_chat, BufferedInputFile(csv_bytes, filename=csv_name))
        except Exception:
            logging.warning("cmd_users: files send failed to %s", target_chat)
            await message.answer_document(BufferedInputFile(html_bytes, filename=html_name))
            await message.answer_document(BufferedInputFile(csv_bytes, filename=csv_name))


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


# ── /export [@username | id] — выгрузка переписок юзера в .zip (только админ) ─
# Обходит отсутствие SSH-доступа к серверу: файл прилетает прямо в Telegram.
# Без аргумента — список кнопок по всем юзерам; с аргументом — сразу архив
# нужного юзера.

# username юзеров в БД не хранится (см. _resolve_username) — поиск по @name
# резолвит всех через bot.get_chat, параллельно, но не больше стольких
# запросов разом, чтобы не упереться в лимиты Telegram API.
_EXPORT_RESOLVE_CONCURRENCY = 10


async def _find_user_by_username(bot: Bot, users: list, username: str) -> str | None:
    """telegram_id юзера бота с таким username (без @, без учёта регистра) или None."""
    target = username.lstrip("@").lower()
    sem = asyncio.Semaphore(_EXPORT_RESOLVE_CONCURRENCY)

    async def resolve(tid: str) -> tuple[str, str]:
        async with sem:
            return tid, await _resolve_username(bot, tid)

    resolved = await asyncio.gather(*(resolve(u["telegram_id"]) for u in users))
    for tid, who in resolved:
        if who.startswith("@") and who[1:].lower() == target:
            return tid
    return None


@dp.message(Command("export"))
async def cmd_export(message: Message, bot: Bot, command: CommandObject) -> None:
    """/export — кнопки по всем юзерам; /export @username или /export <id> —
    сразу архив этого юзера."""
    if not _is_admin(message.from_user.id):
        return
    users = list_all_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return

    arg = (command.args or "").strip()
    if arg:
        if arg.isdigit():
            telegram_id = arg if any(u["telegram_id"] == arg for u in users) else None
            not_found = f"Юзер с id {arg} не найден среди пользователей бота."
        else:
            telegram_id = await _find_user_by_username(bot, users, arg)
            not_found = f"Юзер @{arg.lstrip('@')} не найден среди пользователей бота."
        if not telegram_id:
            await message.answer(not_found)
            return
        await _send_user_export(message, bot, telegram_id)
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
    await _send_user_export(call.message, bot, telegram_id)


async def _send_user_export(message: Message, bot: Bot, telegram_id: str) -> None:
    """Собирает .zip со всеми переписками юзера (json/txt/html) и шлёт
    ответом на message. Общая для кнопки (cb_export_user) и /export с
    аргументом (cmd_export); админ-проверка — у вызывающих."""
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
        await message.answer(f"У {who} нет сохранённых переписок с сообщениями.")
        return

    buf.seek(0)
    filename = f"export_{who.lstrip('@')}.zip"
    await message.answer_document(
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
        try:
            stats = get_provider_stats()
            stats_lines = ""
            if stats:
                rows = [
                    f"• {n}: ok {s['ok']}, лимит {s['rate_limit']}, ошибок {s['error']}, ~{s['avg_ms']:.0f}мс"
                    for n in PROVIDER_NAMES if (s := stats.get(n))
                ]
                if rows:
                    stats_lines = "\n\n📊 Вызовы (с рестарта):\n" + "\n".join(rows)
        except Exception as e:
            # Админская диагностика — показываем саму ошибку, а не общую
            # заглушку «Что-то пошло не так» (так баг с _provider_stats
            # 2026-10-14 было не разглядеть без логов сервера).
            logging.exception("cmd_provider: не удалось собрать статистику")
            stats_lines = f"\n\n⚠️ Статистика не собралась: {type(e).__name__}: {str(e)[:200]}"
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


# ── /apistatus — живая загрузка провайдеров по ключам (только для админа) ────
# В отличие от /provider (общая ok/rate_limit/error статистика по провайдеру
# целиком), тут разбивка ПО КЛЮЧАМ — какой конкретно ключ заблокирован/близок
# к лимиту, плюс сырые rate-limit заголовки, если провайдер их отдаёт (см.
# llm.py: _track_response/get_provider_stats_snapshot). In-memory, с рестарта
# бота — не переживает деплой.

def _format_apistatus(snapshot: dict[tuple[str, str], dict]) -> str:
    if not snapshot:
        return "Пока нет ни одного вызова с последнего рестарта."

    by_provider: dict[str, list[tuple[str, dict]]] = {}
    for (provider, key_tail), data in snapshot.items():
        by_provider.setdefault(provider, []).append((key_tail, data))

    blocks = []
    for name in PROVIDER_NAMES:
        entries = by_provider.get(name)
        if not entries:
            continue
        lines = [f"<b>{html.escape(name)}</b>"]
        for key_tail, data in sorted(entries):
            reqs, errs = data["requests_24h"], data["errors_1h"]
            limit = KNOWN_DAILY_LIMITS.get(name)
            quota = f" (лимит {limit}/сутки)" if limit else ""
            lines.append(f"  ...{html.escape(key_tail)}: {reqs} запросов/24ч{quota}, {errs} ошибок за час")
            for h, v in data["headers"].items():
                lines.append(f"    {html.escape(h)}: {html.escape(v)}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "Пока нет ни одного вызова с последнего рестарта."


@dp.message(Command("apistatus"))
async def cmd_apistatus(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        text = "📡 Статус API по ключам (с рестарта бота):\n\n" + _format_apistatus(get_provider_stats_snapshot())
        groq_models = get_groq_model_stats()
        if groq_models:
            served = sum(s["ok"] for s in groq_models.values()) or 1
            lines = ["\n\n<b>Groq по моделям («Ответ с CueMe»)</b>"]
            for model, s in sorted(groq_models.items()):
                cd = f", ключей в кулдауне: {s['cooldown_keys']}" if s["cooldown_keys"] else ""
                lines.append(f"  {html.escape(model)}: ответила {s['ok']} ({s['ok'] * 100 // served}%), "
                             f"лимит {s['rate_limit']}{cd}")
            text += "\n".join(lines)
        invalid = get_invalid_output_counts()
        if invalid:
            lines = ["\n\n<b>Невалидные ответы (мусор/рассуждения/обрыв)</b>"]
            for prov, (hour, day) in sorted(invalid.items()):
                lines.append(f"  {html.escape(prov)}: {hour} за час, {day} за сутки")
            text += "\n".join(lines)
    except Exception as e:
        # См. cmd_provider — админу саму ошибку, не общую заглушку.
        logging.exception("cmd_apistatus: не удалось собрать статус")
        await message.answer(f"⚠️ /apistatus упал: {type(e).__name__}: {str(e)[:300]}")
        return
    await _answer_long(message, text, parse_mode="HTML")
