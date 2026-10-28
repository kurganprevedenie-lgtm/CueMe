"""Рефералка, награда за промо-канал, экран-гейт «подпишись на канал», funnel-напоминания.

Вынесено из main.py без изменения логики (строки 1069–2108 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import hashlib
import html
import logging
import sqlite3
from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)
from aiogram import Bot, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    Message,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import (
    APP_NAME,
    PREMIUM_CHANNEL_ID,
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REPLY_BONUS,
    PROMO_CHANNEL_ANALYSIS_BONUS,
    PROMO_CHANNEL_DATE_BONUS,
    REFERRAL_REPLY_BONUS,
    REFERRAL_ANALYSIS_BONUS,
    REFERRAL_DATE_BONUS,
)
from storage import (
    get_latest_business_connection,
    get_analysis_trial_count,
    get_date_trial_count,
    add_reply_trial_bonus,
    get_reply_trial_bonus,
    add_analysis_trial_bonus,
    get_analysis_trial_bonus,
    add_date_trial_bonus,
    get_date_trial_bonus,
    get_users_with_known_premium_expiry,
    get_premium_expiry_reminder_until,
    set_premium_expiry_reminder_until,
    get_pending_referral,
    get_stars_premium_until,
    get_tribute_premium_until,
    get_trial_used,
    mark_bot_blocked,
    get_users_for_funnel_scan,
    set_funnel_reminder_state,
    count_business_messages_for_owner,
    get_best_active_contact_for_funnel,
    get_user_typical_active_hour_utc,
    mark_referral_credited,
    get_pending_unlock,
    delete_pending_unlock,
    mark_promo_reward_claimed,
    has_claimed_promo_reward,
)
from app_state import (
    _get_action,
    _is_admin,
    _new_action,
    _split_long_text,
    dp,
)
from handlers.subscription import _gen_outcome, _is_premium, _record_gate_event
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── Реферальная программа ─────────────────────────────────────────────────────
# Пригласивший получает бонусные попытки (REFERRAL_REPLY_BONUS/
# REFERRAL_ANALYSIS_BONUS/REFERRAL_DATE_BONUS) за КАЖДОГО друга — раньше
# это были REFERRAL_REWARD_DAYS дней полной Premium-подписки, заменено
# 2026-10 на бонус к трём конкретным фичам (см. _is_premium/_has_quota/
# _run_deep_analysis/_run_ideal_date). Два независимых пути привести друга:
# 1) реферальная ССЫЛКА (/start ref<CODE>, см. cmd_start) — засчитывается и
#    начисляется МГНОВЕННО при первом /start приглашённого, без требования
#    Premium или подключения Автоматизации чатов — только сам факт /start.
# 2) персональный КОД, который друг вводит вручную командой /redeem — тут
#    начисление специально отложено до реального использования бота (первый
#    контакт, см. _credit_referral_if_pending) — слабее сигнал вовлечённости,
#    чем клик по настоящей ссылке, так что anti-abuse тут строже.
# Оба пути пишут в одну и ту же таблицу referrals (save_referral_pending,
# PRIMARY KEY referred_telegram_id — один друг не может быть засчитан дважды
# ни при каком сочетании путей).


async def _credit_referral_if_pending(bot: Bot, referred_id: str) -> None:
    """Начисляет рефереру Premium-награду за referred_id, если по нему есть
    незачтённая (credited=0) запись — идемпотентно, повторный вызов для уже
    зачтённого друга просто no-op (get_pending_referral вернёт None). Вызывается
    из двух мест: сразу из cmd_start (реферальная ссылка — мгновенно) и из
    business-connect/JSON-import (страховка для /redeem-кода — там начисление
    ждёт реального первого контакта). Каждый новый друг НАКАПЛИВАЕТ бонус —
    add_* всегда прибавляет к уже начисленному, без верхнего предела (как и
    раньше было с днями полного Premium). Идемпотентно: credited-флаг +
    PRIMARY KEY(referred_id) не дают начислить дважды за одного и того же
    друга."""
    referrer_id = get_pending_referral(referred_id)
    if not referrer_id:
        return
    add_reply_trial_bonus(referrer_id, REFERRAL_REPLY_BONUS)
    add_analysis_trial_bonus(referrer_id, REFERRAL_ANALYSIS_BONUS)
    add_date_trial_bonus(referrer_id, REFERRAL_DATE_BONUS)
    mark_referral_credited(referred_id)
    try:
        await bot.send_message(
            int(referrer_id),
            "🎉 Твой друг начал пользоваться CueMe! Держи подарок — "
            f"+{REFERRAL_REPLY_BONUS} «Ответить за меня», "
            f"+{REFERRAL_ANALYSIS_BONUS} «Анализ собеседника», "
            f"+{REFERRAL_DATE_BONUS} «Идеальное свидание».",
        )
    except Exception:
        logging.warning("referral notify failed: referrer=%s", referrer_id)
    await main._offer_pending_reveals(bot, referrer_id)


# _has_referral_premium/_has_promo_channel_premium — раньше участвовали в
# _is_premium как источники полного Premium (временное окно по until),
# заменены 2026-10 на бонус попытками (см. _credit_referral_if_pending/
# cb_promo_check/on_promo_channel_membership_change) — больше ниоткуда не
# вызываются, оставлены закомментированными на случай отката.
# def _has_referral_premium(telegram_id: str) -> bool:
#     """Активно ли реферальное окно полной Premium-подписки."""
#     until = get_deep_analysis_free_until(telegram_id)
#     return bool(until and until > datetime.now(timezone.utc))
#
#
# def _has_promo_channel_premium(telegram_id: str) -> bool:
#     """Активно ли окно Premium за подписку на промо-канал (PROMO_CHANNEL_USERNAME —
#     ПУБЛИЧНЫЙ канал, не путать с приватным PREMIUM_CHANNEL_ID/Tribute)."""
#     until = get_promo_channel_premium_until(telegram_id)
#     return bool(until and until > datetime.now(timezone.utc))


def _has_stars_premium(telegram_id: str) -> bool:
    """Активно ли окно Premium, купленное за Telegram Stars — независимо от
    членства в приватном канале Tribute (см. _run_stars_successful_payment)."""
    until = get_stars_premium_until(telegram_id)
    return bool(until and until > datetime.now(timezone.utc))


def _has_tribute_webhook_premium(telegram_id: str) -> bool:
    """Активно ли окно Tribute-Premium, подтверждённое вебхуком (см.
    handle_tribute_webhook) — NULL/пусто у юзеров, оплативших до появления
    вебхука (для них актуален только live-фолбэк на get_chat_member в
    _is_premium)."""
    until = get_tribute_premium_until(telegram_id)
    return bool(until and until > datetime.now(timezone.utc))


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


@dp.callback_query(F.data == "promo:offer")
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
        f"+{PROMO_CHANNEL_REPLY_BONUS} «Ответить за меня», "
        f"+{PROMO_CHANNEL_ANALYSIS_BONUS} «Анализ собеседника», "
        f"+{PROMO_CHANNEL_DATE_BONUS} «Идеальное свидание» бесплатно. Проверяем "
        "автоматически, но если через пару минут ничего не пришло — жми «✅ Я подписался».",
        reply_markup=_promo_offer_kb(),
    )


