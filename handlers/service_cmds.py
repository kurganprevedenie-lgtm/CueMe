"""/rebuild, /progress, /help.

Вынесено из main.py без изменения логики (строки 9970–10120 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import html
import logging
from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message
from config import (
    FIRST_BUILD_THRESHOLD,
    PROMO_CHANNEL_USERNAME,
    PROMO_CHANNEL_REPLY_BONUS,
    PROMO_CHANNEL_ANALYSIS_BONUS,
    PROMO_CHANNEL_DATE_BONUS,
    REBUILD_THRESHOLD,
    REFERRAL_REPLY_BONUS,
    REFERRAL_ANALYSIS_BONUS,
    REFERRAL_DATE_BONUS,
)
from llm import RateLimitError, build_overall_style
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    get_all_per_contact_style_cards,
    get_my_style_last_rebuild_count,
    get_my_style_per_contact,
    list_contacts,
    save_style_card,
)
from app_state import _contact_name, dp
from handlers.subscription import _require_premium
from handlers.cards import _rebuild_contact
from handlers.start_menu import help_kb, support_kb


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
