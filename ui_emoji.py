"""Оформление только интерфейса: проверенные ID, без сетевых запросов меню.

Наборы получены getStickerSet 2026-11-05, превью просмотрены вручную.
Исходный текст кнопок/FSM и текст пользовательских переписок не заменяем.
"""
from dataclasses import dataclass
import html
import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from config import APP_NAME, UI_CUSTOM_EMOJI


@dataclass(frozen=True)
class Emoji:
    id: str
    emoji: str
    fallback: str
    pack: str
    animated: bool = True


ICONS = {
    "reply": Emoji("5443038326535759644", "💬", "💬", "NewsEmoji"),
    "analysis": Emoji("5379679518740978720", "🔬", "🔬", "RestrictedEmoji"),
    "date": Emoji("5192959294470895031", "💐", "💐", "BirthdayCollection"),
    "new_dialogue": Emoji("5397916757333654639", "➕", "➕", "NewsEmoji"),
    "profile": Emoji("5373012449597335010", "👤", "👤", "RestrictedEmoji"),
    "friends": Emoji("5372926953978341366", "👥", "👥", "RestrictedEmoji"),
    "premium": Emoji("5217822164362739968", "👑", "👑", "NewsEmoji"),
    "gift": Emoji("5449800250032143374", "🎁", "🎁", "BirthdayCollection"),
    "activation": Emoji("5193018401810822951", "🎉", "🎉", "BirthdayCollection"),
    "settings": Emoji("5341715473882955310", "⚙️", "⚙️", "NewsEmoji"),
    "support": Emoji("5452069934089641166", "❓", "🆘", "NewsEmoji"),
    "home": Emoji("5416041192905265756", "🏠", "⬅️", "NewsEmoji"),
    "success": Emoji("5206607081334906820", "✔️", "✅", "NewsEmoji"),
    "waiting": Emoji("5451732530048802485", "⏳", "⏳", "RestrictedEmoji"),
    "warning": Emoji("5447644880824181073", "⚠️", "⚠️", "NewsEmoji"),
    "refresh": Emoji("5264727218734524899", "🔄", "🔄", "RestrictedEmoji"),
    "deleted": Emoji("5445267414562389170", "🗑", "🗑", "NewsEmoji"),
    "stars": Emoji("5438496463044752972", "⭐️", "⭐", "NewsEmoji"),
    "locked": Emoji("5296369303661067030", "🔒", "🔒", "NewsEmoji"),
    "welcome": Emoji("5472055112702629499", "👋", "👋", "RestrictedEmoji"),
    "warmth": Emoji("5213447602732813642", "💞", "💞", "MadEmoji2"),
    "media": Emoji("5305265301917549162", "📎", "💾", "NewsEmoji"),
    "edited": Emoji("5395444784611480792", "✏️", "✏️", "NewsEmoji"),
    "more": Emoji("5334544901428229844", "ℹ️", "🎬", "NewsEmoji"),
}


def icon(role: str) -> str:
    item = ICONS[role]
    return (f'<tg-emoji emoji-id="{item.id}">{item.emoji}</tg-emoji>'
            if UI_CUSTOM_EMOJI else item.fallback)


def plain_heading(text: str, role: str) -> str:
    """Только явно выбранный UI-заголовок; остальное экранируем, не заменяем."""
    _, separator, tail = text.partition(" ")
    return f"{icon(role)} {html.escape(tail)}" if separator else html.escape(text)


async def edit_plain_ui_long(message, text: str, role: str, reply_markup=None):
    """Сначала режем обычный текст, затем экранируем HTML каждого куска.

    Не разрываем &amp;/tg-emoji и не меняем эмодзи в теле LLM-ответа.
    Клавиатура, как у _edit_or_answer_long, только у последнего куска.
    """
    chunks = []
    while text:
        encoded = text.encode("utf-16-le")
        chunk = encoded[:4000 * 2].decode("utf-16-le", errors="ignore")
        if len(chunk) < len(text):
            cut = max(chunk.rfind("\n\n"), chunk.rfind("\n"))
            if cut > 0:
                chunk = chunk[:cut]
        chunks.append(chunk)
        text = text[len(chunk):].lstrip()
    for i, chunk in enumerate(chunks):
        formatted = plain_heading(chunk, role) if i == 0 else html.escape(chunk)
        send = message.edit_text if i == 0 else message.answer
        await send(formatted, parse_mode="HTML", reply_markup=reply_markup if i == len(chunks) - 1 else None)


# Только статические UI-подписи. Не используем для имён контактов/LLM-текста.
_BUTTON_ROLES = {
    f"💬 Ответ с {APP_NAME}": "reply", "🔬 Анализ собеседника": "analysis",
    "💐 Идеальное свидание": "date", "👑 Подписка": "premium",
    "🗑 Удалённые сообщения": "deleted", "🆘 Помощь": "support",
    "🆘 Написать в поддержку": "support", "👥 Реферальная система": "friends",
    "➕ Другой человек": "new_dialogue", "👥 Ответ для другого собеседника": "friends",
    "🔄 Другие варианты": "refresh", "🔄 Попробовать ещё раз": "refresh",
    "🔄 Обновить анализ": "refresh", "🔄 Другая идея": "refresh",
    "👑 Разблокировать полный анализ": "premium", "👑 Разблокировать подарки": "premium",
    "⬅️ Вернуться в меню": "home", "👑 Оформить Premium": "premium",
    "💎 Оформить подписку": "premium", "⭐ Оплатить Stars": "stars",
    "✅ Я оплатил(а) — проверить": "success", "🎯 Живой коучинг": "reply",
    "🎲 Готовые фразы для начала": "new_dialogue",
}