@dp.callback_query(F.data == "promo:check")
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

    if has_claimed_promo_reward(telegram_id):
        await call.answer("Уже получено раньше", show_alert=True)
        return

    add_reply_trial_bonus(telegram_id, PROMO_CHANNEL_REPLY_BONUS)
    add_analysis_trial_bonus(telegram_id, PROMO_CHANNEL_ANALYSIS_BONUS)
    add_date_trial_bonus(telegram_id, PROMO_CHANNEL_DATE_BONUS)
    mark_promo_reward_claimed(telegram_id)
    await call.answer()
    await call.message.edit_text(
        f"✅ Готово! Ты подписан(а) — держи подарок: "
        f"+{PROMO_CHANNEL_REPLY_BONUS} «Ответить за меня», "
        f"+{PROMO_CHANNEL_ANALYSIS_BONUS} «Анализ собеседника», "
        f"+{PROMO_CHANNEL_DATE_BONUS} «Идеальное свидание».",
        reply_markup=_promo_back_kb(),
    )
    await main._offer_pending_reveals(bot, telegram_id)


# ── Экран-гейт «подпишись на канал» (первая попытка любой из трёх фич) ───────
# См. _quota_gate/_run_deep_analysis/_run_ideal_date — там он показывается,
# здесь обрабатываются его кнопки. kind — reply|analysis|date, ref — contact_id
# для analysis/date (для reply не нужен: что повторить, лежит в _pending_unlock).

_UNLOCK_REVEAL_BUTTON = {
    "reply": "👀 Показать ответ",
    "analysis": "👀 Показать анализ",
    "date": "👀 Показать идею",
}


@dp.callback_query(F.data.startswith("unlock:check:"))
async def cb_unlock_check(call: CallbackQuery, bot: Bot) -> None:
    """Та же реальная проверка подписки, что в cb_promo_check. Бонус —
    только если промо-награду ещё не получали (has_claimed_promo_reward):
    повторное нажатие или подписка, уже засчитанная автоматически
    (on_promo_channel_membership_change), второй раз ничего не начисляют."""
    _, _, kind, ref = call.data.split(":", 3)
    telegram_id = str(call.from_user.id)

    try:
        member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
        subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        logging.exception("unlock check failed for %s", telegram_id)
        subscribed = False
    if not subscribed:
        await _record_gate_event(telegram_id, "gate_check_fail", kind)
        await call.answer("Не вижу подписки — проверь и попробуй снова", show_alert=True)
        return
    await _record_gate_event(telegram_id, "gate_check_ok", kind)

    if not has_claimed_promo_reward(telegram_id):
        add_reply_trial_bonus(telegram_id, PROMO_CHANNEL_REPLY_BONUS)
        add_analysis_trial_bonus(telegram_id, PROMO_CHANNEL_ANALYSIS_BONUS)
        add_date_trial_bonus(telegram_id, PROMO_CHANNEL_DATE_BONUS)
        mark_promo_reward_claimed(telegram_id)

    reply_left = max(0, get_reply_trial_bonus(telegram_id) - get_trial_used(telegram_id))
    analysis_left = max(0, get_analysis_trial_bonus(telegram_id) - get_analysis_trial_count(telegram_id))
    date_left = max(0, get_date_trial_bonus(telegram_id) - get_date_trial_count(telegram_id))

    b = InlineKeyboardBuilder()
    b.button(text=_UNLOCK_REVEAL_BUTTON.get(kind, "👀 Показать"), callback_data=f"unlock:reveal:{kind}:{ref}")
    await call.answer()
    await call.message.edit_text(
        f"Готово ✅ Разблокировано: {reply_left} / {analysis_left} / {date_left}.",
        reply_markup=b.as_markup(),
    )


# cb_unlock_reveal — старая версия (до 2026-10-10): контекст «Ответить за
# меня» брала из памяти процесса (_pending_unlock) — после перезапуска его не
# было, юзер видел «пришли ещё раз»; при ошибке генерации кнопки повтора не
# было (сообщение с «👀 Показать» уже переписано), а аналитика видела только
# нажатие, не результат. Заменена версией ниже. Оставлено для отката.
# @dp.callback_query(F.data.startswith("unlock:reveal:"))
# async def cb_unlock_reveal(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
#     """Анализ/свидание — уже посчитаны и лежат в кэше, просто рендерим заново
#     (теперь full_access=True). «Ответить за меня»/live — до гейта вызова LLM
#     не было, поэтому честно повторяем тот же запрос (_pending_unlock)."""
#     _, _, kind, ref = call.data.split(":", 3)
#     user_id = call.from_user.id
#     telegram_id = str(user_id)
#     await call.answer()
#     await _record_gate_event(telegram_id, "gate_reveal", kind)
#
#     if kind == "analysis":
#         await _run_deep_analysis(bot, call.message, telegram_id, int(ref), edit=True)
#         return
#     if kind == "date":
#         await _run_ideal_date(bot, call.message, telegram_id, int(ref), edit=True)
#         return
#
#     replay = _pending_unlock.pop(user_id, None)
#     if not replay:
#         await call.message.edit_text("Готово ✅ Пришли сообщение ещё раз — теперь отвечу.")
#         return
#     try:
#         await call.message.edit_reply_markup(reply_markup=None)
#     except Exception:
#         pass
#
#     stale = "Контекст устарел — начни заново через «💬 Ответ с CueMe»."
#     if replay["kind"] == "reply_incoming":
#         if not (await state.get_data()).get("style_card"):
#             await call.message.answer(stale)
#             return
#         await _process_reply_incoming(call.message, state, bot, replay["incoming"], user_id)
#     elif replay["kind"] == "live_incoming":
#         await _process_live_incoming(call.message, state, bot, replay["incoming"], user_id)
#     else:
#         ctx = _get_action(user_id, replay["action_id"])
#         if not ctx:
#             await call.message.answer(stale)
#             return
#         if replay["kind"] == "variants":
#             await _run_variants_generation(
#                 call.message, ctx, user_id, bot, replay["action_id"], state,
#                 force_fresh=replay["force_fresh"],
#             )
#         else:
#             await _run_live_coach_step(
#                 call.message, ctx, user_id, bot, replay["action_id"], force_fresh=replay["force_fresh"],
#             )


