"""Stars-подписка и вебхук Tribute.

Вынесено из main.py без изменения логики (строки 2109–2530 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram import Bot, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    LinkPreviewOptions,
    Message,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import (
    ADMIN_TELEGRAM_IDS,
    PREMIUM_CHANNEL_ID,
    TRIBUTE_API_KEY,
    TRIBUTE_WEBHOOK_PATH,
    TRIBUTE_WEBHOOK_PORT,
    TRIBUTE_CHANNEL_ID,
    REFERRAL_REPLY_BONUS,
    REFERRAL_ANALYSIS_BONUS,
    REFERRAL_DATE_BONUS,
    STAR_PRICE_DAY,
    STAR_PRICE_WEEK,
    STAR_PRICE_MONTH,
    STARS_SUBSCRIPTION_PERIOD,
)
from storage import (
    count_successful_referrals,
    get_or_create_referral_code,
    get_stars_premium_until,
    set_tribute_premium_until,
    record_tribute_payment,
    record_event,
    record_star_payment,
    set_stars_premium_until,
    upsert_user,
)
from app_state import dp
from handlers.subscription import stars_tariff_kb
import handlers  # ссылки по модулю: имена ниже по исходному main.py / общее состояние
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


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


@dp.callback_query(F.data == "stars_menu")
async def cb_stars_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "⭐ Оплата Telegram Stars — прямо в Telegram, без сторонних сайтов. "
        "Выбери тариф:",
        reply_markup=stars_tariff_kb(),
    )


@dp.callback_query(F.data == "stars_back")
async def cb_stars_back(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await main._show_premium_screen(call.message, bot, str(call.from_user.id), edit=True)


@dp.callback_query(F.data.startswith("stars_buy:"))
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


@dp.pre_checkout_query()
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


@dp.message(F.successful_payment)
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

    # Было: срок ставился от «сейчас» поверх текущего — покупка недели при
    # оставшихся 20 днях подарка (/gift_premium) или прошлой покупки срезала
    # срок до 7 дней. Теперь разовая покупка прибавляется к текущему сроку, а
    # подписка не укорачивает его (дата следующего списания — Telegram'а).
    current_until = get_stars_premium_until(telegram_id)
    now_utc = datetime.now(timezone.utc)
    if is_subscription:
        expires_at = datetime.fromtimestamp(sp.subscription_expiration_date, tz=timezone.utc)
        if current_until and current_until > expires_at:
            expires_at = current_until
    else:
        base = current_until if current_until and current_until > now_utc else now_utc
        expires_at = base + timedelta(days=_STARS_TIERS[tier]["days"])

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
    await handlers.business._offer_pending_reveals(bot, telegram_id)


# ── Tribute webhook (события оплаты приватного канала-пропуска) ─────────────
# Раньше в проекте такой интеграции не было вообще — Tribute управляет
# членством в PREMIUM_CHANNEL_ID полностью САМА (добавляет/убирает по факту
# оплаты/отмены), бот только читал итог через get_chat_member. Баг: если
# добавление на стороне Tribute почему-то тихо не срабатывало (как у
# @REVkazik — юзер уже был Premium по рефералке, бот об этом даже не узнал),
# юзера приходилось добавлять вручную. Вебхук даёт боту реальное событие
# оплаты — теперь после КАЖДОЙ успешной оплаты бот САМ, безусловно и
# независимо от текущего Premium-статуса, пытается выдать доступ (см.
# _grant_premium_channel_access) — не полагаясь только на Tribute.
#
# Формат пейлоада и заголовок подписи — по официальной документации Tribute
# (wiki.tribute.tg/for-content-creators/api-documentation/webhooks). Точных
# примеров тела запроса и кода проверки подписи в открытой документации нет
# (только описание словами) — сверить с реальным телом первого тестового
# вебхука на проде ОБЯЗАТЕЛЬНО перед боевым запуском, это единственное
# место в правке, где нет 100% гарантии совпадения с продовым API.

_TRIBUTE_GRANT_RETRIES = 3
_TRIBUTE_GRANT_RETRY_DELAY_SECONDS = 3.0


def _verify_tribute_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 тела запроса с TRIBUTE_API_KEY как секретом (hex digest) —
    заголовок trbt-signature. hmac.compare_digest — защита от timing-атаки
    при сравнении подписи."""
    if not signature:
        return False
    expected = hmac.new(TRIBUTE_API_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _alert_admin_tribute_grant_failed(
    bot: Bot, telegram_id: str, username: str | None, error: Exception | None,
) -> None:
    """Все попытки выдать доступ в канал не удались (см.
    _grant_premium_channel_access) — алерт каждому из ADMIN_TELEGRAM_IDS.
    Сам платёж/Premium-статус в БД НЕ трогаем — юзер остаётся отмечен как
    оплативший, это чисто сигнал на ручное вмешательство (см. п.2 задачи —
    не блокировать и не откатывать факт оплаты из-за этой ошибки)."""
    who = f"@{username}" if username else f"id{telegram_id}"
    reason = str(error) if error else "неизвестная ошибка"
    text = (
        f"⚠️ Не удалось выдать доступ в Premium-канал юзеру {who} ({telegram_id}) "
        f"после оплаты — нужно добавить вручную.\nПричина: {reason}"
    )
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await bot.send_message(int(admin_id), text)
        except Exception:
            logging.exception("tribute: не удалось отправить алерт админу %s", admin_id)


async def _grant_premium_channel_access(bot: Bot, telegram_id: str, username: str | None) -> None:
    """Безусловно, после КАЖДОЙ успешной Tribute-оплаты (new_subscription/
    renewed_subscription) — НЕ завязано на текущий is_premium_now (это и
    была причина бага с @REVkazik: юзер уже был Premium по рефералке, и шаг
    выдачи доступа в канал где-то пропускался). Одноразовая invite-ссылка
    (create_chat_invite_link, member_limit=1) в личку — не
    approve_chat_join_request: у закрытого канала без join-request потока
    заявок нет, юзера приглашают напрямую ссылкой. Если юзер уже в канале
    (Tribute сама справилась, или это повторная доставка вебхука на уже
    обработанное событие) — тихо выходим, это успех, а не ошибка
    (идемпотентность, см. п.3 задачи). 3 попытки с паузой на случай
    временного сбоя Telegram API; если не вышло — алерт админу, платёж/
    Premium в БД не откатываем."""
    if not PREMIUM_CHANNEL_ID:
        return  # канал не настроен — нечего выдавать

    last_error: Exception | None = None
    for attempt in range(1, _TRIBUTE_GRANT_RETRIES + 1):
        try:
            member = await bot.get_chat_member(PREMIUM_CHANNEL_ID, int(telegram_id))
            if member.status in ("member", "administrator", "creator"):
                return  # уже в канале — Tribute справилась сама либо повтор вебхука
            invite = await bot.create_chat_invite_link(
                PREMIUM_CHANNEL_ID, name=f"tribute:{telegram_id}"[:32], member_limit=1,
            )
            await bot.send_message(
                int(telegram_id),
                f"🎉 Оплата прошла! Вот ссылка в закрытый канал Premium:\n{invite.invite_link}",
            )
            return
        except TelegramForbiddenError as e:
            # Юзер заблокировал бота — ссылку в личку не доставить никаким
            # числом попыток, это не временный сбой API. Сразу алерт.
            last_error = e
            break
        except Exception as e:
            last_error = e
            logging.warning(
                "tribute: попытка %d/%d выдать доступ в канал %s не удалась: %s",
                attempt, _TRIBUTE_GRANT_RETRIES, telegram_id, e,
            )
            if attempt < _TRIBUTE_GRANT_RETRIES:
                await asyncio.sleep(_TRIBUTE_GRANT_RETRY_DELAY_SECONDS)

    await _alert_admin_tribute_grant_failed(bot, telegram_id, username, last_error)


async def _handle_tribute_subscription_event(bot: Bot, event_name: str, payload: dict) -> None:
    """new_subscription/renewed_subscription — обновляет tribute_premium_until
    (окно Premium теперь ИЗВЕСТНО боту, не только факт членства, см.
    _has_tribute_webhook_premium/_premium_expiry_info) и безусловно пытается
    выдать доступ в канал (_grant_premium_channel_access) — правильная
    логика из задачи: оплата → обновить premium_until → отдельно и
    безусловно выдать доступ, не завязывая на прежний Premium-статус.
    Идемпотентно по (subscription_id, period_id) — record_tribute_payment,
    тот же паттерн UNIQUE-констрейнта, что у record_star_payment."""
    # Было: чужой канал и пустой payload отбрасывались молча — в журнале не
    # было видно, почему оплата не дала Premium.
    if not payload:
        logging.warning("tribute: пустой payload события %s — пропускаю", event_name)
        return
    channel_id = payload.get("channel_id")
    if TRIBUTE_CHANNEL_ID and str(channel_id) != TRIBUTE_CHANNEL_ID:
        logging.warning("tribute: событие %s для другого канала channel_id=%r (жду %s) — пропускаю",
                        event_name, channel_id, TRIBUTE_CHANNEL_ID)
        return  # событие для другого канала на этом же API-ключе

    telegram_id = str(payload.get("telegram_user_id") or "")
    subscription_id = payload.get("subscription_id")
    period_id = payload.get("period_id")
    expires_at_raw = payload.get("expires_at")
    if not telegram_id or subscription_id is None or period_id is None or not expires_at_raw:
        logging.warning("tribute: неожиданный payload события %s: %r", event_name, payload)
        return

    try:
        expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
    except ValueError:
        logging.warning("tribute: не удалось распарсить expires_at %r", expires_at_raw)
        return

    is_new = record_tribute_payment(
        telegram_id=telegram_id, subscription_id=int(subscription_id), period_id=int(period_id),
        event_name=event_name, amount=int(payload.get("amount") or 0),
        currency=str(payload.get("currency") or ""), expires_at=expires_at,
    )
    if not is_new:
        return  # повторная доставка того же события (subscription_id+period_id) — уже обработано

    set_tribute_premium_until(telegram_id, expires_at)
    upsert_user(telegram_id, f"user{telegram_id}")
    record_event(telegram_id, "tribute_payment", f"{event_name}:{payload.get('amount')}")

    username = payload.get("telegram_username")
    until_label = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    try:
        await bot.send_message(int(telegram_id), f"🎉 Оплата прошла! Premium активен до {until_label}.")
    except Exception:
        logging.warning("tribute: не удалось отправить подтверждение оплаты %s", telegram_id)
    await handlers.business._offer_pending_reveals(bot, telegram_id)

    # Безусловно — независимо от того, был ли уже Premium через другой
    # источник (реферал/промо/Stars) до этой оплаты. Именно этот шаг раньше
    # где-то пропускался и вызывал баг с @REVkazik.
    await _grant_premium_channel_access(bot, telegram_id, username)


async def handle_tribute_webhook(request: web.Request) -> web.Response:
    raw_body = await request.read()
    signature = request.headers.get("trbt-signature", "")
    if not _verify_tribute_signature(raw_body, signature):
        logging.warning("tribute: неверная подпись вебхука")
        return web.json_response({"status": "invalid signature"}, status=401)

    try:
        event = json.loads(raw_body)
    except ValueError:
        return web.json_response({"status": "bad json"}, status=400)

    name = event.get("name")
    payload = event.get("payload") or {}
    bot: Bot = request.app["bot"]

    if name in ("new_subscription", "renewed_subscription"):
        try:
            await _handle_tribute_subscription_event(bot, name, payload)
        except Exception:
            # 200 всё равно возвращаем — иначе Tribute будет ретраить вебхук
            # до ~24ч (см. комментарий выше про её retry-политику), а сбой
            # уже залогирован — при задублированном событии record_tribute_payment
            # всё равно идемпотентен, повторная попытка ничего не сломает.
            logging.exception("tribute: сбой обработки события %s", name)
    # Остальные типы (donation/product/order/cancelled_subscription) нас
    # пока не касаются — просто подтверждаем доставку, чтобы Tribute не ретраила.

    return web.json_response({"status": "ok"})


async def _start_tribute_webhook_server(bot: Bot) -> None:
    """Поднимает aiohttp-сервер ТОЛЬКО если TRIBUTE_API_KEY задан — иначе
    вебхук выключен полностью, бот работает как раньше (только polling).
    Слушает локально на TRIBUTE_WEBHOOK_PORT — наружу нужен реверс-прокси
    (nginx/caddy) с HTTPS, Tribute требует https-адрес в настройках вебхука
    в личном кабинете."""
    if not TRIBUTE_API_KEY:
        return
    app = web.Application()
    app["bot"] = bot
    app.router.add_post(TRIBUTE_WEBHOOK_PATH, handle_tribute_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", TRIBUTE_WEBHOOK_PORT)
    await site.start()
    logging.info("tribute webhook: слушаю 0.0.0.0:%d%s", TRIBUTE_WEBHOOK_PORT, TRIBUTE_WEBHOOK_PATH)


async def _referral_link(bot: Bot, code: str) -> str:
    """t.me/<реальный username бота>?start=ref<CODE> — читает username живьём
    через bot.get_me(), никогда не хардкодит (раньше в ссылке был чужой
    username — этот баг был именно тут)."""
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref{code}"


async def _invite_text(bot: Bot, telegram_id: str) -> tuple[str, str]:
    """Тело приглашения — общее для /invite и рассылки-напоминания
    (cmd_broadcast_invite), чтобы формулировка гарантированно не разъезжалась.
    Возвращает (текст, ссылка) — ссылка нужна отдельно для кнопки
    «📋 Скопировать ссылку» (copy_text, см. invite_kb)."""
    code = get_or_create_referral_code(telegram_id)
    count = count_successful_referrals(telegram_id)
    link = await _referral_link(bot, code)

    text = (
        "🎁 Пригласи друга\n\n"
        f"👥 Приведено друзей: {count}\n\n"
        f"Пригласи друга по ссылке — получи +{REFERRAL_REPLY_BONUS} «Ответить за "
        f"меня», +{REFERRAL_ANALYSIS_BONUS} «Анализ собеседника», "
        f"+{REFERRAL_DATE_BONUS} «Идеальное свидание» сразу, как только он запустит "
        "бота. Без ограничений по количеству друзей:\n\n"
        f"{link}"
    )
    return text, link


def invite_kb(link: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=link))
    b.button(text="⬅️ Назад", callback_data="show_premium")
    b.adjust(1)
    return b.as_markup()


async def _show_invite(
    message: Message, bot: Bot, telegram_id: str | None = None, edit: bool = False,
) -> None:
    telegram_id = telegram_id or str(message.from_user.id)
    text, link = await _invite_text(bot, telegram_id)
    # Ссылка сама по себе не разворачивается в превью (Telegram
    # показывал большую карточку бота под текстом) — она уже видна как
    # текст и копируется кнопкой, лишняя карточка тут не нужна.
    preview = LinkPreviewOptions(is_disabled=True)
    kb = invite_kb(link)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=preview)
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML", link_preview_options=preview)


@dp.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    await _show_invite(message, bot)
