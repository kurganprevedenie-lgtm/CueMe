"""/redeem, /myref, /menu и главное меню.

Вынесено из main.py без изменения логики (строки 2961–3207 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import logging
from pathlib import Path
from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import MAIN_MENU_LOGO_FILE_ID, MAIN_MENU_LOGO_PATH
from storage import (
    count_successful_referrals,
    get_reply_trial_bonus,
    get_analysis_trial_bonus,
    get_date_trial_bonus,
    get_pending_referral,
    get_referrer_by_code,
    is_legacy_kb_cleared,
    mark_legacy_kb_cleared,
    list_contacts,
    save_referral_pending,
)
from app_state import (
    BTN_DATE,
    BTN_DEEP,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_UNIFIED,
    BTN_WATCH,
    _PROGRESS_FRAMES,
    dp,
)
import main  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


class ReferralRedeem(StatesGroup):
    waiting_for_code = State()


@dp.message(Command("redeem"))
async def cmd_redeem(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await _process_redeem(message, parts[1].strip())
        return
    await state.set_state(ReferralRedeem.waiting_for_code)
    await message.answer("Введи код от друга:")


@dp.message(ReferralRedeem.waiting_for_code)
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


@dp.message(Command("myref"))
async def cmd_myref(message: Message) -> None:
    telegram_id = str(message.from_user.id)
    count = count_successful_referrals(telegram_id)
    lines = ["🎁 Награда за рефералов:\n"]

    reply_bonus = get_reply_trial_bonus(telegram_id)
    analysis_bonus = get_analysis_trial_bonus(telegram_id)
    date_bonus = get_date_trial_bonus(telegram_id)
    if reply_bonus or analysis_bonus or date_bonus:
        lines.append(
            f"✅ Бонусные попытки: +{reply_bonus} «Ответить за меня», "
            f"+{analysis_bonus} «Анализ собеседника», +{date_bonus} «Идеальное свидание»"
        )
    else:
        lines.append("⏳ Активной награды нет — пригласи друга через /invite")

    lines.append(f"👥 Приведено друзей: {count}")
    await message.answer("\n".join(lines))


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


_MAIN_MENU_TEXT = (
    "👋 Вот что я умею:\n\n"
    "💬 Ответ с CueMe — подскажу, что написать в моменте\n"
    "🔬 Анализ собеседника — разберу вашу переписку по фактам\n"
    "💐 Идеальное свидание — накидаю идеи для свидания"
)


def main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню — 6 пунктов, той же вёрстки/механики, что «Подписка»
    (premium_menu_kb): inline-кнопки на одном сообщении, редактируемом при
    переходах. «👑 Подписка» ведёт в уже существующую edit-in-place иерархию
    (callback_data="show_premium" — тот же, что и «⬅️ Назад» из Реферальной
    системы, см. cb_show_premium). Остальные — одноразовые действия
    (мультишаговые FSM-флоу/результаты своим сообщением), не вложенные
    экраны — см. cb_main_menu_action."""
    b = InlineKeyboardBuilder()
    b.button(text=BTN_UNIFIED, callback_data="mm:unified")
    b.button(text=BTN_DEEP, callback_data="mm:deep")
    b.button(text=BTN_DATE, callback_data="mm:date")
    b.button(text=BTN_WATCH, callback_data="watch:menu")
    b.button(text=BTN_SUBSCRIPTION, callback_data="show_premium")
    b.button(text=BTN_SUPPORT, callback_data="mm:support")
    b.adjust(1)
    return b.as_markup()


