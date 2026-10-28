"""Подписка (Tribute): проверка Premium, клавиатуры оплаты, «✅ Я оплатил(а) — проверить».

Вынесено из main.py без изменения логики (строки 681–1068 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import contextvars
import logging
import time
from datetime import datetime, timezone
from aiogram import Bot, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import (
    PREMIUM_CACHE_TTL,
    PREMIUM_NEG_CACHE_TTL,
    PREMIUM_CHANNEL_ID,
    PREMIUM_SUBSCRIBE_URL,
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REPLY_BONUS,
    PROMO_CHANNEL_ANALYSIS_BONUS,
    PROMO_CHANNEL_DATE_BONUS,
    STAR_PRICE_DAY,
    STAR_PRICE_WEEK,
    STAR_PRICE_MONTH,
)
from storage import (
    get_reply_trial_bonus,
    get_analysis_trial_bonus,
    get_date_trial_bonus,
    get_last_gate_event,
    get_stars_premium_until,
    get_tribute_premium_until,
    get_trial_used,
    increment_trial_used,
    save_pending_unlock,
    delete_pending_unlock,
    record_event,
)
from app_state import dp
import handlers  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── Подписка (Tribute) ──────────────────────────────────────────────────────
# Пропуск — членство в приватном канале, которым управляет Tribute (добавляет
# при оплате, убирает при отмене/неоплате). Бот только читает текущий статус.

_premium_cache: dict[str, tuple[bool, float]] = {}  # telegram_id -> (is_premium, checked_at)
_premium_clock = time.monotonic  # часы кэша Premium и антиспама проверки оплаты (тесты подменяют)


async def _is_premium(bot: Bot, telegram_id: str) -> bool:
    """Проверяет членство в PREMIUM_CHANNEL_ID с кэшем на PREMIUM_CACHE_TTL сек,
    чтобы не дёргать Telegram API на каждое сообщение. Пока PREMIUM_CHANNEL_ID
    не настроен — всегда False (только бесплатные попытки). Оплата Telegram
    Stars (_has_stars_premium) и оплата Tribute, ПОДТВЕРЖДЁННАЯ вебхуком
    (_has_tribute_webhook_premium) дают полный Premium без похода в Telegram
    API — авторитетно само событие оплаты, а не факт членства в канале
    (который может отставать/не сработать на стороне Tribute, см.
    handle_tribute_webhook). Живая проверка членства ниже остаётся
    фолбэком — для юзеров, оплативших ДО того, как появился вебхук (тогда
    tribute_premium_until ещё не заполнен).

    Реферальная награда и награда за подписку на промо-канал раньше тоже
    были источниками полного Premium здесь (_has_referral_premium/
    _has_promo_channel_premium) — заменены 2026-10 на бонусные попытки к
    трём конкретным фичам (см. REFERRAL_*_BONUS/PROMO_CHANNEL_*_BONUS в
    config.py, _has_quota/_run_deep_analysis/_run_ideal_date ниже), больше
    не дают полный доступ. Функции-проверки не удалены физически."""
    # if _has_referral_premium(telegram_id):
    #     return True
    # if _has_promo_channel_premium(telegram_id):
    #     return True
    if handlers.referrals_gate._has_stars_premium(telegram_id):
        return True
    if handlers.referrals_gate._has_tribute_webhook_premium(telegram_id):
        return True
    if not PREMIUM_CHANNEL_ID:
        return False

    cached = _premium_cache.get(telegram_id)
    # Было: один TTL (PREMIUM_CACHE_TTL) и для «да», и для «нет».
    if cached and _premium_clock() - cached[1] < (PREMIUM_CACHE_TTL if cached[0] else PREMIUM_NEG_CACHE_TTL):
        return cached[0]

    try:
        member = await bot.get_chat_member(PREMIUM_CHANNEL_ID, int(telegram_id))
        is_prem = member.status in ("member", "administrator", "creator")
    except Exception:
        is_prem = False

    _premium_cache[telegram_id] = (is_prem, _premium_clock())
    return is_prem


_CHECK_PAYMENT_BTN = "✅ Я оплатил(а) — проверить"


def paywall_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if PREMIUM_SUBSCRIBE_URL:
        b.button(text="💎 Оформить подписку", url=PREMIUM_SUBSCRIBE_URL)
        b.button(text=_CHECK_PAYMENT_BTN, callback_data="check_payment")
    b.button(text="⭐ Оплатить Stars", callback_data="stars_menu")
    b.adjust(1)
    return b.as_markup()


def premium_menu_kb() -> InlineKeyboardMarkup:
    """Клавиатура под карточкой «👑 Подписка»: оформить (Tribute) + оплата
    Stars прямо в Telegram + два бесплатных пути (реферальная награда и
    подписка на промо-канал) + «⬅️ Назад» в главное меню. Кнопка
    «🎁 Пригласи друга» переименована в «👥 Реферальная система» — тот же
    callback_data="show_invite", просто название под новую иерархию экранов
    (Подписка → Реферальная система)."""
    b = InlineKeyboardBuilder()
    if PREMIUM_SUBSCRIBE_URL:
        b.button(text="💎 Оформить подписку", url=PREMIUM_SUBSCRIBE_URL)
        b.button(text=_CHECK_PAYMENT_BTN, callback_data="check_payment")
    b.button(text="⭐ Оплатить Stars", callback_data="stars_menu")
    b.button(text="👥 Реферальная система", callback_data="show_invite")
    b.button(text="📢 Подписаться на канал", callback_data="promo:offer")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


def stars_tariff_kb() -> InlineKeyboardMarkup:
    """Тарифы Stars: день/неделя — разовая покупка, месяц — нативная
    Stars-подписка с автопродлением (помечена отдельно, т.к. отменяется
    иначе — через настройки Telegram, не через бота)."""
    b = InlineKeyboardBuilder()
    b.button(text=f"⭐ День — {STAR_PRICE_DAY} Stars", callback_data="stars_buy:day")
    b.button(text=f"⭐ Неделя — {STAR_PRICE_WEEK} Stars", callback_data="stars_buy:week")
    b.button(text=f"⭐ Месяц (автопродление) — {STAR_PRICE_MONTH} Stars", callback_data="stars_buy:month")
    b.button(text="‹ Назад", callback_data="stars_back")
    b.adjust(1)
    return b.as_markup()


async def _send_paywall(target: Message, text: str, edit: bool = False) -> None:
    """edit=True — редактирует ТО ЖЕ сообщение (например, главное меню или
    экран с контактом), а не шлёт отдельное новое — вызывающий код передаёт
    edit только когда точно знает, что target это сообщение БОТА (иначе
    edit_text упадёт на чужом/юзерском сообщении)."""
    if edit:
        await target.edit_text(text, reply_markup=premium_menu_kb())
    else:
        await target.answer(text, reply_markup=premium_menu_kb())
    if getattr(target, "bot", None):
        _start_payment_watch(target.bot, str(target.chat.id))


# ── «✅ Я оплатил(а) — проверить» и фоновая проверка оплаты Tribute ─────────
# Вебхук Tribute не настроен (диагностика 2026-10-23: TRIBUTE_API_KEY пуст,
# все 43 Premium-юзера — только по членству в канале), поэтому о Tribute-
# оплате бот узнаёт лишь по вступлению в канал. Чтобы не ждать, пока человек
# сам что-то нажмёт: кнопка ручной проверки и фоновая проверка после показа
# экрана оплаты — бот не видит нажатие url-кнопки «Оформить подписку».

_CHECK_PAYMENT_COOLDOWN = 10            # секунд между ручными проверками
_PAYMENT_WATCH_INTERVAL = 20            # секунд между фоновыми проверками
_PAYMENT_WATCH_DURATION = 5 * 60        # сколько всего ждём
_check_payment_last: dict[str, float] = {}
_payment_watches: dict[str, asyncio.Task] = {}


def _premium_until_known(telegram_id: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    dates = [d for d in (get_tribute_premium_until(telegram_id), get_stars_premium_until(telegram_id)) if d and d > now]
    return max(dates) if dates else None


async def _confirm_premium_active(bot: Bot, telegram_id: str) -> None:
    until = _premium_until_known(telegram_id)
    text = f"🎉 Premium активен до {handlers.premium_screen._format_until(until)}." if until else "🎉 Premium активен!"
    await bot.send_message(int(telegram_id), text)
    await handlers.business._offer_pending_reveals(bot, telegram_id)


def _start_payment_watch(bot: Bot, telegram_id: str) -> None:
    """Фоновая проверка каждые _PAYMENT_WATCH_INTERVAL с, до
    _PAYMENT_WATCH_DURATION: стал Premium — сам присылает подтверждение.
    Один таск на юзера (уже идёт — не трогаем), сам убирает себя из словаря."""
    if not PREMIUM_CHANNEL_ID or not PREMIUM_SUBSCRIBE_URL:
        return
    task = _payment_watches.get(telegram_id)
    if task and not task.done():
        return
    _payment_watches[telegram_id] = asyncio.get_running_loop().create_task(_payment_watch(bot, telegram_id))


async def _payment_watch(bot: Bot, telegram_id: str) -> None:
    try:
        if await _is_premium(bot, telegram_id):
            return  # уже Premium — ждать нечего
        for _ in range(max(1, round(_PAYMENT_WATCH_DURATION / _PAYMENT_WATCH_INTERVAL))):
            await asyncio.sleep(_PAYMENT_WATCH_INTERVAL)
            _premium_cache.pop(telegram_id, None)
            if await _is_premium(bot, telegram_id):
                logging.info("payment watch: %s стал Premium — подтверждаю", telegram_id)
                await _confirm_premium_active(bot, telegram_id)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("payment watch failed: telegram_id=%s", telegram_id)
    finally:
        if _payment_watches.get(telegram_id) is asyncio.current_task():
            _payment_watches.pop(telegram_id, None)


def _check_payment_retry_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Проверить ещё раз", callback_data="check_payment")
    return b.as_markup()


@dp.callback_query(F.data == "check_payment")
async def cb_check_payment(call: CallbackQuery, bot: Bot) -> None:
    telegram_id = str(call.from_user.id)
    now = _premium_clock()
    left = _CHECK_PAYMENT_COOLDOWN - (now - _check_payment_last.get(telegram_id, -1e9))
    if left > 0:
        await call.answer(f"Проверяю не чаще раза в {_CHECK_PAYMENT_COOLDOWN} с — подожди {int(left) + 1} с.")
        return
    _check_payment_last[telegram_id] = now
    await call.answer()
    _premium_cache.pop(telegram_id, None)
    if await _is_premium(bot, telegram_id):
        task = _payment_watches.pop(telegram_id, None)
        if task and not task.done():
            task.cancel()  # уже подтвердили — фоновой проверке нечего слать
        await _confirm_premium_active(bot, telegram_id)
        return
    await call.message.answer(
        "Платёж ещё обрабатывается. Обычно до пары минут. "
        "Если через 5 минут не появится — напиши мне.\n\n"
        "Проверь, что вступил(а) в закрытый канал по ссылке от Tribute — "
        "без этого бот подписку не видит.",
        reply_markup=_check_payment_retry_kb(),
    )
    _start_payment_watch(bot, telegram_id)


async def _has_quota(bot: Bot, telegram_id: str) -> bool:
    """Есть ли доступ к генерации: premium или остались бесплатные попытки.
    Базового лимита больше нет (был FREE_TRIAL_REQUESTS=5 безусловно) — весь
    лимит это бонус за подписку на промо-канал/рефералку
    (add_reply_trial_bonus). Без списания."""
    if await _is_premium(bot, telegram_id):
        return True
    return get_reply_trial_bonus(telegram_id) > get_trial_used(telegram_id)


def _never_unlocked(telegram_id: str, is_premium: bool) -> bool:
    """Юзер ни разу не открывал бесплатный пул (ни подпиской на промо-канал,
    ни рефералкой) — только ему показывается экран-гейт «подпишись на
    канал». Кто пул уже открывал и потратил — видит обычный пейволл."""
    if is_premium:
        return False
    return not (
        get_reply_trial_bonus(telegram_id)
        or get_analysis_trial_bonus(telegram_id)
        or get_date_trial_bonus(telegram_id)
    )


# Что повторить после разблокировки для «Ответить за меня»/live-диалога —
# у них, в отличие от анализа/свидания, нет результата в кэше до гейта
# (гейт стоит ДО вызова LLM), поэтому на «👀 Показать» запрос честно
# повторяется заново. Один слот на юзера (последнее заблокированное
# действие), в памяти — после рестарта бота «Показать» попросит прислать
# сообщение ещё раз.
# Было: _pending_unlock: dict[int, dict] = {} — только в памяти процесса,
# терялось при каждом перезапуске (автодеплой перезапускает бота на каждый
# пуш), и юзер после подписки получал «пришли ещё раз» вместо ответа.
# Теперь — таблица pending_unlock (save/get/delete_pending_unlock), со
# снимком диалога на момент гейта. Оставлено для отката.
# _pending_unlock: dict[int, dict] = {}

# Исход генерации для «👀 Показать» после подписки (cb_unlock_reveal): функции
# генерации отмечают, чем кончилось (ok / rate_limit / llm_error / empty /
# stale / no_data / no_quota) — по нему решаем, показать ли «Попробовать ещё
# раз», и пишем gate_reveal_ok / gate_reveal_failed. Вне reveal — None, и
# _note_outcome ничего не делает.
_gen_outcome: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gen_outcome", default=None)


def _retry_kb(callback_data: str) -> InlineKeyboardMarkup | None:
    """«🔄 Попробовать ещё раз» под сообщением об ошибке генерации. Внутри
    «👀 Показать» после подписки (cb_unlock_reveal) — None: там своя кнопка
    повтора, второй не нужно."""
    if _gen_outcome.get() is not None:
        return None
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Попробовать ещё раз", callback_data=callback_data)
    return b.as_markup()


def _note_outcome(status: str) -> None:
    box = _gen_outcome.get()
    if box is not None and "status" not in box:
        box["status"] = status

_UNLOCK_GATE_FIRST_LINE = {
    "reply": "Ответ готов, жду тебя 👀",
    "analysis": "Анализ готов, жду тебя 👀",
    "date": "Идея готова, жду тебя 👀",
}


def _unlock_gate_text(kind: str) -> str:
    return (
        f"{_UNLOCK_GATE_FIRST_LINE[kind]}\n\n"
        f"Подпишись на {PROMO_CHANNEL_USERNAME} — и сразу открываешь:\n"
        f"💬 {PROMO_CHANNEL_REPLY_BONUS} раз «Ответить за меня»\n"
        f"🔬 {PROMO_CHANNEL_ANALYSIS_BONUS} «Анализа собеседника»\n"
        f"💡 {PROMO_CHANNEL_DATE_BONUS} «Идеальных свидания»\n\n"
        "Бесплатно. Один тап — и всё твоё."
    )


def _unlock_gate_kb(kind: str, ref: int | str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📢 Открыть канал", url=f"https://t.me/{PROMO_CHANNEL_USERNAME.lstrip('@')}")
    b.button(text="✅ Я подписался", callback_data=f"unlock:check:{kind}:{ref}")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


async def _record_gate_event(telegram_id: str, event_type: str, kind: str) -> None:
    """События экрана-гейта для «Где уходят» в /users: gate_shown /
    gate_check_ok / gate_check_fail / gate_reveal, meta — reply|analysis|date.
    То же событие подряд (повторное нажатие на тот же экран) не пишется.
    («Открыть канал» — url-кнопка, Telegram боту о нажатии не сообщает,
    поэтому gate_open_click не пишется.)"""
    try:
        last = await asyncio.to_thread(get_last_gate_event, telegram_id)
        if last and last["event_type"] == event_type and last["meta"] == kind:
            return
        await asyncio.to_thread(record_event, telegram_id, event_type, kind)
    except Exception:
        logging.exception("gate event: не удалось записать %s", event_type)


async def _quota_gate(
    bot: Bot, target: Message, telegram_id: str, edit: bool = False,
    replay: dict | None = None,
) -> bool:
    """Проверка доступа БЕЗ списания. Если попытки кончились — пейволл.
    Списание делает _charge_trial_if_needed уже ПОСЛЕ успешной генерации.
    Юзеру, который пул ещё ни разу не открывал (_never_unlocked), вместо
    пейволла — экран «подпишись на канал»; replay — что повторить после
    разблокировки (см. cb_unlock_reveal)."""
    if await _has_quota(bot, telegram_id):
        return True
    _note_outcome("no_quota")
    if _never_unlocked(telegram_id, is_premium=False):
        if replay:
            await asyncio.to_thread(save_pending_unlock, telegram_id, replay)
        else:
            await asyncio.to_thread(delete_pending_unlock, telegram_id)
        text, kb = _unlock_gate_text("reply"), _unlock_gate_kb("reply", 0)
        await (target.edit_text(text, reply_markup=kb) if edit else target.answer(text, reply_markup=kb))
        await _record_gate_event(telegram_id, "gate_shown", "reply")
        return False
    await _send_paywall(
        target,
        "Бесплатные попытки закончились — но, похоже, тебе заходит 😏 Дальше — "
        "по подписке: весь функционал плюс полный разбор собеседника с подарками.",
        edit=edit,
    )
    return False


async def _charge_trial_if_needed(bot: Bot, telegram_id: str) -> None:
    """Списывает одну попытку триала. Вызывать ТОЛЬКО после успешного ответа LLM.
    Premium попытки не тратит."""
    if await _is_premium(bot, telegram_id):
        return
    increment_trial_used(telegram_id)


async def _require_premium(bot: Bot, target: Message, telegram_id: str, edit: bool = False) -> bool:
    """Гейт для функций без бесплатного триала (анализ собеседника, стиль
    собеседника и т.п.) — доступ только по активной подписке."""
    if await _is_premium(bot, telegram_id):
        return True

    await _send_paywall(target, "Эта функция доступна только по подписке CueMe Premium.", edit=edit)
    return False


# Гейт-на-входе для «Анализ собеседника»/«Идеальное свидание» — блокировал
# доступ ТЕКСТОВОЙ заглушкой сразу, без генерации, как только пробник
# потрачен. Заменён на «видимый» пейволл (задача «повышение конверсии»):
# карточка теперь считается ПОЛНОСТЬЮ в любом случае (не дороже — тот же
# кэш, что у Premium), а урезание/полный показ решается на этапе рендера в
# самих _run_deep_analysis/_run_ideal_date (full_access). Оставлено
# закомментированным на случай отката к простому текстовому пейволлу.
# async def _require_premium_or_free_trial(
#     bot: Bot, target: Message, telegram_id: str,
#     get_trial_used_fn, edit: bool = False,
# ) -> bool:
#     """Гейт для функций с ОДНОЙ бесплатной попыткой на юзера («Анализ
#     собеседника» / «Идеальное свидание» — каждая фича считается отдельно
#     своим полем в users, НЕ переиспользует общий users.trial_used от «Ответ
#     с CueMe»). Premium активен — пропускает без ограничений. Иначе, если
#     пробник ещё не потрачен — пропускает (списание делает вызывающий код
#     ПОСЛЕ успешной генерации, см. _charge_feature_trial_if_needed). Пробник
#     уже потрачен — обычный пейволл, как раньше у _require_premium."""
#     if await _is_premium(bot, telegram_id):
#         return True
#     if not get_trial_used_fn(telegram_id):
#         return True
#
#     await _send_paywall(target, "Эта функция доступна только по подписке CueMe Premium.", edit=edit)
#     return False


async def _charge_feature_trial_if_needed(bot: Bot, telegram_id: str, mark_trial_used_fn) -> None:
    """Списывает одноразовый пробник «Анализ собеседника»/«Идеальное
    свидание». Вызывать ТОЛЬКО после успешной генерации (недостаточно данных
    не считается использованной попыткой). Premium ничего не списывает."""
    if await _is_premium(bot, telegram_id):
        return
    mark_trial_used_fn(telegram_id)
