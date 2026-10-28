"""/premium и экран «👑 Подписка».

Вынесено из main.py без изменения логики (строки 10121–10366 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import html
from datetime import datetime, timedelta, timezone
from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from config import (
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REPLY_BONUS,
    PROMO_CHANNEL_ANALYSIS_BONUS,
    PROMO_CHANNEL_DATE_BONUS,
    REFERRAL_REPLY_BONUS,
    REFERRAL_ANALYSIS_BONUS,
    REFERRAL_DATE_BONUS,
)
from storage import (
    get_analysis_trial_count,
    get_date_trial_count,
    get_reply_trial_bonus,
    get_analysis_trial_bonus,
    get_date_trial_bonus,
    get_latest_star_payment,
    get_stars_premium_until,
    get_tribute_premium_until,
    get_trial_used,
    has_claimed_promo_reward,
)
from app_state import dp
from handlers.subscription import (
    _is_premium,
    _never_unlocked,
    _start_payment_watch,
    paywall_kb,
    premium_menu_kb,
)
from handlers.stars_tribute import _show_invite
from handlers.redeem_menu import _menu_screen_target, _send_main_menu
from handlers.business import _offer_pending_reveals


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
