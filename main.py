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
import handlers.admin_tools  # noqa: E402
import handlers.reply  # noqa: E402
import handlers.service_cmds  # noqa: E402
import handlers.premium_screen  # noqa: E402

# Обратная совместимость: main.<имя> для тестов и ops-скриптов (import main).
# Подменять в тестах надо там, где имя вызывается, — в его модуле.
for _m in (app_state, handlers.subscription, handlers.referrals_gate, handlers.stars_tribute, handlers.admin_broadcast, handlers.redeem_menu, handlers.profile, handlers.cards, handlers.analysis, handlers.business, handlers.start_menu, handlers.admin_tools, handlers.reply, handlers.service_cmds, handlers.premium_screen,):
    globals().update({_k: _v for _k, _v in vars(_m).items() if not _k.startswith("__")})
del _m

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