async def _send_main_menu(target: Message, edit: bool = False) -> None:
    """Экран главного меню — общий для /menu, кнопки «⬅️ Вернуться в меню»
    под результатами генерации и возврата «⬅️ Назад» из «👑 Подписка».
    edit=True (Назад из Подписки) — редактирует ТО ЖЕ сообщение (та же
    механика, что и у самой Подписки). edit=False — новое сообщение, с
    логотипом (MAIN_MENU_LOGO_*), если он настроен. Для edit=True логотипа
    нет намеренно: edit_text не может превратить текстовое сообщение в фото.
    Обратное тоже нельзя — поэтому кнопки, которые редактируют меню на
    месте, сначала подменяют фото-меню текстовым (_menu_screen_target).

    target.chat.id — telegram_id юзера в обоих случаях (личный чат с ботом,
    chat.id == user id даже когда target это call.message, чей from_user —
    бот, не юзер)."""
    telegram_id = str(target.chat.id)
    if not is_legacy_kb_cleared(telegram_id):
        # Старая persistent reply-клавиатура (main_kb(), убрана из кода) у
        # части юзеров ещё физически видна на устройстве — inline-меню сверху
        # её не перекрывает, нужен отдельный reply_markup=ReplyKeyboardRemove().
        # Разово, маленьким отдельным сообщением перед самим меню (снять
        # клавиатуру и отредактировать существующее сообщение одним вызовом
        # нельзя — это разные типы reply_markup).
        try:
            await target.bot.send_message(
                target.chat.id,
                "Старые кнопки внизу экрана отключены — всё нужное теперь в меню ниже.",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            logging.warning("legacy kb clear failed: telegram_id=%s", telegram_id)
        mark_legacy_kb_cleared(telegram_id)

    if edit:
        await target.edit_text(_MAIN_MENU_TEXT, reply_markup=main_menu_kb())
    else:
        # Логотип — только для НОВОГО сообщения (edit_text не может превратить
        # текст в фото, см. коммент у функции). Тот же приоритет путей, что и
        # у онбординг-фото: файл на диске → file_id → голый текст.
        photo_path = Path(MAIN_MENU_LOGO_PATH) if MAIN_MENU_LOGO_PATH else None
        if photo_path and photo_path.is_file():
            await target.answer_photo(
                photo=FSInputFile(photo_path),
                caption=_MAIN_MENU_TEXT,
                reply_markup=main_menu_kb(),
            )
        elif MAIN_MENU_LOGO_FILE_ID:
            await target.answer_photo(
                photo=MAIN_MENU_LOGO_FILE_ID,
                caption=_MAIN_MENU_TEXT,
                reply_markup=main_menu_kb(),
            )
        else:
            await target.answer(_MAIN_MENU_TEXT, reply_markup=main_menu_kb())


async def _menu_screen_target(msg: Message) -> Message:
    """Экраны, открываемые из главного меню на месте (Подписка, Анализ,
    Свидание, Поддержка), редактируют сообщение через edit_text — на
    фото-меню (с логотипом) это падает: фото нельзя превратить в текст.
    Тогда вместо фото-меню ставим текстовое сообщение-заглушку и
    возвращаем его — дальше экран редактирует уже его, как раньше."""
    if not msg.photo:
        return msg
    placeholder = await msg.answer(_PROGRESS_FRAMES[0])
    try:
        await msg.delete()
    except Exception:
        logging.warning("menu photo delete failed: chat=%s", msg.chat.id)
    return placeholder


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await _send_main_menu(message)


@dp.callback_query(F.data.startswith("mm:"))
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
        await main._start_unified_reply(call.message, state)
    elif action == "deep":
        await main._show_deep_analysis(await _menu_screen_target(call.message), bot, telegram_id, edit=True)
    elif action == "date":
        await main._show_ideal_date(await _menu_screen_target(call.message), bot, telegram_id, edit=True)
    elif action == "support":
        await main._show_help(await _menu_screen_target(call.message), edit=True)


_BACK_TO_MENU_BUTTON = InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="back_to_menu")


def _with_back_to_menu(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавляет строку «⬅️ Вернуться в меню» под уже собранной клавиатурой
    результата генерации (Ответ с CueMe / Анализ собеседника / Идеальное
    свидание) — не трогает остальные кнопки той клавиатуры."""
    markup.inline_keyboard.append([_BACK_TO_MENU_BUTTON])
    return markup


@dp.callback_query(F.data == "back_to_menu")
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
