"""👑 Подписка: экран статуса, награда за промо-канал, оплата Telegram Stars
и навигация внутри раздела (edit-in-place одного сообщения).

Tribute-подписка (приватный канал-пропуск) проверяется в handlers/common.py
(_is_premium) — здесь только экраны и параллельные способы оплаты.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher. Платёжные
хендлеры вынесены в ОТДЕЛЬНЫЙ payments_router, который main.py подключает
первым: апдейт об оплате не должен перехватываться состояниями ввода
(см. комментарий о порядке роутеров в main.py).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    FREE_TRIAL_REQUESTS,
    PROMO_CHANNEL_REWARD_DAYS,
    PROMO_CHANNEL_USERNAME,
    STARS_SUBSCRIPTION_PERIOD,
    STAR_PRICE_DAY,
    STAR_PRICE_WEEK,
    STAR_PRICE_MONTH,
)
from handlers.common import (
    _format_remaining,
    _format_until,
    _is_premium,
    _premium_expiry_info,
    _send_main_menu,
    paywall_kb,
    premium_menu_kb,
    stars_tariff_kb,
)
from handlers.referral import _show_invite
from storage import (
    get_promo_channel_pause,
    get_promo_channel_premium_until,
    get_trial_used,
    get_users_with_active_promo_premium,
    has_claimed_promo_reward,
    pause_promo_channel_premium,
    record_event,
    record_star_payment,
    resume_promo_channel_premium,
    set_promo_channel_reward,
    set_stars_premium_until,
)

router = Router(name="subscription")
payments_router = Router(name="payments")


# ── Награда за подписку на промо-канал (ТРЕТИЙ бесплатный путь к Premium) ────
# PROMO_CHANNEL_USERNAME — ПУБЛИЧНЫЙ промо-канал (t.me/CueMee), НЕ путать с
# PREMIUM_CHANNEL_ID (приватный канал-пропуск Tribute, платный, отдельная
# механика в _is_premium). Награда — ОДИН раз за всё время: has_claimed_promo_reward
# навсегда true после первого начисления, отписка-подписка заново не даёт дубль.
# Подписка проверяется автоматически — через chat_member-апдейты
# (on_promo_channel_membership_change) + суточную сверку на случай пропущенного
# апдейта (_reconcile_promo_channel_premium) — плюс ручная кнопка «✅ Я
# подписался» (cb_promo_check) как подстраховка на случай, если Telegram не
# прислал chat_member-апдейт (бот был офлайн и т.п.) — единственный путь,
# которым тогда можно было бы получить награду.

def _promo_back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="show_premium")
    return b.as_markup()


def _promo_offer_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📢 Открыть канал", url=f"https://t.me/{PROMO_CHANNEL_USERNAME.lstrip('@')}")
    b.button(text="✅ Я подписался", callback_data="promo:check")
    b.button(text="⬅️ Назад", callback_data="show_premium")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "promo:offer")
async def cb_promo_offer(call: CallbackQuery) -> None:
    """Экран внутри «👑 Подписка» — редактирует ТО ЖЕ сообщение (как и
    остальные переходы в этом разделе, см. _show_premium_screen/_show_invite),
    не шлёт отдельное. Подписка проверяется автоматически
    (on_promo_channel_membership_change) — награда и подтверждение придут
    отдельным сообщением, когда бот это увидит; «✅ Я подписался»
    (cb_promo_check) — подстраховка, если это событие почему-то не пришло."""
    await call.answer()
    if has_claimed_promo_reward(str(call.from_user.id)):
        await call.message.edit_text("Ты уже получал эту награду раньше 🙂", reply_markup=_promo_back_kb())
        return
    await call.message.edit_text(
        f"Подпишись на {PROMO_CHANNEL_USERNAME} и получи "
        f"{PROMO_CHANNEL_REWARD_DAYS} дня Premium бесплатно. Проверяем автоматически, "
        "но если через пару минут ничего не пришло — жми «✅ Я подписался».",
        reply_markup=_promo_offer_kb(),
    )


@router.callback_query(F.data == "promo:check")
async def cb_promo_check(call: CallbackQuery, bot: Bot) -> None:
    """Ручная проверка — подстраховка на случай, если Telegram не прислал
    chat_member-апдейт (on_promo_channel_membership_change) вовремя. Реально
    проверяет членство через bot.get_chat_member (anti-abuse), не просто
    верит нажатию. Редактирует ЭТОТ ЖЕ экран «Подписка» (как остальные
    переходы в разделе), не шлёт новое сообщение — в отличие от
    on_promo_channel_membership_change, у которой юзер асинхронно мог уже
    уйти в другой раздел меню, тут это тот же самый клик, тот же экран."""
    telegram_id = str(call.from_user.id)

    try:
        member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
        subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        logging.exception("promo channel check failed for %s", telegram_id)
        subscribed = False

    if not subscribed:
        await call.answer("Не вижу подписки — проверь и попробуй снова", show_alert=True)
        return

    remaining_seconds, _ = get_promo_channel_pause(telegram_id)
    if remaining_seconds:
        until = datetime.now(timezone.utc) + timedelta(seconds=remaining_seconds)
        resume_promo_channel_premium(telegram_id, until)
        await call.answer()
        await call.message.edit_text(
            f"🎉 С возвращением! Оставшееся время Premium возобновлено — до {_format_until(until)}.",
            reply_markup=_promo_back_kb(),
        )
        return

    if has_claimed_promo_reward(telegram_id):
        await call.answer("Уже получено раньше", show_alert=True)
        return

    until = datetime.now(timezone.utc) + timedelta(days=PROMO_CHANNEL_REWARD_DAYS)
    set_promo_channel_reward(telegram_id, until)
    await call.answer()
    await call.message.edit_text(
        f"✅ Готово! Ты подписан(а), Premium активен на {PROMO_CHANNEL_REWARD_DAYS} "
        f"дня (до {_format_until(until)}).",
        reply_markup=_promo_back_kb(),
    )


@router.chat_member()
async def on_promo_channel_membership_change(event: ChatMemberUpdated, bot: Bot) -> None:
    """Автоматическая проверка подписки на промо-канал (PROMO_CHANNEL_USERNAME) —
    основной путь, плюс суточная сверка на случай пропущенного апдейта
    (_reconcile_promo_channel_premium, но она только приостанавливает —
    новую награду не выдаёт) и ручная кнопка «✅ Я подписался» (cb_promo_check)
    как подстраховка. Требует прав администратора бота в этом канале —
    иначе chat_member-апдейты по нему не приходят. При is_member=True —
    три случая:
    1) юзер на паузе (был Premium, отписался, теперь снова подписан) —
       возобновляем с сохранённого остатка, БЕЗ новой выдачи награды;
    2) награда уже выдавалась когда-либо (claimed=True, паузы нет) — второй
       раз не даём (has_claimed_promo_reward — разовый anti-abuse флаг), тихо;
    3) первая выдача — начисляем PROMO_CHANNEL_REWARD_DAYS, ставим claimed,
       уведомляем НОВЫМ сообщением (не правкой экрана «Подписка» — юзер к
       этому моменту мог уйти в другой раздел меню)."""
    channel_username = PROMO_CHANNEL_USERNAME.lstrip("@").lower()
    if (event.chat.username or "").lower() != channel_username:
        return

    telegram_id = str(event.new_chat_member.user.id)
    was_member = event.old_chat_member.status in ("member", "administrator", "creator")
    is_member = event.new_chat_member.status in ("member", "administrator", "creator")
    if was_member == is_member:
        return

    if not is_member:
        until = get_promo_channel_premium_until(telegram_id)
        if not until:
            return
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            pause_promo_channel_premium(telegram_id, int(remaining))
            await _notify_promo_premium_paused(bot, telegram_id)
        return

    remaining_seconds, _ = get_promo_channel_pause(telegram_id)
    if remaining_seconds:
        until = datetime.now(timezone.utc) + timedelta(seconds=remaining_seconds)
        resume_promo_channel_premium(telegram_id, until)
        await _notify_promo_premium_resumed(bot, telegram_id, until)
        return

    if has_claimed_promo_reward(telegram_id):
        return

    until = datetime.now(timezone.utc) + timedelta(days=PROMO_CHANNEL_REWARD_DAYS)
    set_promo_channel_reward(telegram_id, until)
    await _notify_promo_reward_granted(bot, telegram_id, until)


_PROMO_PAUSED_TEXT = (
    "Очень жаль, что вы отписались от нашего канала 😔 К сожалению, мы "
    "приостановили ваш пробный Premium-период. Чтобы возобновить его — "
    "подпишитесь обратно, и оставшееся время вернётся."
)


async def _notify_promo_premium_paused(bot: Bot, telegram_id: str) -> None:
    """Уведомляет юзера о приостановке промо-Premium из-за отписки от канала.
    Молча глотает ошибку отправки (юзер мог заблокировать бота) — постановка
    на паузу уже применена и не зависит от того, дошло ли уведомление."""
    try:
        await bot.send_message(int(telegram_id), _PROMO_PAUSED_TEXT)
    except Exception:
        logging.warning("promo premium pause notify failed: telegram_id=%s", telegram_id)


async def _notify_promo_reward_granted(bot: Bot, telegram_id: str, until: datetime) -> None:
    """Уведомляет о ПЕРВОЙ выдаче промо-Premium после автоматически
    подтверждённой подписки — отдельным НОВЫМ сообщением (не правкой экрана
    «Подписка»: асинхронное событие, юзер мог уже уйти в другой раздел
    меню). Молча глотает ошибку отправки — награда уже начислена
    независимо от того, дошло ли уведомление."""
    text = (
        f"✅ Готово! Ты подписан(а), Premium активен на {PROMO_CHANNEL_REWARD_DAYS} "
        f"дня (до {_format_until(until)})."
    )
    try:
        await bot.send_message(int(telegram_id), text)
    except Exception:
        logging.warning("promo reward grant notify failed: telegram_id=%s", telegram_id)


async def _notify_promo_premium_resumed(bot: Bot, telegram_id: str, until: datetime) -> None:
    """Уведомляет о возобновлении промо-Premium после повторной подписки
    (юзер был на паузе) — тоже отдельное новое сообщение."""
    text = f"🎉 С возвращением! Оставшееся время Premium возобновлено — до {_format_until(until)}."
    try:
        await bot.send_message(int(telegram_id), text)
    except Exception:
        logging.warning("promo premium resume notify failed: telegram_id=%s", telegram_id)


async def _reconcile_promo_channel_premium(bot: Bot) -> None:
    """Суточная подстраховка на случай пропущенного chat_member-события
    (например, бот был офлайн): проходит по всем юзерам с активным окном
    промо-Premium, явно перепроверяет членство через bot.get_chat_member и
    ставит на паузу тех, кто уже не подписан, а событие поймать не удалось.
    Отдельного планировщика (cron/APScheduler) в проекте нет — это обычный
    фоновый asyncio-таск, запускается один раз из main()."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            for telegram_id in get_users_with_active_promo_premium():
                until = get_promo_channel_premium_until(telegram_id)
                if not until or until <= datetime.now(timezone.utc):
                    continue
                try:
                    member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
                    subscribed = member.status in ("member", "administrator", "creator")
                except Exception:
                    continue
                if subscribed:
                    continue
                remaining = (until - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    pause_promo_channel_premium(telegram_id, int(remaining))
                    await _notify_promo_premium_paused(bot, telegram_id)
        except Exception:
            logging.exception("promo channel reconciliation pass failed")


# ── Stars-подписка (Telegram Stars, XTR) ──────────────────────────────────────
# Второй, независимый способ оплаты рядом с Tribute — НЕ канал-пропуск: Stars
# просто открывает окно users.stars_premium_until (см. _has_stars_premium),
# без членства в PREMIUM_CHANNEL_ID. День/неделя — разовая покупка (Telegram
# не поддерживает Stars-подписки короче 30 дней), продлевать нужно вручную —
# бот предложит это через _premium_status_text/paywall, когда окно истекло.
# Месяц — нативная Stars-подписка (subscription_period=STARS_SUBSCRIPTION_PERIOD,
# ровно 30 дней — единственное значение, которое принимает Telegram) с
# автопродлением; списывается Telegram-ом самостоятельно, отменяется
# пользователем через настройки Telegram, не через бота.

_STARS_TIERS = {
    "day":   {"stars": STAR_PRICE_DAY,   "days": 1,  "title": "CueMe Premium — 1 день"},
    "week":  {"stars": STAR_PRICE_WEEK,  "days": 7,  "title": "CueMe Premium — 1 неделя"},
    "month": {"stars": STAR_PRICE_MONTH, "days": 30, "title": "CueMe Premium — 1 месяц (автопродление)"},
}


@router.callback_query(F.data == "stars_menu")
async def cb_stars_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "⭐ Оплата Telegram Stars — прямо в Telegram, без сторонних сайтов. "
        "Выбери тариф:",
        reply_markup=stars_tariff_kb(),
    )


