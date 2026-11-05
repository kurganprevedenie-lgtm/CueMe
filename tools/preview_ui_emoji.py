"""Приватный предпросмотр без polling, без БД и без рассылки.

py -3.13 -m tools.preview_ui_emoji --send
Отправляет только основному админу из конфигурации. Не запускает рабочего бота.
"""
import argparse
import asyncio
import html
import json

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_TELEGRAM_ID, ADMIN_TELEGRAM_IDS, APP_NAME, BOT_TOKEN
from ui_emoji import ICONS, icon, install_ui_emoji

LABELS = {
    "reply": "Ответ с ассистентом", "analysis": "Анализ общения", "date": "Свидание",
    "new_dialogue": "Новый диалог", "profile": "Профиль", "friends": "Приглашение друзей",
    "premium": "Premium", "gift": "Подарок и бонус", "activation": "Активация Premium",
    "settings": "Настройки", "support": "Поддержка", "home": "Вернуться в меню",
    "success": "Успех", "waiting": "Ожидание", "warning": "Предупреждение",
    "refresh": "Другие варианты", "deleted": "Удалённые сообщения", "stars": "Оплата Stars",
    "locked": "Закрытая функция", "welcome": "Приветствие", "warmth": "Бонус за друга",
    "media": "Исчезающие медиа", "edited": "Изменённые сообщения", "more": "Дополнительные команды",
}


async def send_preview(bot: Bot, chat_id: int) -> dict:
    """Вызывающий обязательно проверяет администратора. Три личных сообщения."""
    # Только текстовые заголовки/чистые клавиатуры, не вызываем оплату,
    # генерацию, счётчики или показ экранов с побочными действиями.
    from handlers.redeem_menu import main_menu_kb, _MAIN_MENU_TEXT
    lines = [f"<b>Предпросмотр {html.escape(APP_NAME)}</b>", "Только для администратора.", ""]
    lines.extend(f"{icon(role)} {LABELS[role]}" for role in ICONS)
    probe = await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML",
                                  reply_markup=main_menu_kb())
    message_ids = {e.custom_emoji_id for e in (probe.entities or []) if e.type == "custom_emoji"}
    buttons = [b for row in (probe.reply_markup.inline_keyboard if probe.reply_markup else []) for b in row]
    button_ids = {b.icon_custom_emoji_id for b in buttons if b.icon_custom_emoji_id}
    await bot.send_message(chat_id, _MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb())
    # Декоративные кнопки демонстрации возвращают в существующее меню.
    await bot.send_message(
        chat_id,
        f"{icon('profile')} Профиль\n\nКак к тебе обращаться?\n\n"
        f"{icon('waiting')} Собираю анализ…\n\n"
        f"{icon('success')} Готово, бот подключён!\n\n"
        f"{icon('warning')} Не получилось собрать ответ, попробуй ещё раз.\n\n"
        f"{icon('date')} Идеальное свидание\n\nПример оформления — без генерации и списания попыток.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="back_to_menu")]]),
    )
    return dict(custom_entities=len(message_ids), custom_button_icons=len(button_ids),
                all_selected_ids_returned=set(i.id for i in ICONS.values()).issubset(message_ids))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="отправить предпросмотр основному админу")
    args = parser.parse_args()
    if not args.send:
        print("Отправка выключена. Для личного предпросмотра: --send")
        return
    owner = ADMIN_TELEGRAM_ID
    if not owner and len(ADMIN_TELEGRAM_IDS) == 1:
        owner = next(iter(ADMIN_TELEGRAM_IDS))
    if not owner or owner not in ADMIN_TELEGRAM_IDS:
        print("Не определён основной администратор; сообщения не отправлены.")
        return
    # Никаких getUpdates: работающий polling на сервере не затрагивается.
    bot = Bot(token=BOT_TOKEN)
    install_ui_emoji(bot)
    try:
        print(json.dumps(await send_preview(bot, int(owner)), ensure_ascii=False))
    except Exception as exc:
        print("Предпросмотр не отправлен полностью:", type(exc).__name__)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
