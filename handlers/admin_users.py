"""Админка по юзеру: /inspect, /wipe, /gift_premium.

Вынесено из main.py без изменения логики (строки 10551–10767 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import html
import json
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from storage import (
    delete_all_user_data,
    get_business_connections_history,
    get_contact_last_messages,
    get_gender,
    get_stars_premium_until,
    get_tribute_premium_until,
    mark_bot_blocked,
    list_contacts,
    record_event,
    record_gift_premium,
    set_stars_premium_until,
)
from app_state import _contact_name, _is_admin, dp
from handlers.subscription import _premium_cache
from handlers.referrals_gate import _in_premium_channel
from handlers.admin_tools import _relative_label, _resolve_username
from handlers.premium_screen import _format_until
from handlers.watch_delete import _resolve_target_id


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