@router.callback_query(F.data == "stars_back")
async def cb_stars_back(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_premium_screen(call.message, bot, str(call.from_user.id), edit=True)


@router.callback_query(F.data.startswith("stars_buy:"))
async def cb_stars_buy(call: CallbackQuery, bot: Bot) -> None:
    tier = call.data.split(":", 1)[1]
    tier_info = _STARS_TIERS.get(tier)
    await call.answer()
    if not tier_info:
        return

    telegram_id = str(call.from_user.id)
    stars = tier_info["stars"]
    title = tier_info["title"]
    description = (
        "Доступ ко всем функциям CueMe (переписать, анализ собеседника, "
        "анализ стиля и т.д.) на выбранный срок."
    )
    # payload — служебный, не показывается юзеру: tier нужен в successful_payment,
    # чтобы понять, сколько дней/автопродление начислить; telegram_id и рандомный
    # хвост — на случай отладки по логам, при обработке доверяем call.from_user, не payload.
    payload = f"stars:{tier}:{telegram_id}:{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label=title, amount=stars)]

    if tier == "month":
        # sendInvoice в этой версии Bot API/aiogram НЕ принимает subscription_period —
        # нативная Stars-подписка создаётся только через createInvoiceLink, поэтому
        # для месяца шлём ссылку с Pay-кнопкой, а не инвойс напрямую в чат.
        try:
            link = await bot.create_invoice_link(
                title=title, description=description, payload=payload,
                currency="XTR", prices=prices, provider_token="",
                subscription_period=STARS_SUBSCRIPTION_PERIOD,
            )
        except Exception:
            logging.exception("stars: не удалось создать invoice link (месяц) для %s", telegram_id)
            await call.message.answer("Не получилось создать ссылку на оплату — попробуй ещё раз позже.")
            return
        b = InlineKeyboardBuilder()
        b.button(text=f"⭐ Оформить за {stars} Stars", url=link)
        await call.message.answer(
            f"{title}\n\nАвтопродление каждые 30 дней, отменить можно в любой "
            "момент в настройках Telegram → Мои подписки.",
            reply_markup=b.as_markup(),
        )
        return

    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id, title=title, description=description,
            payload=payload, currency="XTR", prices=prices, provider_token="",
        )
    except Exception:
        logging.exception("stars: не удалось отправить инвойс (%s) для %s", tier, telegram_id)
        await call.message.answer("Не получилось выставить счёт — попробуй ещё раз позже.")


