"""Главное меню: /menu, тапы по inline-кнопкам меню (mm:*), возврат
«⬅️ Вернуться в меню» под результатами генерации и текстовый фолбэк для тех,
у кого ещё висит старая reply-клавиатура.

Сам экран меню (_MAIN_MENU_TEXT/main_menu_kb/_send_main_menu) живёт в
handlers/common.py — на него ссылаются и «Подписка», и онбординг, см.
комментарий там.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.analysis import _show_deep_analysis
from handlers.common import (
    BTN_DATE,
    BTN_DEEP,
    BTN_HELP,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_UNIFIED,
    _ALL_BTNS,
    _send_main_menu,
)
from handlers.date_ideas import _show_ideal_date
from handlers.referral import _show_invite
from handlers.reply_flow import _start_unified_reply
from handlers.subscription import _show_premium_screen
from handlers.support import _show_help

router = Router(name="main_menu")


# main_kb() (persistent reply-клавиатура) убрана совсем по запросу — главное
# меню теперь то же самое, что «Подписка»: одно сообщение с inline-кнопками,
# которое редактируется при переходах, а не нижняя панель. Не удалена
# физически — на случай отката (но ReplyKeyboardRemove в местах, которые
# раньше её отправляли, теперь активно снимает эту клавиатуру у тех, у кого
# она ещё видна с более ранней версии бота — см. handle_business_connection/
# handle_document ниже).
# def main_kb() -> ReplyKeyboardMarkup:
#     b = ReplyKeyboardBuilder()
#     b.row(KeyboardButton(text=BTN_UNIFIED))
#     b.row(KeyboardButton(text=BTN_DEEP), KeyboardButton(text=BTN_DATE))
#     b.row(KeyboardButton(text=BTN_SUBSCRIPTION))
#     b.row(KeyboardButton(text=BTN_SUPPORT))
#     return b.as_markup(resize_keyboard=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await _send_main_menu(message)


@router.callback_query(F.data.startswith("mm:"))
async def cb_main_menu_action(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«unified»/«deep»/«date» — одноразовые действия (новое сообщение/
    FSM-флоу), само сообщение главного меню не трогают. «support» — как
    «👑 Подписка» (см. main_menu_kb): вложенный edit-экран, «⬅️ Назад»
    (help_kb → callback_data="sub:to_menu") редактирует обратно в меню."""
    action = call.data.split(":", 1)[1]
    await call.answer()
    await state.clear()
    telegram_id = str(call.from_user.id)
    if action == "unified":
        await _start_unified_reply(call.message, state)
    elif action == "deep":
        await _show_deep_analysis(call.message, bot, telegram_id, edit=True)
    elif action == "date":
        await _show_ideal_date(call.message, bot, telegram_id, edit=True)
    elif action == "support":
        await _show_help(call.message, edit=True)


@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(call: CallbackQuery) -> None:
    """Не редактирует сообщение с результатом (его контент остаётся в
    истории чата нетронутым) — присылает главное меню НОВЫМ сообщением,
    тем же способом, что /menu."""
    await call.answer()
    await _send_main_menu(call.message)


# more_menu_kb убрана вместе с BTN_MORE — «Идеальное свидание» стало кнопкой
# первого уровня, «Скрипты общения» убраны совсем, «Пригласить друга»
# доступно через «👑 Подписка»/командой /invite. Не удалена физически —
# на случай отката.
# def more_menu_kb() -> InlineKeyboardMarkup:
#     b = InlineKeyboardBuilder()
#     b.button(text=BTN_DATE, callback_data="menu:date")
#     b.button(text=BTN_REVIVE, callback_data="menu:revive")
#     b.button(text=BTN_INVITE, callback_data="menu:invite")
#     b.adjust(1)
#     return b.as_markup()


# ── Кнопки главного меню ──────────────────────────────────────────────────────

@router.message(F.text.in_(_ALL_BTNS))
async def handle_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    # if message.text == BTN_SCREENSHOT:
    #     await _start_screenshot(message, state)
    # elif message.text == BTN_REPLY:
    #     await _start_reply(message, state)
    # elif message.text == BTN_LIVE:
    #     await _show_live_start(message)
    if message.text == BTN_UNIFIED:
        await _start_unified_reply(message, state)
    elif message.text == BTN_DEEP:
        await _show_deep_analysis(message, bot)
    elif message.text == BTN_DATE:
        await _show_ideal_date(message, bot)
    elif message.text == BTN_SUBSCRIPTION:
        await _show_premium_screen(message, bot, str(message.from_user.id))
    elif message.text == BTN_HELP:
        await _show_help(message)
    elif message.text == BTN_SUPPORT:
        await _show_help(message)


@router.callback_query(F.data.startswith("menu:"))
async def cb_submenu(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    action = call.data.split(":", 1)[1]
    telegram_id = str(call.from_user.id)
    await call.answer()
    if action == "date":
        await _show_ideal_date(call.message, bot, telegram_id)
    # elif action == "revive":  # «Скрипты общения» убраны совсем — см. BTN_REVIVE
    #     await _show_revive(call.message, state)
    elif action == "invite":
        await _show_invite(call.message, bot, telegram_id)