_REVEAL_RETRY_STATUSES = {"rate_limit", "llm_error", "empty", "unknown"}


async def _do_unlock_reveal(call: CallbackQuery, state: FSMContext, bot: Bot, kind: str, ref: str) -> str | None:
    """Показывает разблокированный результат. Возвращает статус, если он
    известен заранее (context_lost), иначе None — тогда статус отметит сама
    функция генерации через _note_outcome."""
    user_id = call.from_user.id
    telegram_id = str(user_id)
    if kind == "analysis":
        await main._run_deep_analysis(bot, call.message, telegram_id, int(ref), edit=True)
        return None
    if kind == "date":
        await main._run_ideal_date(bot, call.message, telegram_id, int(ref), edit=True)
        return None

    replay = await asyncio.to_thread(get_pending_unlock, telegram_id)
    if not replay:
        await call.message.edit_text("Готово ✅ Попытки открыты — пришли сообщение собеседника ещё раз, и я отвечу.")
        return "context_lost"
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    stale = "Контекст устарел — начни заново через «💬 Ответ с CueMe»."
    if replay["kind"] in ("reply_incoming", "live_incoming"):
        # После перезапуска бота состояние диалога в памяти пропало —
        # восстанавливаем его из снимка, сделанного в момент гейта.
        if replay.get("fsm_data") and not (await state.get_data()).get("contact_id"):
            await state.set_state(replay.get("fsm_state"))
            await state.set_data(replay["fsm_data"])
        if replay["kind"] == "reply_incoming":
            if not (await state.get_data()).get("style_card"):
                await call.message.answer(stale)
                return "context_lost"
            await main._process_reply_incoming(call.message, state, bot, replay["incoming"], user_id)
        else:
            await main._process_live_incoming(call.message, state, bot, replay["incoming"], user_id)
        return None

    action_id = replay["action_id"]
    ctx = _get_action(user_id, action_id)
    if not ctx and replay.get("ctx"):
        ctx = replay["ctx"]
        action_id = _new_action(user_id, ctx)
    if not ctx:
        await call.message.answer(stale)
        return "context_lost"
    if replay["kind"] == "variants":
        await main._run_variants_generation(
            call.message, ctx, user_id, bot, action_id, state, force_fresh=replay["force_fresh"],
        )
    else:
        await main._run_live_coach_step(call.message, ctx, user_id, bot, action_id, force_fresh=replay["force_fresh"])
    return None


