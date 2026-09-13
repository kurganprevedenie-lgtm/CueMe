"""🆘 Помощь: список команд (/help и кнопка главного меню) плюс ссылка на
поддержку @CueMeSupport.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import FREE_TRIAL_REQUESTS, REFERRAL_REWARD_DAYS

router = Router(name="support")


def support_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🆘 Написать в поддержку", url="https://t.me/CueMeSupport")
    return b.as_markup()


def help_kb() -> InlineKeyboardMarkup:
    """Как support_kb(), плюс «⬅️ Назад» — для _show_help(edit=True) из
    главного меню (callback_data="sub:to_menu" — тот же таргет, что и
    «Назад» из «Подписка», см. cb_sub_to_main_menu, работает для любого
    сообщения, не только для экрана Подписки)."""
    b = InlineKeyboardBuilder()
    b.button(text="🆘 Написать в поддержку", url="https://t.me/CueMeSupport")
    b.button(text="⬅️ Назад", callback_data="sub:to_menu")
    b.adjust(1)
    return b.as_markup()


# _show_support — раньше показывала только ссылку на @CueMeSupport по тапу
# «🆘 Помощь» в главном меню. По запросу «в кнопке помощь должен быть список
# команд» — BTN_SUPPORT теперь ведёт на _show_help (тот же список, что и
# BTN_HELP//help), с кнопкой на поддержку внизу (support_kb в _show_help).
# Не удалена физически — на случай отката.
# async def _show_support(message: Message) -> None:
#     await message.answer(
#         "Если что-то не работает или есть вопрос — пиши сюда:",
#         reply_markup=support_kb(),
#     )


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
        "<b>👑 Подписка</b> (кнопка в меню) — статус подписки + "
        f"🎁 Пригласить друга (/invite) — получить свой код, за друга по коду дадим "
        f"{REFERRAL_REWARD_DAYS} дня Premium подписки\n\n"
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
        f"💎 {FREE_TRIAL_REQUESTS} бесплатных попыток на ответ, "
        "дальше и остальные функции — по подписке. Статус — /premium."
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=help_kb())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=support_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show_help(message)