@payments_router.pre_checkout_query()
async def process_stars_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    """ok=True после базовой валидации payload — своих провайдер-проверок для
    Stars не требуется (provider_token=''), но формат должен быть наш и
    сумма — ровно тарифная, иначе отклоняем, чтобы не провести левый платёж."""
    parts = pre_checkout_query.invoice_payload.split(":")
    ok = (
        len(parts) == 4
        and parts[0] == "stars"
        and parts[1] in _STARS_TIERS
        and pre_checkout_query.currency == "XTR"
        and pre_checkout_query.total_amount == _STARS_TIERS[parts[1]]["stars"]
    )
    await bot.answer_pre_checkout_query(
        pre_checkout_query.id, ok=ok,
        error_message=None if ok else "Счёт устарел или повреждён — попробуй оплатить заново.",
    )


@payments_router.message(F.successful_payment)
async def process_stars_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    if sp.currency != "XTR":
        return  # не наш платёж — Tribute вообще не идёт через Bot Payments API

    parts = sp.invoice_payload.split(":")
    if len(parts) != 4 or parts[0] != "stars" or parts[1] not in _STARS_TIERS:
        logging.warning("stars: successful_payment с неожиданным payload %r", sp.invoice_payload)
        return

    tier = parts[1]
    telegram_id = str(message.from_user.id)  # доверяем from_user, не payload
    is_subscription = sp.subscription_expiration_date is not None

    if is_subscription:
        expires_at = datetime.fromtimestamp(sp.subscription_expiration_date, tz=timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=_STARS_TIERS[tier]["days"])

    # Telegram может задублировать доставку successful_payment — record_star_payment
    # идемпотентен по charge_id (UNIQUE), применяем окно Premium только на первой записи.
    is_new = record_star_payment(
        telegram_id=telegram_id, tier=tier, stars_amount=sp.total_amount,
        charge_id=sp.telegram_payment_charge_id, is_subscription=is_subscription,
        expires_at=expires_at,
    )
    if not is_new:
        return

    set_stars_premium_until(telegram_id, expires_at)
    record_event(telegram_id, "stars_payment", f"{tier}:{sp.total_amount}")

    until_label = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    extra = " Продлится автоматически, спишется ещё раз через 30 дней." if is_subscription else ""
    await message.answer(f"🎉 Готово! Premium активен до {until_label}.{extra}")