def theme_keyboard(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Копия, без мутации общих кнопок. callback_data, URL и оплата неизменны.

    Кнопки выбора контакта специально исключены: их текст — пользовательские данные.
    Reply-клавиатуры намеренно не принимаются: их text — часть маршрутизации.
    Стрелки «Назад» остаются обычными: среди четырёх наборов нет левой стрелки.
    """
    if not UI_CUSTOM_EMOJI:
        return markup
    result = markup.model_copy(deep=True)
    for row in result.inline_keyboard:
        for button in row:
            cb = button.callback_data or ""
            if cb.startswith(("deepan:", "idealdate:", "mystyle:", "reply:", "setup:", "del:")):
                continue
            if cb.startswith("unified_contact:") and cb != "unified_contact:new":
                continue
            role = _BUTTON_ROLES.get(button.text)
            if cb.startswith("stars_buy:") and button.text.startswith("⭐ "):
                role = "stars"
            if cb.startswith("watch:toggle:") and button.text.startswith("✅ "):
                role = "success"
            if cb == "watch:locked" and button.text.startswith("🔒 "):
                role = "locked"
            if role:
                button.icon_custom_emoji_id = ICONS[role].id
                button.text = button.text.split(" ", 1)[1]
    return result


_IDS = {item.id: item for item in ICONS.values()}


def ordinary_method(method):
    """Откат только оформленных нами полей; сущности цитат/LLM не меняются."""
    result = method.model_copy(deep=True)
    for field in ("text", "caption"):
        text = getattr(result, field, None)
        if isinstance(text, str):
            for item in ICONS.values():
                text = re.sub(r'<tg-emoji emoji-id="' + item.id + r'">.*?</tg-emoji>',
                              lambda _: item.fallback, text, flags=re.DOTALL)
            setattr(result, field, text)
    rich = getattr(result, "rich_message", None)
    if rich is not None and isinstance(getattr(rich, "html", None), str):
        rich_html = rich.html
        for item in ICONS.values():
            rich_html = re.sub(r'<tg-emoji emoji-id="' + item.id + r'">.*?</tg-emoji>',
                               lambda _: item.fallback, rich_html, flags=re.DOTALL)
        result.rich_message = rich.model_copy(update={"html": rich_html})
    media = getattr(result, "media", None)
    if media is not None and isinstance(getattr(media, "caption", None), str):
        caption = media.caption
        for item in ICONS.values():
            caption = re.sub(r'<tg-emoji emoji-id="' + item.id + r'">.*?</tg-emoji>',
                             lambda _: item.fallback, caption, flags=re.DOTALL)
        result.media = media.model_copy(update={"caption": caption})
    markup = getattr(result, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            for button in row:
                item = _IDS.get(button.icon_custom_emoji_id)
                if item:
                    button.icon_custom_emoji_id = None
                    button.text = f"{item.fallback} {button.text}"
    return result


def _has_ui_icons(method):
    for field in ("text", "caption"):
        if any(f'emoji-id="{i}"' in (getattr(method, field, None) or "") for i in _IDS):
            return True
    rich = getattr(method, "rich_message", None)
    if rich is not None and isinstance(getattr(rich, "html", None), str):
        if any(f'emoji-id="{i}"' in rich.html for i in _IDS):
            return True
    media = getattr(method, "media", None)
    if media is not None and isinstance(getattr(media, "caption", None), str):
        if any(f'emoji-id="{i}"' in media.caption for i in _IDS):
            return True
    markup = getattr(method, "reply_markup", None)
    return isinstance(markup, InlineKeyboardMarkup) and any(
        b.icon_custom_emoji_id in _IDS for row in markup.inline_keyboard for b in row)


class UiEmojiFallback(BaseRequestMiddleware):
    """При отказе Telegram один повтор обычными эмодзи, далее — обычный UI.

    Сетевые ошибки/429 не повторяем: нельзя продублировать доставленное сообщение.
    """
    def __init__(self):
        self.available = UI_CUSTOM_EMOJI

    async def __call__(self, make_request, bot, method):
        if not _has_ui_icons(method):
            return await make_request(bot, method)
        if not self.available:
            return await make_request(bot, ordinary_method(method))
        try:
            result = await make_request(bot, method)
            # Некоторые версии API молча убирают custom_emoji. Уже доставленное
            # сообщение не повторяем, следующие экраны показываем обычными.
            tagged = any('emoji-id="' + item.id + '"' in (getattr(method, field, None) or "")
                         for item in ICONS.values() for field in ("text", "caption"))
            if tagged and hasattr(result, "entities"):
                returned = list(result.entities or []) + list(getattr(result, "caption_entities", None) or [])
                if not any(e.type == "custom_emoji" and e.custom_emoji_id in _IDS for e in returned):
                    self.available = False
                    logging.warning("UI: Telegram не сохранил custom_emoji; следующие экраны — обычными")
            return result
        except TelegramBadRequest as exc:
            error = exc.message.lower()
            if not any(part in error for part in ("emoji", "premium", "button_icon", "not enough rights")):
                raise
            logging.warning("UI: Telegram отклонил кастомные эмодзи; использую обычные")
            self.available = False
            return await make_request(bot, ordinary_method(method))


def install_ui_emoji(bot):
    middleware = UiEmojiFallback()
    bot.session.middleware(middleware)
    return middleware
