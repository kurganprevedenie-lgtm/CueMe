"""Точка входа бота CueMe. Сам код разнесён по модулям в порядке исходного
main.py (app_state, handlers/*): порядок импорта ниже = порядок регистрации
хендлеров, менять его нельзя (см. tests/test_handler_registry.py)."""
import sys

# ВРЕМЕННО, на время разнесения: модули ссылаются на ещё не вынесенный код
# через `main.имя`; при запуске `python main.py` модуль называется __main__.
sys.modules.setdefault("main", sys.modules[__name__])
import app_state  # noqa: E402
import handlers.subscription  # noqa: E402
import handlers.referrals_gate  # noqa: E402
import handlers.stars_tribute  # noqa: E402
import handlers.admin_broadcast  # noqa: E402
import handlers.redeem_menu  # noqa: E402
import handlers.profile  # noqa: E402
import handlers.cards  # noqa: E402
import handlers.analysis  # noqa: E402
import handlers.business  # noqa: E402
import handlers.start_menu  # noqa: E402

# Обратная совместимость: main.<имя> для тестов и ops-скриптов (import main).
# Подменять в тестах надо там, где имя вызывается, — в его модуле.
for _m in (app_state, handlers.subscription, handlers.referrals_gate, handlers.stars_tribute, handlers.admin_broadcast, handlers.redeem_menu, handlers.profile, handlers.cards, handlers.analysis, handlers.business, handlers.start_menu,):
    globals().update({_k: _v for _k, _v in vars(_m).items() if not _k.startswith("__")})
del _m

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
            premium_source, premium_until, premium_is_subscription = _premium_expiry_info(tid)
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

# Ручной диалог («➕ Другой человек» / первый диалог без контактов) — просим
# username вместо простого имени, чтобы потом можно было автоматически
# слить эту запись с реальным Business-контактом того же человека (см.
# _persist_business_message → find_unlinked_manual_contact_by_username /
# merge_manual_contact_into в storage.py). Если юзернейма нет — ведём себя
# как раньше, просто сохраняем как название без привязки к username
# (см. handle_unified_name → _looks_like_username).
_UNIFIED_NAME_PROMPT = (
    "Знаешь юзернейм собеседника? Пришли его (с @ или без) — тогда я смогу сам "
    "связать этот диалог с реальным контактом, если он потом подключится через "
    "автоматизацию. Если юзернейма нет — просто напиши любое имя, как раньше."
)

_USERNAME_RE = re.compile(r"^@?[a-zA-Z][a-zA-Z0-9_]{4,31}$")


def _looks_like_username(text: str) -> bool:
    """Похоже ли на валидный формат Telegram username (с @ или без)."""
    return bool(_USERNAME_RE.match(text.strip()))


async def _start_unified_reply(message: Message, state: FSMContext) -> None:
    await state.set_state(UnifiedReply.waiting_for_input)
    sent = await message.answer(
        "Перешли одно или несколько сообщений подряд или просто вставь текст "
        "переписки — соберу всё вместе"
    )
    await state.update_data(setup_chat_id=sent.chat.id, setup_message_id=sent.message_id)


@dp.callback_query(F.data == "unified_reply_restart")
async def cb_unified_reply_restart(call: CallbackQuery, state: FSMContext) -> None:
    """«👥 Ответ для другого собеседника» на результате «Ответ с CueMe»
    (variants_result_kb/live_variants_kb) — не редактирует сообщение с
    вариантами (остаётся в истории чата нетронутым), запускает флоу заново
    НОВЫМ сообщением, тот же способ входа, что и через главное меню (см.
    _start_unified_reply)."""
    await call.answer()
    await _start_unified_reply(call.message, state)


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


# Дебаунс-буфер для пачки пересланных сообщений на этапе сбора контекста
# (UnifiedReply.waiting_for_input): Telegram доставляет несколько подряд
# пересланных ОБЫЧНЫХ текстовых сообщений (не альбом) отдельными апдейтами,
# не одним пакетом — значит нельзя обрабатывать первое же сообщение, нужно
# подождать тишины. In-memory, не в БД — временное состояние одной сессии
# ввода, переживать перезапуск бота не обязано. Ключ — telegram user id (int).
_unified_input_buffer: dict[int, list[tuple[str | None, str]]] = {}
_unified_input_timers: dict[int, asyncio.Task] = {}
_UNIFIED_INPUT_DEBOUNCE_SECONDS = 2.0


def _forward_author_label(message: Message) -> str | None:
    """Имя автора пересланного сообщения по forward_origin (Bot API 7.0+) —
    None, если сообщение НЕ переслано (юзер напечатал сам, значит это его
    собственная реплика, см. _combine_unified_input)."""
    origin = message.forward_origin
    if origin is None:
        return None
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title or "Собеседник"
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title or "Канал"
    return "Собеседник"


