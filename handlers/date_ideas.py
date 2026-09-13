"""💐 Идеальное свидание: выборка сообщений по всей истории, генерация идеи
свидания и подарков через LLM (с кэшем) и хендлеры кнопки/обновления.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.

_deep_stats_summary импортируется из handlers/analysis — это общий счётчик
статистики переписки, исторически живущий там (кандидат на вынос в
services/, см. список находок рефакторинга).
"""
import logging
import random

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from handlers.analysis import _deep_stats_summary
from handlers.common import (
    _answer_long,
    _contact_name,
    _require_premium,
    _send_no_contacts_hint,
    _with_back_to_menu,
    contacts_kb,
)
from llm import RateLimitError, build_ideal_date
from services.cards import _gen_interaction_card
from storage import (
    delete_ideal_date,
    get_all_dated_messages,
    get_contact_by_id,
    get_ideal_date,
    list_contacts,
    save_ideal_date,
)

router = Router(name="date_ideas")


# ── 💐 Идеальное свидание ─────────────────────────────────────────────────────

IDEAL_DATE_MIN_MSGS = 5  # минимум сообщений собеседника, иначе не за что зацепиться


def _spread_sample(rows: list[dict], direction: str, target: int, offset: float = 0.0) -> list[str]:
    """Равномерная выборка target сообщений заданного направления по ВСЕЙ истории
    (не только последние N) — как периодизация в _periodized_dated_lines, но
    плоским списком текстов. Так упоминания интересов из любого периода переписки
    попадают в промпт, а не только из свежих сообщений.
    offset ∈ [0,1) сдвигает точку внутри каждого временного окна — при offset=0
    выборка детерминированная, со случайным offset «Другая идея» видит ДРУГИЕ
    сообщения (тот же равномерный охват, другие представители)."""
    msgs = [
        r["text"] for r in sorted(
            (r for r in rows if r["direction"] == direction and r["text"] and r["text"].strip()),
            key=lambda r: r["date"],
        )
    ]
    if len(msgs) <= target:
        return msgs
    step = len(msgs) / target
    last = len(msgs) - 1
    return [msgs[min(last, int(i * step + offset * step))] for i in range(target)]


def _ideal_date_samples(contact_id: int, owner_user_id: str, offset: float = 0.0) -> dict | None:
    """Семплы для build_ideal_date по ВСЕЙ истории переписки (business + JSON,
    через get_all_dated_messages) с равномерным охватом всех периодов — как в
    «Анализе собеседника», а не только последние сообщения. offset сдвигает
    выборку («Другая идея» → другие сообщения). None — сообщений собеседника
    слишком мало для осмысленной идеи."""
    rows = get_all_dated_messages(owner_user_id, contact_id)
    contact_msgs = _spread_sample(rows, "in", 100, offset)
    my_msgs      = _spread_sample(rows, "out", 40, offset)
    if len(contact_msgs) < IDEAL_DATE_MIN_MSGS:
        return None
    stats = _deep_stats_summary(rows)
    return {"contact_sample": contact_msgs, "my_sample": my_msgs, "features_summary": stats}


async def _gen_ideal_date(contact_id: int, owner_user_id: str, fresh: bool = False) -> dict | None:
    """Ленивая генерация с кэшем в ideal_date. None — данных мало.
    fresh=True («Другая идея») — не читает кэш и берёт СЛУЧАЙНО сдвинутую
    выборку, чтобы модель увидела другие сообщения и дала заметно другую идею."""
    if not fresh:
        cached = get_ideal_date(contact_id)
        if cached:
            return cached

    offset = random.random() if fresh else 0.0
    samples = _ideal_date_samples(contact_id, owner_user_id, offset)
    if not samples:
        return None

    interaction_card = await _gen_interaction_card(contact_id, owner_user_id) or ""
    date_idea, gift_ideas = await build_ideal_date(
        samples["contact_sample"], samples["my_sample"],
        interaction_card, samples["features_summary"],
    )
    save_ideal_date(contact_id, date_idea, gift_ideas)
    return {"date_idea": date_idea, "gift_ideas": gift_ideas}


def _format_ideal_date(name: str, data: dict) -> str:
    """Оба блока (идея свидания + подарки) — одним сообщением."""
    return (
        f"💐 Идеальное свидание — {name}\n\n"
        f"{data['date_idea'].strip()}\n\n"
        f"{data['gift_ideas'].strip()}"
    )


def ideal_date_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другая идея", callback_data=f"idealdate_refresh:{contact_id}")
    return _with_back_to_menu(b.as_markup())


async def _run_ideal_date(
    bot: Bot, target: Message, telegram_id: str, contact_id: int,
    edit: bool = False, fresh: bool = False,
) -> None:
    if not await _require_premium(bot, target, telegram_id, edit=edit):
        return
    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    name = _contact_name(contact)

    wait_text = f"Придумываю идеальное свидание с {name}. Это займёт ~20 секунд..."
    await (target.edit_text(wait_text) if edit else target.answer(wait_text))

    try:
        data = await _gen_ideal_date(contact_id, telegram_id, fresh=fresh)
    except RateLimitError:
        await target.answer("Лимит LLM исчерпан, попробуй позже.")
        return
    except Exception:
        logging.exception("ideal_date: ошибка генерации")
        await target.answer("Не удалось придумать идею — попробуй ещё раз.")
        return

    if not data:
        await target.answer(
            f"Пока маловато сообщений от {name}, чтобы зацепиться за что-то "
            f"конкретное — нужно хотя бы {IDEAL_DATE_MIN_MSGS} его сообщений "
            "(JSON-экспорт или накопление через Автоматизацию чатов)."
        )
        return

    await _answer_long(target, _format_ideal_date(name, data), reply_markup=ideal_date_result_kb(contact_id))


async def _show_ideal_date(
    message: Message, bot: Bot, telegram_id: str | None = None, edit: bool = False,
) -> None:
    telegram_id = telegram_id or str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await _send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        await _run_ideal_date(bot, message, telegram_id, contacts[0]["id"], edit=edit)
        return

    if edit:
        await message.edit_text("С кем свидание?", reply_markup=contacts_kb(contacts, "idealdate"))
    else:
        await message.answer("С кем свидание?", reply_markup=contacts_kb(contacts, "idealdate"))


@router.callback_query(F.data.startswith("idealdate_refresh:"))
async def cb_ideal_date_refresh(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer("Придумываю другую идею...")
    delete_ideal_date(contact_id)
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, fresh=True)


@router.callback_query(F.data.startswith("idealdate:"))
async def cb_ideal_date_contact(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, edit=True)