@dp.callback_query(F.data.startswith("unlock:reveal:"))
async def cb_unlock_reveal(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«👀 Показать» после подписки. Контекст «Ответить за меня» — из базы
    (pending_unlock), переживает перезапуск бота. Исход пишется событием
    gate_reveal_ok / gate_reveal_failed (meta "<kind>:<причина>"). При
    временной ошибке — сообщение с кнопкой «🔄 Попробовать ещё раз» на тот
    же контекст; попытка при этом не списывается (списание — только после
    успешного ответа, в самих функциях генерации)."""
    _, _, kind, ref = call.data.split(":", 3)
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _record_gate_event(telegram_id, "gate_reveal", kind)

    box: dict = {}
    token = _gen_outcome.set(box)
    try:
        status = await _do_unlock_reveal(call, state, bot, kind, ref)
    except Exception:
        logging.exception("unlock reveal: сбой owner=%s kind=%s", telegram_id, kind)
        status = "unknown"
    finally:
        _gen_outcome.reset(token)
    status = status or box.get("status", "unknown")

    if status == "ok":
        await asyncio.to_thread(delete_pending_unlock, telegram_id)
        await _record_gate_event(telegram_id, "gate_reveal_ok", kind)
        return

    await _record_gate_event(telegram_id, "gate_reveal_failed", f"{kind}:{status}")
    logging.warning("unlock reveal: не доставлено owner=%s kind=%s причина=%s", telegram_id, kind, status)
    if status in _REVEAL_RETRY_STATUSES:
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Попробовать ещё раз", callback_data=call.data)
        try:
            await call.message.answer(
                "Подписка уже засчитана ✅ Ответ сейчас не получился — нажми, чтобы попробовать ещё раз.",
                reply_markup=b.as_markup(),
            )
        except Exception:
            logging.warning("unlock reveal: не удалось отправить кнопку повтора owner=%s", telegram_id)
    elif status != "no_quota":
        # Контекст потерян / данных мало — повтор не поможет; no_quota не
        # трогаем: _quota_gate только что сохранил свежий контекст нового гейта.
        await asyncio.to_thread(delete_pending_unlock, telegram_id)


@dp.chat_member()
async def on_promo_channel_membership_change(event: ChatMemberUpdated, bot: Bot) -> None:
    """Автоматическая проверка подписки на промо-канал (PROMO_CHANNEL_USERNAME) —
    основной путь, плюс ручная кнопка «✅ Я подписался» (cb_promo_check) как
    подстраховка на случай, если это chat_member-событие не пришло. Требует
    прав администратора бота в этом канале — иначе апдейты по нему не
    приходят.

    Награда теперь — разовый бонус попытками (не окно полного Premium), так
    что при is_member=False тут больше нечего делать: отписка не отбирает
    уже выданный бонус (было иначе, пока награда была time-based окном —
    см. комментарий у pause_promo_channel_premium/resume_promo_channel_premium).
    При is_member=True — выдаём бонус РОВНО ОДИН РАЗ (has_claimed_promo_reward —
    разовый anti-abuse флаг), уведомляем НОВЫМ сообщением (не правкой экрана
    «Подписка» — юзер к этому моменту мог уйти в другой раздел меню)."""
    channel_username = PROMO_CHANNEL_USERNAME.lstrip("@").lower()
    if (event.chat.username or "").lower() != channel_username:
        return

    telegram_id = str(event.new_chat_member.user.id)
    was_member = event.old_chat_member.status in ("member", "administrator", "creator")
    is_member = event.new_chat_member.status in ("member", "administrator", "creator")
    if was_member == is_member:
        return

    if not is_member:
        return

    if has_claimed_promo_reward(telegram_id):
        return

    add_reply_trial_bonus(telegram_id, PROMO_CHANNEL_REPLY_BONUS)
    add_analysis_trial_bonus(telegram_id, PROMO_CHANNEL_ANALYSIS_BONUS)
    add_date_trial_bonus(telegram_id, PROMO_CHANNEL_DATE_BONUS)
    mark_promo_reward_claimed(telegram_id)
    await _notify_promo_reward_granted(bot, telegram_id)


_PROMO_PAUSED_TEXT = (
    "Очень жаль, что вы отписались от нашего канала 😔 К сожалению, мы "
    "приостановили ваш пробный Premium-период. Чтобы возобновить его — "
    "подпишитесь обратно, и оставшееся время вернётся."
)
# ^ больше не используется (см. _notify_promo_premium_paused ниже) — оставлен
# как есть вместе с функцией, которая его показывала, на случай отката.


# _notify_promo_premium_paused/_notify_promo_premium_resumed — были
# актуальны для time-based окна Premium (пауза на отписку/возобновление на
# повторную подписку); с переходом на разовый бонус попытками (2026-10)
# отписка/подписка больше не паузит и не возобновляет уже выданный бонус,
# см. on_promo_channel_membership_change выше. Не удалены физически.
# async def _notify_promo_premium_paused(bot: Bot, telegram_id: str) -> None:
#     """Уведомляет юзера о приостановке промо-Premium из-за отписки от канала.
#     Молча глотает ошибку отправки (юзер мог заблокировать бота) — постановка
#     на паузу уже применена и не зависит от того, дошло ли уведомление."""
#     try:
#         await bot.send_message(int(telegram_id), _PROMO_PAUSED_TEXT)
#     except Exception:
#         logging.warning("promo premium pause notify failed: telegram_id=%s", telegram_id)


async def _notify_promo_reward_granted(bot: Bot, telegram_id: str) -> None:
    """Уведомляет о ПЕРВОЙ выдаче промо-награды после автоматически
    подтверждённой подписки — отдельным НОВЫМ сообщением (не правкой экрана
    «Подписка»: асинхронное событие, юзер мог уже уйти в другой раздел
    меню). Молча глотает ошибку отправки — награда уже начислена
    независимо от того, дошло ли уведомление."""
    text = (
        f"✅ Готово! Ты подписан(а) — держи подарок: "
        f"+{PROMO_CHANNEL_REPLY_BONUS} «Ответить за меня», "
        f"+{PROMO_CHANNEL_ANALYSIS_BONUS} «Анализ собеседника», "
        f"+{PROMO_CHANNEL_DATE_BONUS} «Идеальное свидание»."
    )
    try:
        await bot.send_message(int(telegram_id), text)
    except Exception:
        logging.warning("promo reward grant notify failed: telegram_id=%s", telegram_id)
    await main._offer_pending_reveals(bot, telegram_id)


# async def _notify_promo_premium_resumed(bot: Bot, telegram_id: str, until: datetime) -> None:
#     """Уведомляет о возобновлении промо-Premium после повторной подписки
#     (юзер был на паузе) — тоже отдельное новое сообщение."""
#     text = f"🎉 С возвращением! Оставшееся время Premium возобновлено — до {_format_until(until)}."
#     try:
#         await bot.send_message(int(telegram_id), text)
#     except Exception:
#         logging.warning("promo premium resume notify failed: telegram_id=%s", telegram_id)
#     await _offer_pending_reveals(bot, telegram_id)


_PREMIUM_EXPIRY_REMINDER_WINDOW = (timedelta(hours=24), timedelta(hours=48))


# ── Funnel-напоминания для НЕ-премиум юзеров ──────────────────────────────────
# Три стадии воронки: подключить автоматизацию → попробовать триал → у
# активного контакта предложить разбор. Не чаще раза в день на юзера,
# преимущественно вечером. См. _check_funnel_reminders.

_FUNNEL_STAGE_CONNECT = "connect"
_FUNNEL_STAGE_TRIAL   = "trial"
_FUNNEL_STAGE_BOND    = "bond"

# Было: растущие интервалы между напоминаниями одной стадии, после 4-го —
# молчим по этой стадии. Заменено на ежедневный каденс без лимита.
# _FUNNEL_REMINDER_DELAYS = [timedelta(hours=6), timedelta(days=2), timedelta(days=5), timedelta(days=10)]

# Первое напоминание — через 6 часов после начала стадии (не сразу, дать
# юзеру время). Дальше — РОВНО раз в день, без ограничения по количеству,
# пока не сменится стадия (подключил/попробовал триал/стал премиумом) или
# юзер не заблокирует бота. Лимита нет — сознательное решение (см.
# обсуждение риска блокировок при таком каденсе).
_FUNNEL_FIRST_DELAY = timedelta(hours=6)
_FUNNEL_REPEAT_INTERVAL = timedelta(days=1)
# «Раз в день» = в окне СЛЕДУЮЩЕГО дня. Буквальное «+24ч от прошлой
# отправки» иногда пропускало день: окно каждый день сдвигается на ±150
# мин, и если завтрашнее окно раньше сегодняшнего больше чем на свою
# ширину (3ч), к моменту «+24ч» оно уже закончилось — переносилось на
# послезавтра (~8% дней). Срок = +24ч минус этот запас: ближайшее окно
# после него — всегда завтрашнее (сегодняшнее к тому времени уже
# закрыто, завтрашнее начинается не раньше чем через 19ч после отправки).
_FUNNEL_REPEAT_EARLY_SLACK = timedelta(hours=12)

# Нет данных о часовом поясе юзера — дефолт "вечер по МСК" (17:00-22:00
# МСК = 14:00-19:00 UTC, Россия без перехода на летнее/зимнее время),
# центр окна 16:30 UTC с разбросом ±150 минут покрывает весь диапазон.
_FUNNEL_DEFAULT_CENTER_HOUR_UTC = 16.5
_FUNNEL_DEFAULT_JITTER_MINUTES = 150
# Если есть реальная история активности (business-сообщения) — целимся в
# час, когда юзер обычно и правда пишет (см. get_user_typical_active_hour_utc),
# с более узким разбросом, чтобы не уехать далеко от его настоящего вечера.
_FUNNEL_KNOWN_HOUR_JITTER_MINUTES = 45


def _funnel_target_window_start(telegram_id: str, today: date, base_hour_utc: float, jitter_minutes: int) -> datetime:
    """Стабильный псевдослучайный момент отправки на СЕГОДНЯ для этого
    юзера — свой у каждого, свой каждый день (хэш telegram_id+дата), но не
    дёргается туда-сюда при повторных проверках в течение одного дня."""
    seed = f"{telegram_id}:{today.isoformat()}"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    offset_minutes = (h % (2 * jitter_minutes + 1)) - jitter_minutes
    base_minutes = int(base_hour_utc * 60) + offset_minutes
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    return day_start + timedelta(minutes=base_minutes)


# Было: _funnel_target_window_for_user(telegram_id, now) → начало окна на
# СЕГОДНЯ, условие отправки «now >= начало окна» — без конца окна. Две
# проблемы: 1) срок (due_at), наступивший поздно вечером/ночью, слал
# напоминание сразу, в любое время суток (due 23:50 UTC = 02:50 МСК);
# 2) окно у юзера с активным часом около полуночи UTC вычислялось на
# СЛЕДУЮЩИЕ сутки (base_minutes > 24ч), и «сегодняшнее» окно никогда не
# наступало — такие юзеры не получали напоминаний вообще. Теперь у окна
# есть ширина _FUNNEL_WINDOW_WIDTH, а попадание проверяется по окнам
# соседних дат (вчера/сегодня/завтра), а не только «сегодня».
#
# def _funnel_target_window_for_user(telegram_id: str, now: datetime) -> datetime:
#     known_hour = get_user_typical_active_hour_utc(telegram_id)
#     if known_hour is not None:
#         return _funnel_target_window_start(telegram_id, now.date(), float(known_hour), _FUNNEL_KNOWN_HOUR_JITTER_MINUTES)
#     return _funnel_target_window_start(telegram_id, now.date(), _FUNNEL_DEFAULT_CENTER_HOUR_UTC, _FUNNEL_DEFAULT_JITTER_MINUTES)

# Ширина окна отправки: проход раз в 15 минут гарантированно попадёт внутрь,
# запас — на случай перезапуска/простоя бота в момент начала окна.
_FUNNEL_WINDOW_WIDTH = timedelta(hours=3)


def _funnel_window_start_on(telegram_id: str, day: date, known_hour: int | None) -> datetime:
    if known_hour is not None:
        return _funnel_target_window_start(telegram_id, day, float(known_hour), _FUNNEL_KNOWN_HOUR_JITTER_MINUTES)
    return _funnel_target_window_start(telegram_id, day, _FUNNEL_DEFAULT_CENTER_HOUR_UTC, _FUNNEL_DEFAULT_JITTER_MINUTES)


def _funnel_next_send_at(telegram_id: str, earliest: datetime, known_hour: int | None) -> datetime:
    """Самый ранний момент >= earliest, попадающий в окно отправки юзера
    [начало окна; начало + _FUNNEL_WINDOW_WIDTH). Если earliest уже внутри
    окна — вернёт сам earliest (= «можно слать прямо сейчас»). Общая
    функция и для реальной отправки (_check_funnel_reminders), и для
    админского прогноза (/funnel_status) — чтобы прогноз не расходился с
    тем, что реально уйдёт."""
    candidates = []
    for offset in range(-1, 3):
        start = _funnel_window_start_on(telegram_id, earliest.date() + timedelta(days=offset), known_hour)
        if start + _FUNNEL_WINDOW_WIDTH > earliest:
            candidates.append(max(start, earliest))
    return min(candidates)


def _funnel_plan(row, now: datetime) -> dict:
    """Что воронка собирается сделать с юзером — без отправки и без
    проверки Premium (она дорогая, см. _check_funnel_reminders). Ключи:
    stage (None — напоминать нечего), skip (почему ничего не шлём, текстом
    для админа), count (какое по счёту напоминание этой стадии будет
    следующим, с 0), due_at (когда наступает срок), send_at (когда реально
    уйдёт — первый момент окна не раньше срока), reset (нужно ли сбросить
    сохранённую цепочку), bond_contact."""
    telegram_id = row["telegram_id"]
    plan = {"stage": None, "skip": None, "count": 0, "due_at": None, "send_at": None,
            "reset": False, "bond_contact": None}
    if row["blocked_bot"]:
        plan["skip"] = "заблокировал бота"
        return plan

    conn = get_latest_business_connection(telegram_id)
    connected = bool(conn and conn["is_enabled"])

    if not connected:
        stage = _FUNNEL_STAGE_CONNECT
        stage_started_raw = row["created_at"]
    elif get_trial_used(telegram_id) == 0:
        stage = _FUNNEL_STAGE_TRIAL
        stage_started_raw = conn["created_at"]
    else:
        bond_contact = get_best_active_contact_for_funnel(
            telegram_id,
            min_total=_FUNNEL_BOND_MIN_TOTAL,
            min_in=_FUNNEL_BOND_MIN_IN,
            max_days_since_last=_FUNNEL_BOND_MAX_DAYS_SINCE_LAST,
        )
        if bond_contact is None:
            plan["skip"] = "попробовал бесплатные ответы, но нет активной переписки для разбора"
            plan["reset"] = row["funnel_reminder_stage"] is not None
            return plan
        stage = _FUNNEL_STAGE_BOND
        stage_started_raw = conn["created_at"]
        plan["bond_contact"] = bond_contact

    plan["stage"] = stage
    try:
        stage_started_at = datetime.fromisoformat(stage_started_raw)
    except (TypeError, ValueError):
        plan["stage"] = None
        plan["skip"] = "битая дата начала стадии"
        return plan

    prev_stage = row["funnel_reminder_stage"]
    count = row["funnel_reminder_count"] if prev_stage == stage else 0
    # count больше не ограничивает число напоминаний (лимита нет) — только
    # номер следующего, для аналитики и /funnel_status.
    plan["count"] = count

    if count == 0:
        due_at = stage_started_at + _FUNNEL_FIRST_DELAY
    else:
        sent_raw = row["funnel_reminder_sent_at"] if prev_stage == stage else None
        try:
            sent_at = datetime.fromisoformat(sent_raw) if sent_raw else None
        except ValueError:
            sent_at = None
        due_at = (sent_at + _FUNNEL_REPEAT_INTERVAL - _FUNNEL_REPEAT_EARLY_SLACK) if sent_at else now
    plan["due_at"] = due_at

    known_hour = get_user_typical_active_hour_utc(telegram_id)
    plan["send_at"] = _funnel_next_send_at(telegram_id, max(due_at, now), known_hour)
    return plan


_FUNNEL_BOND_MIN_TOTAL = 12
_FUNNEL_BOND_MIN_IN = 4
_FUNNEL_BOND_MAX_DAYS_SINCE_LAST = 14


def _funnel_reminder_content(stage: str, telegram_id: str, bond_contact: sqlite3.Row | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if stage == _FUNNEL_STAGE_CONNECT:
        text = (
            f"Ты начал(-а) знакомство с {APP_NAME}, но бот ещё не подключён к твоим "
            "чатам — без этого он не видит переписку и не может подсказывать "
            "ответы. Подключается за минуту:"
        )
        return text, main.business_connect_kb()

    # Тексты стадий 'trial'/'bond' — HTML (<b>), шлются с parse_mode="HTML"
    # (_send_funnel_reminder). Текст 'connect' — plain, экранируется там же.
    if stage == _FUNNEL_STAGE_TRIAL:
        n = count_business_messages_for_owner(telegram_id)
        seen_line = f"\n\nУже накопилось <b>{n} сообщений</b> в потоке — есть на чём проверить." if n > 0 else ""
        text = (
            "🎁 Бот подключён и готов, осталось попробовать."
            f"{seen_line}\n\n"
            f"Подпишись на {PROMO_CHANNEL_USERNAME} — и первые {PROMO_CHANNEL_REPLY_BONUS} "
            "ответов бесплатно. Понравится — оформишь подписку, "
            "не понравится — ничего не теряешь."
        )
        b = InlineKeyboardBuilder()
        b.button(text=f"💬 Ответ с {APP_NAME}", callback_data="qs:yes")
        return text, b.as_markup()

    # stage == _FUNNEL_STAGE_BOND — bond_contact уже проверен вызывающим
    # кодом на порог активности (get_best_active_contact_for_funnel).
    # @username вместо display_name — имя не надо склонять («с Маша»).
    username = bond_contact["username"] if "username" in bond_contact.keys() else None
    handle = f"@{username}" if username else (bond_contact["display_name"] or "этим человеком")
    total = bond_contact["total"]
    text = (
        f"У тебя с <b>{html.escape(handle)}</b> уже <b>{total} сообщений</b> "
        "за последнее время.\n\n"
        "Если хочешь понять, что происходит на самом деле (нравишься ли, "
        "стоит ли продолжать) — можем разобрать переписку."
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔬 Разобрать переписку", callback_data=f"deepan:{bond_contact['contact_id']}")
    return text, b.as_markup()


async def _send_funnel_reminder(
    bot: Bot, chat_id: int, stage: str, telegram_id: str, bond_contact: sqlite3.Row | None = None,
) -> None:
    """Отправка напоминания воронки. Стадия 'connect' уходит в том же виде,
    что экран подключения после демо (cb_onboarding_show_connect): фото-
    инструкция + 4 шага в <blockquote>, только вместо приветствия сверху —
    текст напоминания. Остальные стадии — обычный текст с кнопкой."""
    text, kb = _funnel_reminder_content(stage, telegram_id, bond_contact)
    if stage == _FUNNEL_STAGE_CONNECT:
        caption = f"{html.escape(text)}\n\n{await main._connect_steps_blockquote(bot)}"
        await main._send_business_connect_prompt_to(bot, chat_id, caption, parse_mode="HTML")
        return
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


def _premium_expiry_reminder_content(remaining: str, gift: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Текст+кнопка напоминания об истечении Premium — вынесено из
    _check_premium_expiry_reminders, чтобы /test_reminders показывал ровно
    то же, что уходит юзерам. Мягкого варианта для Stars-подписки с
    автопродлением больше нет — по просьбе убрано совсем, такие подписки
    не напоминаем (см. _check_premium_expiry_reminders)."""
    if gift:
        # Подарок — не пейволл: человек ничего не покупал, просто предупреждаем.
        text = (f"🎁 Подарочный Premium закончится через {remaining}. "
                "Если пригодился — продлить можно в «👑 Подписка».")
    else:
        text = f"⏳ Подписка заканчивается через {remaining} — продли, чтобы не потерять доступ."
    b = InlineKeyboardBuilder()
    b.button(text="👑 Подписка", callback_data="show_premium")
    return text, b.as_markup()


async def _in_premium_channel(bot: Bot, telegram_id: str) -> bool | None:
    """Живое членство в канале-пропуске Tribute, без кэша. None — канал не
    настроен или Telegram не ответил."""
    if not PREMIUM_CHANNEL_ID:
        return None
    try:
        member = await bot.get_chat_member(PREMIUM_CHANNEL_ID, int(telegram_id))
    except Exception:
        return None
    return member.status in ("member", "administrator", "creator")


async def _check_premium_expiry_reminders(bot: Bot) -> None:
    """Напоминание об окончании Premium — ровно один раз на конкретный срок
    истечения (until), окно 24-48ч до него (_PREMIUM_EXPIRY_REMINDER_WINDOW).
    Суточной сверки достаточно, чтобы ни один until не проскочил мимо окна:
    интервал проверки (24ч) равен ширине окна (24ч), так что любой until
    попадёт хотя бы в одну проверку. «Уже напоминали именно про этот срок»
    сравнивается с premium_expiry_reminder_until — при новом продлении/
    оплате until меняется сам, отдельно сбрасывать флаг по всем местам
    продления не нужно (см. get_premium_expiry_reminder_until в storage.py).

    Источники с известной датой окончания — реферал/промо-канал/Stars и,
    теперь, Tribute — но только если оплата подтверждена вебхуком (см.
    tribute_premium_until/handle_tribute_webhook); Tribute-юзеров без этого
    поля (оплатили до вебхука) by-design пропускаем — until неизвестен,
    заранее предупредить нечем, у них остаётся только live-фолбэк на
    get_chat_member. Tribute-напоминание намеренно жёсткое (is_subscription
    трактуется как False, «продли, чтобы не потерять доступ»), даже если
    подписка технически автопродлевается — мы не отслеживаем
    cancelled_subscription, так что не можем достоверно обещать
    автопродление; ложное «не волнуйся» хуже лишнего напоминания."""
    now = datetime.now(timezone.utc)
    window_lo, window_hi = now + _PREMIUM_EXPIRY_REMINDER_WINDOW[0], now + _PREMIUM_EXPIRY_REMINDER_WINDOW[1]

    for telegram_id in get_users_with_known_premium_expiry():
        source, until, is_subscription = main._premium_expiry_info(telegram_id)
        if until is None:
            continue
        if not (window_lo <= until < window_hi):
            continue
        if get_premium_expiry_reminder_until(telegram_id) == until:
            continue  # уже напоминали именно про этот срок

        if is_subscription:
            continue  # Stars-подписка автопродлевается сама — не напоминаем
        if source == "gift" and await _in_premium_channel(bot, telegram_id):
            continue  # подарок поверх Tribute без известной даты — доступ не кончается
        text, kb = _premium_expiry_reminder_content(main._format_remaining(until - now), gift=source == "gift")

        try:
            await bot.send_message(int(telegram_id), text, reply_markup=kb)
        except Exception:
            logging.warning("premium expiry reminder notify failed: telegram_id=%s", telegram_id)
        set_premium_expiry_reminder_until(telegram_id, until)


async def _check_funnel_reminders(bot: Bot) -> None:
    """Ежедневный (не чаще раза в день) проход по воронке для НЕ-премиум
    юзеров, преимущественно вечером (см. _funnel_next_send_at):
    - не подключил Business-автоматизацию → напомнить подключить
    - подключил, но не пробовал триал → напомнить попробовать
    - использовал триал И есть контакт с реально активной перепиской
      (get_best_active_contact_for_funnel) → предложить разбор именно с
      этим человеком; если ни один контакт не проходит порог — молчим
    - премиум (любым способом) → пропускаем и сбрасываем цепочку;
      напоминание об ИСТЕЧЕНИИ премиума — отдельный уже существующий
      _check_premium_expiry_reminders, эта функция его не трогает.
    Каденс — funnel_reminder_stage/count/sent_at: первое через
    _FUNNEL_FIRST_DELAY после начала стадии, дальше ежедневно без
    ограничения по количеству, пока не сменится стадия (смена стадии или
    премиум сбрасывают счётчик) или юзер не заблокирует бота. count —
    только номер напоминания для аналитики. Вызывается часто (раз в 15
    минут из _reconcile_promo_channel_premium), но реально шлёт не чаще
    раза в день на юзера — после отправки sent_at сдвигает due_at на
    следующий день.

    _is_premium проверяется ПОСЛЕДНИМ, прямо перед отправкой, а не первым:
    для не-премиум юзера он всегда доходит до bot.get_chat_member (кэш
    всего PREMIUM_CACHE_TTL), и при проверке первым каждый проход раз в 15
    минут дёргал бы Telegram API по всей базе. Дешёвые локальные проверки
    (стадия/due_at/окно) отсекают почти всех раньше."""
    now = datetime.now(timezone.utc)

    for row in get_users_for_funnel_scan():
        telegram_id = row["telegram_id"]
        plan = _funnel_plan(row, now)
        if plan["reset"]:
            set_funnel_reminder_state(telegram_id, None, 0, None)
        stage = plan["stage"]
        if stage is None:
            continue

        # Шлём, только если срок наступил И сейчас внутри окна отправки
        # юзера (send_at == now — «можно прямо сейчас», см. _funnel_next_send_at).
        if plan["send_at"] > now:
            continue

        if await _is_premium(bot, telegram_id):
            if row["funnel_reminder_stage"] is not None:
                set_funnel_reminder_state(telegram_id, None, 0, None)
            continue

        try:
            await _send_funnel_reminder(bot, int(telegram_id), stage, telegram_id, plan["bond_contact"])
        except TelegramForbiddenError:
            mark_bot_blocked(telegram_id)
            continue
        except Exception:
            logging.warning("funnel reminder failed: telegram_id=%s stage=%s", telegram_id, stage)
            continue

        set_funnel_reminder_state(telegram_id, stage, plan["count"] + 1, now.isoformat())
        await asyncio.sleep(0.05)  # тот же троттлинг, что в broadcast-функциях


_FUNNEL_STAGE_LABELS = {
    _FUNNEL_STAGE_CONNECT: "подключить бота",
    _FUNNEL_STAGE_TRIAL: "попробовать бесплатный ответ",
    _FUNNEL_STAGE_BOND: "разбор активной переписки",
}
_MSK = timezone(timedelta(hours=3))


def _funnel_fmt_msk(dt: datetime) -> str:
    return dt.astimezone(_MSK).strftime("%d.%m %H:%M МСК")


async def _funnel_user_label(bot: Bot, telegram_id: str) -> str:
    try:
        chat = await bot.get_chat(int(telegram_id))
        if chat.username:
            return f"@{chat.username} ({telegram_id})"
    except Exception:
        pass
    return telegram_id


@dp.message(Command("funnel_status"))
async def cmd_funnel_status(message: Message, bot: Bot) -> None:
    """Админу — прогноз напоминаний воронки: кому, что и когда уйдёт.
    Считает тем же _funnel_plan, что и реальная отправка
    (_check_funnel_reminders), ничего не шлёт и не пишет в БД.
    /funnel_status — ближайшие отправки по всем; /funnel_status <telegram_id>
    — подробно по одному юзеру (с проверкой Premium)."""
    if not _is_admin(message.from_user.id):
        return
    now = datetime.now(timezone.utc)
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lstrip("@") if len(parts) == 2 else ""

    if arg:
        row = next((r for r in get_users_for_funnel_scan() if r["telegram_id"] == arg), None)
        if row is None:
            await message.answer(f"Пользователя с telegram_id {arg} нет в базе. Нужен именно числовой id (см. /users).")
            return
        plan = _funnel_plan(row, now)
        lines = [f"👤 {await _funnel_user_label(bot, arg)}", ""]
        sent_before = row["funnel_reminder_sent_at"]
        if row["funnel_reminder_stage"]:
            lines.append(
                f"Последнее напоминание: «{_FUNNEL_STAGE_LABELS.get(row['funnel_reminder_stage'], row['funnel_reminder_stage'])}», "
                f"№{row['funnel_reminder_count']}"
                + (f", {_funnel_fmt_msk(datetime.fromisoformat(sent_before))}" if sent_before else "")
            )
        else:
            lines.append("Напоминаний по воронке ещё не было.")
        if plan["stage"] is None:
            lines.append(f"Дальше: ничего не уйдёт — {plan['skip']}.")
        elif await _is_premium(bot, arg):
            lines.append("Дальше: ничего не уйдёт — есть Premium (по воронке Premium-пользователям "
                         "не напоминаем; цепочка сбросится в момент, когда подошёл бы срок).")
        else:
            lines.append(f"Следующее: «{_FUNNEL_STAGE_LABELS[plan['stage']]}», "
                         f"№{plan['count'] + 1} (дальше — раз в день, пока не сменится стадия)")
            lines.append(f"Срок наступает: {_funnel_fmt_msk(plan['due_at'])}")
            lines.append(f"Уйдёт: {_funnel_fmt_msk(plan['send_at'])}"
                         + (" (ближайший проход, до 15 минут)" if plan["send_at"] <= now else ""))
            if plan["bond_contact"] is not None:
                bc = plan["bond_contact"]
                who = f"@{bc['username']}" if bc["username"] else (bc["display_name"] or "без имени")
                lines.append(f"Контакт для разбора: {who}, {bc['total']} сообщений")
        await message.answer("\n".join(lines))
        return

    rows = get_users_for_funnel_scan()
    upcoming, skips = [], {}
    for row in rows:
        plan = _funnel_plan(row, now)
        skip = plan["skip"]
        # Premium проверяем только у тех, кому что-то запланировано (у
        # остальных и так ничего не уйдёт) — та же проверка, что
        # _check_funnel_reminders делает в момент отправки.
        if plan["stage"] is not None and await _is_premium(bot, row["telegram_id"]):
            skip = "есть Premium"
        if skip:
            skips[skip] = skips.get(skip, 0) + 1
        else:
            upcoming.append((plan["send_at"], row["telegram_id"], plan))
    upcoming.sort(key=lambda x: x[0])

    horizon = now + timedelta(hours=48)
    soon = [u for u in upcoming if u[0] <= horizon]
    lines = [f"📬 Напоминания воронки — ближайшие 48 часов: {len(soon)}", ""]
    for send_at, tid, plan in soon[:40]:
        when = "сейчас" if send_at <= now else _funnel_fmt_msk(send_at)
        lines.append(f"{when} — {await _funnel_user_label(bot, tid)}: "
                     f"«{_FUNNEL_STAGE_LABELS[plan['stage']]}» №{plan['count'] + 1}")
    if len(soon) > 40:
        lines.append(f"… и ещё {len(soon) - 40}")
    later = len(upcoming) - len(soon)
    lines += ["", f"Позже 48 часов запланировано: {later}"]
    if skips:
        lines.append("Не получат ничего:")
        lines += [f"  • {reason}: {n}" for reason, n in sorted(skips.items(), key=lambda x: -x[1])]
    lines += ["", "Подробно по одному: /funnel_status <telegram_id>"]
    for chunk in _split_long_text("\n".join(lines)):
        await message.answer(chunk)


@dp.message(Command("test_reminders"))
async def cmd_test_reminders(message: Message) -> None:
    """Админу — все автоматические напоминания подряд, ровно в том виде, в
    каком их получат юзеры (тексты берутся из тех же функций, что в
    фоновых проверках). Ничего не пишет в БД: счётчики воронки и отметки
    «уже напоминали» не трогаются."""
    if not _is_admin(message.from_user.id):
        return
    telegram_id = str(message.from_user.id)

    bond_contact = get_best_active_contact_for_funnel(
        telegram_id,
        min_total=_FUNNEL_BOND_MIN_TOTAL,
        min_in=_FUNNEL_BOND_MIN_IN,
        max_days_since_last=_FUNNEL_BOND_MAX_DAYS_SINCE_LAST,
    )
    bond_note = "реальный контакт из твоей переписки"
    if bond_contact is None:
        # Своего активного контакта нет — подставляем пример, кнопка
        # «Разобрать переписку» в этом случае никуда не ведёт.
        bond_contact = {"display_name": "Маша", "total": 27, "contact_id": 0}
        bond_note = "пример — активного контакта у тебя нет, кнопка не сработает"

    # Воронка — через _send_funnel_reminder, тот же путь, что в фоновой
    # проверке (для 'connect' это фото-инструкция, а не голый текст).
    funnel_samples = [
        ("1/4 · Воронка: не подключил автоматизацию", _FUNNEL_STAGE_CONNECT, None),
        ("2/4 · Воронка: подключил, но не пробовал триал", _FUNNEL_STAGE_TRIAL, None),
        (f"3/4 · Воронка: разбор активного контакта ({bond_note})", _FUNNEL_STAGE_BOND, bond_contact),
    ]
    for title, stage, contact in funnel_samples:
        await message.answer(f"— {title} —")
        await _send_funnel_reminder(message.bot, message.chat.id, stage, telegram_id, contact)

    await message.answer("— 4/4 · Истечение Premium —")
    text, kb = _premium_expiry_reminder_content(main._format_remaining(timedelta(hours=30)))
    await message.answer(text, reply_markup=kb)


async def _reconcile_promo_channel_premium(bot: Bot) -> None:
    """Единственный фоновый цикл в проекте (нет cron/APScheduler). Раз в
    FUNNEL_POLL_SECONDS (~15 минут) — только _check_funnel_reminders (ему
    нужна частая проверка, чтобы поймать сегодняшнее вечернее окно юзера,
    но сам он шлёт не чаще раза в день на юзера, см. докстринг). Раз в
    сутки (аккумулируем тики) — остальные суточные проверки: 1) подстраховка
    на случай пропущенного chat_member-события для промо-Premium (проходит
    по всем юзерам с активным окном, явно перепроверяет членство через
    bot.get_chat_member и ставит на паузу тех, кто уже не подписан, а
    событие поймать не удалось); 2) напоминание об истечении Premium
    (_check_premium_expiry_reminders) — им частая проверка не нужна.
    Запускается один раз из main().

    Было: while True: sleep(24h) → промо-сверка → напоминание об истечении.
    Суточный каденс этих двух проверок сохранён (tick % ticks_per_day)."""
    FUNNEL_POLL_SECONDS = 15 * 60
    ticks_per_day = (24 * 60 * 60) // FUNNEL_POLL_SECONDS

    tick = 0
    while True:
        await asyncio.sleep(FUNNEL_POLL_SECONDS)
        tick += 1

        try:
            await _check_funnel_reminders(bot)
        except Exception:
            logging.exception("funnel reminder pass failed")

        if tick % ticks_per_day != 0:
            continue

        # Суточная сверка промо-Premium (pause на отписку по until-окну) — была
        # актуальна для time-based окна Premium; с переходом на бонус-попытки
        # (2026-10) отписка/подписка больше не паузит и не возобновляет уже
        # выданный бонус, сверять нечего. Закомментировано, не удалено.
        # try:
        #     for telegram_id in get_users_with_active_promo_premium():
        #         until = get_promo_channel_premium_until(telegram_id)
        #         if not until or until <= datetime.now(timezone.utc):
        #             continue
        #         try:
        #             member = await bot.get_chat_member(PROMO_CHANNEL_USERNAME, int(telegram_id))
        #             subscribed = member.status in ("member", "administrator", "creator")
        #         except Exception:
        #             continue
        #         if subscribed:
        #             continue
        #         remaining = (until - datetime.now(timezone.utc)).total_seconds()
        #         if remaining > 0:
        #             pause_promo_channel_premium(telegram_id, int(remaining))
        #             await _notify_promo_premium_paused(bot, telegram_id)

        try:
            await _check_premium_expiry_reminders(bot)
        except Exception:
            logging.exception("premium expiry reminder pass failed")