def _combine_unified_input(buffer: list[tuple[str | None, str]]) -> str:
    """Склеивает буфер пересланных/введённых сообщений в один контекст.
    Одно сообщение без forward-метаданных (обычный случай — вставил текст
    или переслал одно сообщение) — как раньше, голый текст без префикса, не
    меняем формат самого частого пути. Несколько сообщений — каждое своей
    строкой «Автор: текст» (автор — по forward_origin, см.
    _forward_author_label), чтобы сохранить структуру диалога, а не
    склеивать всё в сплошной блок; сообщения без forward-метаданных внутри
    такой пачки — реплика от лица юзера бота («Я»)."""
    if len(buffer) == 1 and buffer[0][0] is None:
        return buffer[0][1]
    return "\n".join(f"{author or 'Я'}: {text}" for author, text in buffer)


async def _flush_unified_input(user_id: int, state: FSMContext, bot: Bot) -> None:
    """Ждёт _UNIFIED_INPUT_DEBOUNCE_SECONDS тишины от этого юзера и только
    потом обрабатывает весь накопленный буфер разом — пока не пришло новое
    сообщение (которое отменит эту задачу и запустит новую, см.
    handle_unified_input). Тело после sleep — старое тело
    handle_unified_input, без изменений в логике выбора контакта."""
    try:
        await asyncio.sleep(_UNIFIED_INPUT_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # пришло новое сообщение раньше паузы — таймер уже перезапущен им

    buffer = _unified_input_buffer.pop(user_id, None)
    _unified_input_timers.pop(user_id, None)
    if not buffer:
        return

    # Юзер мог за это время уйти из этого шага (команда меню и т.п.) —
    # буфер тогда уже не актуален, применять его некуда.
    if await state.get_state() != UnifiedReply.waiting_for_input.state:
        return

    incoming = _combine_unified_input(buffer)
    telegram_id = str(user_id)
    contacts = list_contacts(telegram_id)
    await state.update_data(pending_text=incoming)

    if not contacts:
        await state.set_state(UnifiedReply.waiting_for_name)
        await _edit_setup_message(state, bot, _UNIFIED_NAME_PROMPT)
        return

    await _edit_setup_message(state, bot, "Кому отвечаем?", reply_markup=unified_contacts_kb(contacts))


@dp.message(UnifiedReply.waiting_for_input, _not_command)
async def handle_unified_input(message: Message, state: FSMContext, bot: Bot) -> None:
    # Функция «скриншот переписки → ответ» убрана целиком (по запросу) —
    # фото в этом состоянии больше не читаем через Vision, просто просим
    # текст. Раньше здесь был branch на message.photo → extract_chat_from_image.
    # Фото не буферизуется — отдельный, немедленный ответ, вне очереди.
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

    user_id = message.from_user.id
    author = _forward_author_label(message)
    _unified_input_buffer.setdefault(user_id, []).append((author, incoming))

    prev_timer = _unified_input_timers.get(user_id)
    if prev_timer and not prev_timer.done():
        prev_timer.cancel()
    _unified_input_timers[user_id] = asyncio.create_task(_flush_unified_input(user_id, state, bot))


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
    if _looks_like_username(name):
        # Юзер прислал юзернейм, а не просто имя/метку — сохраняем отдельно
        # для будущего автослияния (см. _UNIFIED_NAME_PROMPT), display_name
        # при этом остаётся как есть (тот же текст, что юзер прислал).
        update_contact_username(contact_id, name.lstrip("@").lower())

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
        await call.message.edit_text(_UNIFIED_NAME_PROMPT)
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
    b.button(text="👥 Ответ для другого собеседника", callback_data="unified_reply_restart")
    b.adjust(1)
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
        _note_outcome("stale")
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

    # progress_msg — заведённое НАМИ сообщение-анимация, ТОЛЬКО для первой
    # генерации в диалоге (не force_fresh): фиксированный 3-секундный круг,
    # см. with_progress_animation/_REPLY_PROGRESS_CYCLE_SECONDS. «Другие
    # варианты» (force_fresh, юзер не выходит из диалога) — по явному
    # запросу БЕЗ анимации и БЕЗ ожидания цикла, результат сразу по
    # готовности LLM. None и при кэш-хите (генерации не было, крутить нечего).
    progress_msg: Message | None = None
    if variants is None:
        # Реальный вызов LLM — здесь и только здесь гейт + списание.
        replay = {"kind": "variants", "action_id": action_id, "force_fresh": force_fresh, "ctx": ctx}
        if not await _quota_gate(bot, target, str(telegram_id), replay=replay):
            return
        prev = ctx.get("variants") if force_fresh else None
        # kind всегда "reply" — "screenshot" убран вместе с функцией
        # «скриншот переписки → ответ» (screenshot_variants в llm.py
        # остался нетронутым, просто больше никем не вызывается).
        coro = suggest_reply_variants(
            text, style_card, interaction_card,
            data_signals=signals, previous_variants=prev, winning_examples=winning,
            user_gender=gender,
        )
        try:
            if force_fresh:
                variants = await coro
            else:
                _t_started, _llm_box = time.monotonic(), []
                progress_msg = await target.answer(_PROGRESS_FRAMES[0])
                variants = await with_progress_animation(
                    bot, progress_msg.chat.id, progress_msg.message_id, _timed_llm(coro, _llm_box),
                    cycle_seconds=_REPLY_PROGRESS_CYCLE_SECONDS,
                )
        except RateLimitError:
            await _finish_generation_message(
                progress_msg, target, force_fresh, "Лимит исчерпан, попробуй позже.",
            )
            _note_outcome("rate_limit")
            return
        except Exception:
            logging.exception("%s-variants: ошибка генерации", kind)
            await _finish_generation_message(
                progress_msg, target, force_fresh, "Не получилось сгенерировать варианты — попробуй ещё раз.",
            )
            _note_outcome("llm_error")
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
        await _finish_generation_message(
            progress_msg, target, force_fresh, "Не получилось сгенерировать варианты — попробуй ещё раз.",
        )
        _note_outcome("empty")
        return

    _save_shown_suggestions(str(telegram_id), ctx.get("contact_id"), kind, variants)
    ctx["variants"] = variants

    # footer_html строится ОДИН раз (при первой генерации) и живёт в ctx —
    # «Другие варианты» (force_fresh) переиспользует его как есть, см.
    # _variants_footer.
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
    await _finish_generation_message(
        progress_msg, target, force_fresh, text_out,
        reply_markup=variants_result_kb(action_id), parse_mode="HTML",
    )
    if progress_msg:
        _log_reply_timing("reply", _t_started, _llm_box)
    _note_outcome("ok")


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
    replay = {
        "kind": "reply_incoming", "incoming": incoming,
        "fsm_state": await state.get_state(), "fsm_data": data,
    }
    if not await _quota_gate(bot, message, telegram_id, edit=edit, replay=replay):
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
# Сам текст перенесён в llm.py (NEUTRAL_INTERACTION_PLACEHOLDER) — там
# suggest_reply_variants узнаёт его как «данных нет» и включает режим
# холодного старта.
_NEUTRAL_INTERACTION_PLACEHOLDER = NEUTRAL_INTERACTION_PLACEHOLDER

LIVE_NOTES_SUMMARY_EVERY = 4  # раз в сколько сообщений показывать «что я уже понял»


def _running_notes_preview(notes_text: str, n: int = 2) -> str:
    """Последние n непустых строк заметок — для короткого «что я уже понял»."""
    lines = [ln.strip() for ln in (notes_text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def live_variants_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"liveregen:{action_id}")
    b.button(text="👥 Ответ для другого собеседника", callback_data="unified_reply_restart")
    b.adjust(1)
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

    replay = {
        "kind": "live_incoming", "incoming": incoming,
        "fsm_state": await state.get_state(), "fsm_data": data,
    }
    if not await _quota_gate(bot, message, telegram_id, replay=replay):
        return

    contact_id = data.get("contact_id")
    if not contact_id:
        await message.answer("Контекст диалога потерян — начни заново через «💬 Ответ с CueMe».")
        _note_outcome("stale")
        return

    # В отличие от остальных LLM-вызовов в файле, раньше был без try/except —
    # сбой провайдера (рейтлимит/таймаут) тут тихо убивал автообработку
    # первого сообщения сразу после создания контакта: юзер видел "Готово —
    # «имя»." и дальше тишину, без единого намёка на ошибку.
    try:
        style_card = await _gen_style_card(telegram_id) or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
    except RateLimitError:
        await message.answer("Лимит запросов исчерпан — попробуй через пару минут.")
        _note_outcome("rate_limit")
        return
    except Exception:
        logging.exception("_process_live_incoming: не удалось получить стиль")
        await message.answer("Сервис сейчас перегружен — попробуй чуть позже.")
        _note_outcome("llm_error")
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
        _note_outcome("stale")
        return

    style_card = ctx["style_card"]
    running_notes = ctx.get("running_notes") or ""
    gender = get_gender(str(telegram_id))

    replay = {"kind": "live_step", "action_id": action_id, "force_fresh": force_fresh, "ctx": ctx}
    if force_fresh:
        if not await _quota_gate(bot, target, str(telegram_id), replay=replay):
            return
        # «Другие варианты» без выхода из диалога — БЕЗ анимации, ответ
        # показываем сразу по готовности LLM (по явному запросу), результат
        # заменяет target (call.message из cb_live_regen) на месте.
        try:
            variants = await suggest_reply_variants(
                text, style_card, running_notes, previous_variants=ctx.get("variants"),
                user_gender=gender,
            )
        except RateLimitError:
            await target.edit_text("Лимит исчерпан, попробуй позже.")
            _note_outcome("rate_limit")
            return
        except Exception:
            logging.exception("live-coach: ошибка регена вариантов")
            await target.edit_text("Не получилось сгенерировать варианты — попробуй ещё раз.")
            _note_outcome("llm_error")
            return
        await _charge_trial_if_needed(bot, str(telegram_id))
        try:
            record_event(str(telegram_id), "gen_live_regen", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие live-регена")
        if not variants:
            await target.edit_text("Не получилось сгенерировать варианты — попробуй ещё раз.")
            _note_outcome("empty")
            return
        _save_shown_suggestions(str(telegram_id), contact_id, "live", variants)
        ctx["variants"] = variants
        # footer_html уже посчитан при первой генерации (см. конец функции) и
        # хранится в ctx — «Другие варианты» его не пересчитывает, инсайт и
        # инструкция остаются как были, меняются только сами варианты.
        footer = ctx.get("footer_html", "")
        await _edit_or_answer_long(
            target, f"{_format_variants(variants)}\n\n{footer}",
            reply_markup=live_variants_kb(action_id), parse_mode="HTML",
        )
        _note_outcome("ok")
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

    # progress_msg — заведённое НАМИ сообщение-анимация, фиксированный
    # 3-секундный круг (первая генерация в новом диалоге, см.
    # _REPLY_PROGRESS_CYCLE_SECONDS); None при кэш-хите (генерации не было).
    # target тут почти всегда сообщение ПОЛЬЗОВАТЕЛЯ (см.
    # _process_live_incoming/handle_live_incoming), бот не может его
    # редактировать — поэтому новое сообщение, не edit.
    progress_msg: Message | None = None
    if variants is None:
        if not await _quota_gate(bot, target, str(telegram_id), replay=replay):
            return
        _t_started, _llm_box = time.monotonic(), []
        progress_msg = await target.answer(_PROGRESS_FRAMES[0])
        try:
            variants, updated_notes = await with_progress_animation(
                bot, progress_msg.chat.id, progress_msg.message_id,
                _timed_llm(live_coach_step(
                    text, style_card, running_notes or None, ctx.get("dialogue_history"),
                    user_gender=gender,
                ), _llm_box),
                cycle_seconds=_REPLY_PROGRESS_CYCLE_SECONDS,
            )
        except RateLimitError:
            await progress_msg.edit_text("Лимит исчерпан, попробуй позже.")
            _note_outcome("rate_limit")
            return
        except Exception:
            logging.exception("live-coach: ошибка генерации")
            await progress_msg.edit_text("Не получилось сгенерировать совет — попробуй ещё раз.")
            _note_outcome("llm_error")
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
        err_text = "Не получилось сгенерировать совет — попробуй ещё раз."
        await (progress_msg.edit_text(err_text) if progress_msg else target.answer(err_text))
        _note_outcome("empty")
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

    # progress_msg (реальная генерация) — анимация превращается в результат тем
    # же edit_message_text. Кэш-хит без progress_msg — прежнее поведение:
    # НОВОЕ самостоятельное сообщение (не edit фазы настройки).
    if progress_msg:
        await _edit_or_answer_long(
            progress_msg, f"{_format_variants(variants)}\n\n{footer}",
            reply_markup=live_variants_kb(action_id), parse_mode="HTML",
        )
        _log_reply_timing("live", _t_started, _llm_box)
    else:
        await _answer_long(
            target, f"{_format_variants(variants)}\n\n{footer}",
            reply_markup=live_variants_kb(action_id), parse_mode="HTML",
        )
    _note_outcome("ok")


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


# ── /help ────────────────────────────────────────────────────────────────────

async def _show_help(message: Message, edit: bool = False) -> None:
    """edit=True (из главного меню, BTN_SUPPORT/mm:support) — редактирует ТО
    ЖЕ сообщение (как «Подписка»), с «⬅️ Назад» в меню (help_kb). edit=False
    (/help, BTN_HELP по тексту со старой клавиатуры) — новое сообщение, без
    Назад (нет экрана, в который возвращаться) — только ссылка на поддержку."""
    text = (
        "Вот что я умею. На главном экране — кнопка «💬 Ответ с CueMe» плюс "
        "«🔬 Анализ собеседника», «💐 Идеальное свидание» и «👑 Подписка»:\n\n"
        "💬 Ответ с CueMe — перешли сообщение или вставь текст: если контакт "
        "уже есть — несколько вариантов ответа (Флирт/Дружески/Уверенно и "
        "т.п.); если нет — заведём новый диалог (живой коучинг с нуля)\n"
        "/reply — ответить на его сообщение\n\n"
        "<b>🔬 Анализ собеседника</b> (кнопка в меню)\n"
        "/deep_analysis — совместимость, как писать этому человеку, стиль и "
        "флаги, готовое сообщение\n\n"
        "<b>💐 Идеальное свидание</b> (кнопка в меню) — идея свидания и подарков под человека\n\n"
        "<b>💾 Исчезающие медиа</b> (Premium) — чтобы сохранить исчезающее фото/видео/"
        "кружок/голосовое, ответь на него — бот пришлёт копию сюда\n\n"
        "<b>✏️ Изменённые сообщения</b> (Premium) — если собеседник исправил "
        "сообщение, пришлю, что было и что стало\n\n"
        "<b>👑 Подписка</b> (кнопка в меню) — статус подписки + "
        f"🎁 Пригласить друга (/invite) — получить свой код, за друга по коду дадим "
        f"+{REFERRAL_REPLY_BONUS} «Ответить за меня», +{REFERRAL_ANALYSIS_BONUS} "
        f"«Анализ собеседника», +{REFERRAL_DATE_BONUS} «Идеальное свидание»\n\n"
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
        f"💎 Подпишись на {PROMO_CHANNEL_USERNAME} — бесплатно откроешь "
        f"{PROMO_CHANNEL_REPLY_BONUS} ответов, {PROMO_CHANNEL_ANALYSIS_BONUS} анализа и "
        f"{PROMO_CHANNEL_DATE_BONUS} идеи свидания; дальше — по подписке. Статус — /premium."
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=help_kb())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=support_kb())


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


def _premium_expiry_info(telegram_id: str) -> tuple[str, datetime | None, bool]:
    """(источник, until, is_subscription) — общий разбор ДЛЯ ЛЮБОГО активного
    Premium, переиспользуется и в тексте «Подписка» (_premium_expiry_line), и
    в экспорте /users (_collect_users_data): раньше там был отдельный, более
    бедный расчёт premium_source, который не проверял Stars вообще и всё, что
    не реферал/промо-канал, молча относил к Tribute — эта функция единая
    точка правды и для текста, и для экспорта.

    Порядок проверки — тот же приоритет, что уже использует _is_premium
    (Stars → Tribute-вебхук → членство в канале Tribute): источник "tribute"
    с until=None возвращается, только если НИКАКОГО активного окна нет
    вообще — это ПОДРАЗУМЕВАЕТ, что _is_premium(telegram_id) уже True
    (иначе Premium вообще нет, вызывать не нужно), а значит юзер состоит в
    канале, просто дата окончания неизвестна (оплатил ДО появления
    Tribute-вебхука — see tribute_premium_until). Если вебхук подтверждал
    оплату — until известен и "tribute" возвращается с реальной датой.
    is_subscription — True только для Stars-тарифа "месяц" (нативная
    подписка с автопродлением), иначе False.

    Источники "referral"/"promo_channel" раньше тоже были веткой здесь —
    убраны 2026-10 вместе с _has_referral_premium/_has_promo_channel_premium
    из _is_premium (замена на бонус попытками, см. комментарий там): они
    никогда не вернутся, т.к. эта функция вызывается только когда
    _is_premium уже True. Не удалены физически."""
    now = datetime.now(timezone.utc)

    # until = get_deep_analysis_free_until(telegram_id)
    # if until and until > now:
    #     return "referral", until, False
    #
    # until = get_promo_channel_premium_until(telegram_id)
    # if until and until > now:
    #     return "promo_channel", until, False

    until = get_stars_premium_until(telegram_id)
    if until and until > now:
        payment = get_latest_star_payment(telegram_id)
        tribute_until = get_tribute_premium_until(telegram_id)
        if payment and payment["tier"] == "gift":
            # Подарок поверх Tribute, который кончается позже, — срок Tribute.
            if tribute_until and tribute_until > until:
                return "tribute", tribute_until, False
            return "gift", until, False
        return "stars", until, bool(payment and payment["is_subscription"])

    until = get_tribute_premium_until(telegram_id)
    if until and until > now:
        return "tribute", until, False

    return "tribute", None, False


def _premium_expiry_line(telegram_id: str) -> str:
    """Строка «сколько осталось + способ оплаты» для активного Premium —
    для пользователя (текст под кнопкой «Подписка»)."""
    now = datetime.now(timezone.utc)
    source, until, is_subscription = _premium_expiry_info(telegram_id)

    # "referral"/"promo_channel" никогда не вернутся из _premium_expiry_info
    # (см. комментарий там) — ветки оставлены закомментированными.
    # if source == "referral":
    #     return (
    #         f"🎁 Реферальная награда — действует до {_format_until(until)} "
    #         f"(осталось {_format_remaining(until - now)})."
    #     )
    # if source == "promo_channel":
    #     return (
    #         f"📢 Награда за подписку на канал — действует до {_format_until(until)} "
    #         f"(осталось {_format_remaining(until - now)})."
    #     )
    if source == "gift":
        return (
            f"🎁 Подарочный Premium — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )
    if source == "stars":
        if is_subscription:
            return (
                f"⭐ Stars-подписка (автопродление) — следующее списание "
                f"{_format_until(until)} (через {_format_remaining(until - now)}). "
                "Отменить — в Telegram: Настройки → Мои подписки."
            )
        return (
            f"⭐ Оплачено Stars — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )

    # source == "tribute": until известен, если оплата подтверждена вебхуком
    # (см. tribute_premium_until/_check_premium_expiry_reminders) — иначе
    # None (оплатил до появления вебхука, точная дата неизвестна боту).
    if until:
        return (
            f"💎 Подписка Tribute — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)}). Продление и отмена — на стороне Tribute."
        )
    return (
        "💎 Подписка оформлена через Tribute — продление и отмена на их "
        "стороне, точную дату окончания бот не знает."
    )


# Что входит в Premium — один список на все экраны, чтобы не расходился;
# показывается цитатой (HTML blockquote) под строкой статуса «Подписки».
# «🔄 Авто-режим» не включён: /auto и авто-переписка убраны из бота.
# (Пробовали оформить цитатой весь экран и убрать кнопки оплаты у
# подписчика — откатили по запросу: цитатой только этот список.)
_PREMIUM_FEATURES_HTML = (
    "<blockquote><b>Что входит:</b>\n"
    "💬 Ответ с CueMe — без лимита\n"
    "🔬 Анализ собеседника — совместимость, флаги, готовое сообщение\n"
    "💡 Идеальное свидание — куда позвать и как подать\n"
    "🗑 Удалённые сообщения — текст, фото, видео, кружки, голосовые</blockquote>"
)


async def _premium_status_text(bot: Bot, telegram_id: str) -> str:
    if await _is_premium(bot, telegram_id):
        expiry_line = html.escape(_premium_expiry_line(telegram_id))
        return (
            "👑 Подписка:\n\n✅ Активна — весь функционал CueMe без ограничений.\n\n"
            f"{_PREMIUM_FEATURES_HTML}\n\n{expiry_line}"
        )

    # Было «осталось N из FREE_TRIAL_REQUESTS» + «БЕСПЛАТНУЮ подписку — за
    # друга»: базового лимита больше нет, а друг теперь даёт попытки, не
    # подписку — оба утверждения стали неверными.
    footer = (
        f"Ещё попытки — за каждого друга: +{REFERRAL_REPLY_BONUS} / "
        f"+{REFERRAL_ANALYSIS_BONUS} / +{REFERRAL_DATE_BONUS}, раздел 👥 Реферальная система.\n\n"
        "Оплатил(а)? Нажми «✅ Я оплатил(а) — проверить»."
    )
    if _never_unlocked(telegram_id, is_premium=False):
        claim_line = (
            ""
            if has_claimed_promo_reward(telegram_id)
            else (
                f"🎁 Подпишись на {PROMO_CHANNEL_USERNAME} — и бесплатно откроешь "
                f"{PROMO_CHANNEL_REPLY_BONUS} «Ответить за меня», "
                f"{PROMO_CHANNEL_ANALYSIS_BONUS} «Анализа собеседника», "
                f"{PROMO_CHANNEL_DATE_BONUS} «Идеальных свидания».\n\n"
            )
        )
        return f"👑 Подписка:\n\n❌ Не активна\n\n{_PREMIUM_FEATURES_HTML}\n\n{html.escape(claim_line + footer)}"

    reply_left = max(0, get_reply_trial_bonus(telegram_id) - get_trial_used(telegram_id))
    analysis_left = max(0, get_analysis_trial_bonus(telegram_id) - get_analysis_trial_count(telegram_id))
    date_left = max(0, get_date_trial_bonus(telegram_id) - get_date_trial_count(telegram_id))
    if not (reply_left or analysis_left or date_left):
        left_block = (
            "⏳ Бесплатные попытки закончились — но, похоже, тебе заходит 😏\n"
            "Дальше по подписке — весь функционал без ограничений.\n\n"
        )
    else:
        left_block = (
            "⏳ Осталось бесплатно:\n"
            f"💬 «Ответить за меня» — {reply_left}\n"
            f"🔬 «Анализ собеседника» — {analysis_left}\n"
            f"💡 «Идеальное свидание» — {date_left}\n\n"
        )
    return f"👑 Подписка:\n\n❌ Не активна\n\n{_PREMIUM_FEATURES_HTML}\n\n{html.escape(left_block + footer)}"


@dp.message(Command("premium"))
async def cmd_premium(message: Message, bot: Bot) -> None:
    text = await _premium_status_text(bot, str(message.from_user.id))
    await message.answer(text, reply_markup=paywall_kb(), parse_mode="HTML")
    await _offer_pending_reveals(bot, str(message.from_user.id))
    _start_payment_watch(bot, str(message.from_user.id))


async def _show_premium_screen(target: Message, bot: Bot, telegram_id: str, edit: bool = False) -> None:
    text = await _premium_status_text(bot, telegram_id)
    if edit:
        await target.edit_text(text, reply_markup=premium_menu_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=premium_menu_kb(), parse_mode="HTML")
    _start_payment_watch(bot, telegram_id)
    # Premium уже активен, а скрытые удалённые не показаны (тизер мог
    # потеряться) — предложить показать (сама функция проверит и то, и другое).
    await _offer_pending_reveals(bot, telegram_id)


@dp.callback_query(F.data == "show_premium")
async def cb_show_premium(call: CallbackQuery, bot: Bot) -> None:
    """Тоже служит «⬅️ Назад» из «👥 Реферальная система» в «👑 Подписка» —
    редактирует то же сообщение (invite_kb() ведёт сюда же)."""
    await call.answer()
    await _show_premium_screen(await _menu_screen_target(call.message), bot, str(call.from_user.id), edit=True)


@dp.callback_query(F.data == "show_invite")
async def cb_show_invite(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_invite(call.message, bot, str(call.from_user.id), edit=True)


@dp.callback_query(F.data == "sub:to_menu")
async def cb_sub_to_main_menu(call: CallbackQuery) -> None:
    """«⬅️ Назад» с экрана «👑 Подписка» — в главное меню, тем же сообщением."""
    await call.answer()
    await _send_main_menu(call.message, edit=True)


# ── 🗑 Удалённые сообщения — настройки трёх Premium-функций слежки ──────────
# Все три по умолчанию включены. Без Premium экран виден, но переключатели
# заблокированы (тап — подсказка про Premium), а сами функции присылают
# только тизеры. Проверки настроек — в handle_deleted_business_messages,
# handle_edited_business_message и _save_ephemeral_reply_media.

_WATCH_ITEMS = (
    ("watch_deleted", "Удалённые сообщения"),
    ("watch_edited", "Изменённые сообщения"),
    ("watch_ephemeral", "Сохранение исчезающих (с таймером)"),
)

_WATCH_TEXT = (
    "🗑 Удалённые сообщения\n\n"
    "Что присылать тебе из переписок:\n"
    "• удалённые собеседником сообщения\n"
    "• изменённые — что было и что стало\n"
    "• исчезающие фото, видео, кружки и голосовые — ответь на такое "
    "сообщение, и я пришлю копию\n\n"
    "Нажми на пункт, чтобы включить или выключить."
)


def _watch_kb(telegram_id: str, is_premium: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label in _WATCH_ITEMS:
        if is_premium:
            mark = "✅" if get_watch_setting(telegram_id, key) else "❌"
            b.button(text=f"{mark} {label}", callback_data=f"watch:toggle:{key}")
        else:
            b.button(text=f"🔒 {label}", callback_data="watch:locked")
    if not is_premium:
        b.button(text="👑 Оформить Premium", callback_data="show_premium")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


async def _show_watch_screen(target: Message, bot: Bot, telegram_id: str) -> None:
    is_premium = await _is_premium(bot, telegram_id)
    text = _WATCH_TEXT if is_premium else f"{_WATCH_TEXT}\n\n🔒 Доступно с Premium."
    await target.edit_text(text, reply_markup=_watch_kb(telegram_id, is_premium))


@dp.callback_query(F.data == "watch:menu")
async def cb_watch_menu(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_watch_screen(await _menu_screen_target(call.message), bot, str(call.from_user.id))


@dp.callback_query(F.data == "watch:locked")
async def cb_watch_locked(call: CallbackQuery) -> None:
    await call.answer("Настройки доступны с Premium", show_alert=True)


@dp.callback_query(F.data.startswith("watch:toggle:"))
async def cb_watch_toggle(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    key = call.data.split(":", 2)[2]
    if key not in WATCH_SETTINGS or not await _is_premium(bot, telegram_id):
        await call.answer("Настройки доступны с Premium", show_alert=True)
        return
    enabled = await asyncio.to_thread(toggle_watch_setting, telegram_id, key)
    await call.answer("Включено" if enabled else "Выключено")
    await _show_watch_screen(call.message, bot, telegram_id)


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
    message: Message, bot: Bot, command: str, arg: str | None = None, usage: str | None = None,
) -> tuple[str, str] | None:
    """Разбирает "<telegram_id>" или "@username" из текста команды — общий
    парсинг для /wipe и /inspect. Возвращает (target_id, arg_display) или
    None (сообщение об ошибке уже отправлено). arg — когда у команды есть
    другие аргументы (/gift_premium), цель передаётся уже вырезанной."""
    if arg is None:
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip() if len(parts) == 2 else ""
    usage = usage or f"Использование: {command} <telegram_id> или {command} @username"
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


# ── /gift_premium — подарить N дней Premium (только для админа) ─────────────
# Срок кладётся в stars_premium_until: _is_premium проверяет его первым, а
# вебхук Tribute пишет в своё поле и его не трогает. Подарок отмечается
# строкой star_payments(tier='gift') — по ней напоминание и статус знают,
# что это подарок (_premium_expiry_info → "gift").

_GIFT_USAGE = ("Использование: /gift_premium <@username|telegram_id> <дней> [текст сообщения]\n"
               "Без текста уйдёт стандартное «🎁 Мы подарили тебе N дней Premium…».")
_GIFT_MAX_DAYS = 365
_pending_gifts: dict[str, tuple[str, int, str | None]] = {}  # admin_id → (кому, дней, текст)


def _gift_default_text(days: int, gender: str | None) -> str:
    described = "описала" if gender == "female" else "описал"
    return (f"🎁 Мы подарили тебе {days} {_plural_days(days)} Premium — спасибо, что подробно "
            f"{described} проблему, и извини за неудобства. Всё починим.")


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def _gift_new_until(telegram_id: str, days: int, now: datetime | None = None) -> datetime:
    """Новый срок: от max(сейчас, текущий stars_premium_until) + days."""
    now = now or datetime.now(timezone.utc)
    current = get_stars_premium_until(telegram_id)
    base = current if current and current > now else now
    return base + timedelta(days=days)


def _apply_gift_premium(admin_id: str, telegram_id: str, days: int) -> datetime:
    until = _gift_new_until(telegram_id, days)
    set_stars_premium_until(telegram_id, until)
    record_gift_premium(telegram_id, until)
    _premium_cache.pop(telegram_id, None)
    record_event(telegram_id, "gift_premium", json.dumps(
        {"by": admin_id, "days": days, "until": until.isoformat()}, ensure_ascii=False))
    return until


def _fmt_until_or_dash(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "—"
    state = "активен" if dt > now else "истёк"
    return f"{_format_until(dt)} ({state})"


@dp.message(Command("gift_premium"))
async def cmd_gift_premium(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[2].isdigit() or not 1 <= int(parts[2]) <= _GIFT_MAX_DAYS:
        await message.answer(_GIFT_USAGE + f"\nДней — от 1 до {_GIFT_MAX_DAYS}.")
        return
    resolved = await _resolve_target_id(message, bot, "/gift_premium", arg=parts[1], usage=_GIFT_USAGE)
    if resolved is None:
        return
    target_id, arg = resolved
    days = int(parts[2])
    custom = parts[3].strip() if len(parts) == 4 and parts[3].strip() else None

    now = datetime.now(timezone.utc)
    in_channel = await _in_premium_channel(bot, target_id)
    channel = {True: "да", False: "нет", None: "не проверить"}[in_channel]
    new_until = _gift_new_until(target_id, days, now)
    text = custom or _gift_default_text(days, get_gender(target_id))
    _pending_gifts[str(message.from_user.id)] = (target_id, days, custom)

    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data=f"gift:yes:{target_id}:{days}")
    b.button(text="Отмена", callback_data="gift:no")
    await message.answer(
        f"🎁 Подарить <b>{days} {_plural_days(days)}</b> Premium — {html.escape(arg)}?\n\n"
        f"Сейчас:\n"
        f"• Stars/подарок: {_fmt_until_or_dash(get_stars_premium_until(target_id), now)}\n"
        f"• Tribute (вебхук): {_fmt_until_or_dash(get_tribute_premium_until(target_id), now)}\n"
        f"• В канале Tribute: {channel}\n\n"
        f"Станет (Stars/подарок): до {_format_until(new_until)}\n\n"
        f"Сообщение пользователю:\n<blockquote>{html.escape(text)}</blockquote>",
        parse_mode="HTML", reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("gift:"))
async def cb_gift_premium(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    admin_id = str(call.from_user.id)
    if call.data == "gift:no":
        _pending_gifts.pop(admin_id, None)
        await call.answer("Отменено")
        await call.message.edit_text("Подарок отменён.")
        return

    _, _, target_id, days_s = call.data.split(":")
    days = int(days_s)
    pending = _pending_gifts.get(admin_id)
    if pending is None or pending[:2] != (target_id, days):
        # бот перезапускался или это кнопка от другой команды — текст мог потеряться
        await call.answer("Запрос устарел — повтори команду", show_alert=True)
        return
    _pending_gifts.pop(admin_id, None)
    await call.answer("Выдаю")

    until = _apply_gift_premium(admin_id, target_id, days)
    text = pending[2] or _gift_default_text(days, get_gender(target_id))
    try:
        await bot.send_message(int(target_id), text)
        delivered = "✉️ Сообщение доставлено."
    except TelegramForbiddenError:
        mark_bot_blocked(target_id)
        delivered = "⚠️ Сообщение НЕ доставлено: пользователь заблокировал бота. Premium всё равно выдан."
    except Exception as e:
        logging.warning("gift_premium: не удалось уведомить %s: %s", target_id, e)
        delivered = f"⚠️ Сообщение НЕ доставлено ({type(e).__name__}). Premium всё равно выдан."
    await call.message.edit_text(
        f"✓ Подарено {days} {_plural_days(days)} Premium пользователю {target_id} — "
        f"до {_format_until(until)}.\n{delivered}"
    )


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
            "GROQ_API_KEY не задан — распознавание голоса пойдёт только "
            "через Gemini-fallback."
        )
    logging.info("Конфиг проверен. Доступные LLM-ключи: %s", ", ".join(present))


_INVALID_ALERT_THRESHOLD = 5  # невалидных за час от одного провайдера → алерт в группу
_invalid_alerted_at: dict[str, float] = {}


def _make_invalid_output_hook(bot: Bot):
    """Хук для llm.set_invalid_output_hook: событие gen_invalid_output и, если
    у провайдера за час больше _INVALID_ALERT_THRESHOLD невалидных, — одно
    сообщение в ADMIN_GROUP_CHAT_ID (не чаще раза в час на провайдера)."""
    def hook(provider: str, tag: str, reason: str) -> None:
        try:
            record_event(None, "gen_invalid_output", json.dumps(
                {"provider": provider, "tag": tag, "reason": reason}, ensure_ascii=False))
        except Exception:
            logging.exception("gen_invalid_output: не удалось записать событие")
        base = provider.split(" ", 1)[0]
        hour, _ = get_invalid_output_counts().get(base, (0, 0))
        now = time.time()
        if ADMIN_GROUP_CHAT_ID and hour > _INVALID_ALERT_THRESHOLD and now - _invalid_alerted_at.get(base, 0) > 3600:
            _invalid_alerted_at[base] = now
            asyncio.get_running_loop().create_task(bot.send_message(
                int(ADMIN_GROUP_CHAT_ID),
                f"⚠️ {base}: {hour} невалидных ответов за час (последний — {tag}: {reason}). "
                "Бот их отбраковывает и идёт к следующему провайдеру, но стоит посмотреть.",
            ))
    return hook


async def main() -> None:
    _validate_startup_config()
    init_db()
    bot = Bot(token=BOT_TOKEN)
    set_invalid_output_hook(_make_invalid_output_hook(bot))
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
    asyncio.create_task(_watch_event_loop_lag())
    await _start_tribute_webhook_server(bot)  # no-op, если TRIBUTE_API_KEY не задан
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
