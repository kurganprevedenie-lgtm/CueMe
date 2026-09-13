"""👥 Реферальная система: ссылка-приглашение (/start ref<CODE>), ручной код
(/redeem), начисление награды пригласившему и экран «Пригласи друга».

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import REFERRAL_REWARD_DAYS
from handlers.common import _has_referral_premium
from storage import (
    count_successful_referrals,
    get_deep_analysis_free_until,
    get_or_create_referral_code,
    get_pending_referral,
    get_referrer_by_code,
    list_contacts,
    mark_referral_credited,
    save_referral_pending,
    set_deep_analysis_free_until,
)

router = Router(name="referral")


# ── Реферальная программа ─────────────────────────────────────────────────────
# Пригласивший получает REFERRAL_REWARD_DAYS дней полной Premium-подписки за
# КАЖДОГО друга. Два независимых пути привести друга:
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
    ждёт реального первого контакта). Каждый новый друг НАКАПЛИВАЕТ
    награду — REFERRAL_REWARD_DAYS прибавляются к уже активному окну (если
    оно ещё не истекло), а не перезаписывают его с текущего момента.
    Идемпотентно: credited-флаг + PRIMARY KEY(referred_id) не дают начислить
    дважды за одного и того же друга."""
    referrer_id = get_pending_referral(referred_id)
    if not referrer_id:
        return
    now = datetime.now(timezone.utc)
    current_until = get_deep_analysis_free_until(referrer_id)
    base = current_until if current_until and current_until > now else now
    until = base + timedelta(days=REFERRAL_REWARD_DAYS)
    set_deep_analysis_free_until(referrer_id, until)
    mark_referral_credited(referred_id)
    try:
        await bot.send_message(
            int(referrer_id),
            "🎉 Твой друг начал пользоваться CueMe! Держи подарок — "
            f"+{REFERRAL_REWARD_DAYS} дня Premium подписки "
            f"(до {until.strftime('%d.%m.%Y %H:%M UTC')}).",
        )
    except Exception:
        logging.warning("referral notify failed: referrer=%s", referrer_id)


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

    if _has_referral_premium(telegram_id):
        until = get_deep_analysis_free_until(telegram_id)
        reward_line = f"✅ Premium подписка (по рефералам) активна до {until.strftime('%d.%m.%Y %H:%M UTC')}\n"
    else:
        reward_line = ""

    text = (
        "🎁 Пригласи друга\n\n"
        f"👥 Приведено друзей: {count}\n"
        f"{reward_line}\n"
        f"Пригласи друга по ссылке — получи {REFERRAL_REWARD_DAYS} дня Premium "
        "сразу, как только он запустит бота. Без ограничений по количеству друзей:\n\n"
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


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    await _show_invite(message, bot)


class ReferralRedeem(StatesGroup):
    waiting_for_code = State()


@router.message(Command("redeem"))
async def cmd_redeem(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await _process_redeem(message, parts[1].strip())
        return
    await state.set_state(ReferralRedeem.waiting_for_code)
    await message.answer("Введи код от друга:")


@router.message(ReferralRedeem.waiting_for_code)
async def handle_redeem_code(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _process_redeem(message, (message.text or "").strip())


async def _process_redeem(message: Message, code: str) -> None:
    """Анти-абуз для /redeem:
    • код должен существовать (принадлежать реальному пользователю);
    • нельзя погасить свой же код (самоприглашение);
    • нельзя погасить код, если у тебя УЖЕ есть хоть один контакт — значит
      ты реально пользовался ботом раньше, «новым другом» задним числом стать
      нельзя (в отличие от старой ссылочной схемы, здесь /redeem доступен
      только ПОСЛЕ выбора пола, так что users-строка есть у всех — надёжный
      признак «нового» теперь список контактов, а не факт существования в БД);
    • один человек может погасить код только один раз — save_referral_pending
      это PRIMARY KEY(referred_telegram_id), INSERT OR IGNORE."""
    telegram_id = str(message.from_user.id)
    code = code.upper().strip()

    referrer_id = get_referrer_by_code(code) if code else None
    if not referrer_id:
        await message.answer("Код не найден — проверь, что ввёл его без опечаток.")
        return
    if referrer_id == telegram_id:
        await message.answer("Это твой собственный код 🙂")
        return
    if list_contacts(telegram_id):
        await message.answer("Похоже, ты уже пользуешься CueMe — этот код не для тебя.")
        return
    if get_pending_referral(telegram_id):
        await message.answer("Ты уже вводил реферальный код раньше.")
        return

    save_referral_pending(referrer_id, telegram_id)
    await message.answer(
        "Принято! Как только ты начнёшь пользоваться ботом — твой друг получит награду.\n\n"
        "Начни с кем-то новый диалог или подключи Автоматизацию чатов — /connect."
    )


@router.message(Command("myref"))
async def cmd_myref(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    count = count_successful_referrals(telegram_id)
    lines = ["🎁 Награда за рефералов:\n"]

    if _has_referral_premium(telegram_id):
        until = get_deep_analysis_free_until(telegram_id)
        until_str = until.strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"✅ Premium подписка — активна до {until_str}")
    else:
        lines.append("⏳ Активной награды нет — пригласи друга через /invite")

    lines.append(f"👥 Приведено друзей: {count}")
    await message.answer("\n".join(lines))