# ── /premium — статус подписки ────────────────────────────────────────────────




def _premium_expiry_line(telegram_id: str) -> str:
    """Строка «сколько осталось + способ оплаты» для активного Premium —
    для пользователя (текст под кнопкой «Подписка»)."""
    now = datetime.now(timezone.utc)
    source, until, is_subscription = _premium_expiry_info(telegram_id)

    if source == "referral":
        return (
            f"🎁 Реферальная награда — действует до {_format_until(until)} "
            f"(осталось {_format_remaining(until - now)})."
        )
    if source == "promo_channel":
        return (
            f"📢 Награда за подписку на канал — действует до {_format_until(until)} "
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

    # source == "tribute" — until=None, точную дату окончания бот не знает.
    return (
        "💎 Подписка оформлена через Tribute — продление и отмена на их "
        "стороне, точную дату окончания бот не знает."
    )


async def _premium_status_text(bot: Bot, telegram_id: str) -> str:
    if await _is_premium(bot, telegram_id):
        expiry_line = _premium_expiry_line(telegram_id)
        return f"👑 Подписка:\n\n✅ Активна — весь функционал CueMe без ограничений.\n\n{expiry_line}"

    used = get_trial_used(telegram_id)
    left = max(0, FREE_TRIAL_REQUESTS - used)
    if left == 0:
        return (
            "👑 Подписка:\n\n"
            "❌ Не активна\n\n"
            "⏳ Бесплатные попытки закончились — но, похоже, тебе заходит 😏\n"
            "Дальше по подписке — весь функционал плюс полный разбор собеседника с подарками.\n\n"
            "Чтобы получить БЕСПЛАТНУЮ подписку перейдите в раздел 🎁 Пригласи друга\n\n"
            "Оплатили, но бот не видит подписку? Подождите пару минут и снова наберите /premium."
        )
    return (
        "👑 Подписка:\n\n"
        "❌ Не активна\n\n"
        f"⏳ Бесплатных попыток осталось: {left} из {FREE_TRIAL_REQUESTS} \n\n"
        "Чтобы получить БЕСПЛАТНУЮ подписку перейдите в раздел 🎁 Пригласи друга\n\n"
        "Оплатили, но бот не видит подписку? Подождите пару минут и снова наберите /premium."
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message, bot: Bot) -> None:
    text = await _premium_status_text(bot, str(message.from_user.id))
    await message.answer(text, reply_markup=paywall_kb())


async def _show_premium_screen(target: Message, bot: Bot, telegram_id: str, edit: bool = False) -> None:
    text = await _premium_status_text(bot, telegram_id)
    if edit:
        await target.edit_text(text, reply_markup=premium_menu_kb())
    else:
        await target.answer(text, reply_markup=premium_menu_kb())


@router.callback_query(F.data == "show_premium")
async def cb_show_premium(call: CallbackQuery, bot: Bot) -> None:
    """Тоже служит «⬅️ Назад» из «👥 Реферальная система» в «👑 Подписка» —
    редактирует то же сообщение (invite_kb() ведёт сюда же)."""
    await call.answer()
    await _show_premium_screen(call.message, bot, str(call.from_user.id), edit=True)


@router.callback_query(F.data == "show_invite")
async def cb_show_invite(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _show_invite(call.message, bot, str(call.from_user.id), edit=True)


@router.callback_query(F.data == "sub:to_menu")
async def cb_sub_to_main_menu(call: CallbackQuery) -> None:
    """«⬅️ Назад» с экрана «👑 Подписка» — в главное меню, тем же сообщением."""
    await call.answer()
    await _send_main_menu(call.message, edit=True)
