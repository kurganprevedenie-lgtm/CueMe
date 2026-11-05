"""Кнопки/маршруты, безопасные заголовки, откат при отказе Telegram."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage, SendRichMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

import main
import ui_emoji as ui


def test_ids_match_real_get_sticker_set_response():
    catalog = json.loads((Path(__file__).parent / "fixtures/ui_emoji_selection.json").read_text(encoding="utf-8"))
    assert set(catalog) == set(ui.ICONS)
    for role, icon in ui.ICONS.items():
        actual = catalog[role]
        assert actual["custom_emoji_id"] == icon.id
        assert actual["emoji"] == icon.emoji
        assert actual["set"] == icon.pack
        assert actual["is_animated"] and not actual["is_video"]
    assert {i.pack for i in ui.ICONS.values()} == {"BirthdayCollection", "NewsEmoji", "RestrictedEmoji", "MadEmoji2"}


def test_main_menu_callbacks_unchanged_and_one_icon_per_button():
    kb = main.main_menu_kb()
    assert [len(row) for row in kb.inline_keyboard] == [2, 2, 1, 1]
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["mm:unified", "mm:deep", "mm:date", "watch:menu", "show_premium", "mm:support"]
    assert all(b.icon_custom_emoji_id and "<tg-emoji" not in b.text for b in buttons)
    assert buttons[0].text == "Ответ с CueMe"


def test_plain_fallback_retains_original_button_text_and_actions(monkeypatch):
    monkeypatch.setattr(ui, "UI_CUSTOM_EMOJI", False)
    assert ui.icon("success") == "✅"
    markup = main.main_menu_kb()
    assert markup.inline_keyboard[0][0].text == main.BTN_UNIFIED
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id is None


def test_contact_names_never_decorated():
    contacts = [{"id": 7, "display_name": "💬 Ответ с CueMe"}]
    kb = main.unified_contacts_kb(contacts)
    first = kb.inline_keyboard[0][0]
    assert first.text == "💬 Ответ с CueMe" and first.icon_custom_emoji_id is None
    assert first.callback_data == "unified_contact:7"
    assert kb.inline_keyboard[1][0].callback_data == "unified_contact:new"
    assert kb.inline_keyboard[1][0].icon_custom_emoji_id == ui.ICONS["new_dialogue"].id


def test_theme_does_not_mutate_shared_markup():
    original = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="back_to_menu")]])
    themed = ui.theme_keyboard(original)
    assert original.inline_keyboard[0][0].text == "⬅️ Вернуться в меню"
    assert original.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert themed.inline_keyboard[0][0].text == "Вернуться в меню"


def test_heading_escapes_but_does_not_replace_llm_or_quote_emojis():
    raw = '💐 Идеальное свидание — <Имя>\n\n💬 "Мой ответ" <tg-emoji emoji-id="1">👑</tg-emoji>'
    result = ui.plain_heading(raw, "date")
    assert result.count("<tg-emoji ") == 1
    assert "💬 &quot;Мой ответ&quot;" in result
    assert "&lt;Имя&gt;" in result and "&lt;tg-emoji" in result


def test_transport_fallback_retains_callback_url_and_original_copy():
    method = SendMessage(chat_id=1, text=ui.icon("success") + " Готово", parse_mode="HTML", reply_markup=main.main_menu_kb())
    plain = ui.ordinary_method(method)
    assert plain.text == "✅ Готово"
    assert plain.reply_markup.inline_keyboard[0][0].text == main.BTN_UNIFIED
    assert plain.reply_markup.inline_keyboard[0][0].callback_data == "mm:unified"
    assert method.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id


def test_unsupported_custom_emoji_retries_once_then_uses_plain_ui():
    async def run():
        calls = []
        middleware = ui.UiEmojiFallback()
        method = SendMessage(chat_id=1, text=ui.icon("warning"), parse_mode="HTML")
        async def request(bot, sent):
            calls.append(sent)
            if len(calls) == 1:
                raise TelegramBadRequest(method=sent, message="CUSTOM_EMOJI_NOT_ALLOWED")
            return "ok"
        assert await middleware(request, None, method) == "ok"
        assert len(calls) == 2 and calls[-1].text == "⚠️"
        assert not middleware.available
        await middleware(request, None, method)
        assert calls[-1].text == "⚠️"
    asyncio.run(run())


@pytest.mark.parametrize("error", ["chat not found", "message is not modified"])
def test_unrelated_errors_are_not_masked(error):
    async def run():
        calls = []
        method = SendMessage(chat_id=1, text=ui.icon("success"))
        async def request(bot, sent):
            calls.append(sent)
            raise TelegramBadRequest(method=sent, message=error)
        with pytest.raises(TelegramBadRequest):
            await ui.UiEmojiFallback()(request, None, method)
        assert len(calls) == 1
    asyncio.run(run())


def test_network_error_does_not_duplicate_message():
    async def run():
        calls = []
        method = SendMessage(chat_id=1, text=ui.icon("success"))
        async def request(bot, sent):
            calls.append(sent)
            raise TelegramNetworkError(method=sent, message="timeout")
        with pytest.raises(TelegramNetworkError):
            await ui.UiEmojiFallback()(request, None, method)
        assert len(calls) == 1
    asyncio.run(run())


def test_arbitrary_user_custom_emoji_untouched():
    text = '<tg-emoji emoji-id="111">😎</tg-emoji>'
    method = SendMessage(chat_id=1, text=text)
    assert ui.ordinary_method(method).text == text
    assert not ui._has_ui_icons(method)


def test_silent_stripping_does_not_duplicate_and_disables_custom_ui():
    async def run():
        calls = []
        middleware = ui.UiEmojiFallback()
        method = SendMessage(chat_id=1, text=ui.icon("success"), parse_mode="HTML")
        async def request(bot, sent):
            calls.append(sent)
            return SimpleNamespace(entities=[], caption_entities=None)
        await middleware(request, None, method)
        assert len(calls) == 1 and not middleware.available
        await middleware(request, None, method)
        assert calls[-1].text == "✅"
    asyncio.run(run())


def test_emoji_preview_denies_non_admin(monkeypatch):
    from handlers import service_cmds
    called = []
    monkeypatch.setattr(service_cmds, "_is_admin", lambda uid: False)
    async def send(*args):
        called.append(args)
    import tools.preview_ui_emoji as preview
    monkeypatch.setattr(preview, "send_preview", send)
    asyncio.run(service_cmds.cmd_emoji_preview(SimpleNamespace(from_user=SimpleNamespace(id=999)), object()))
    assert called == []


def test_emoji_preview_from_group_always_goes_to_admin_private_chat(monkeypatch):
    from handlers import service_cmds
    import tools.preview_ui_emoji as preview
    monkeypatch.setattr(service_cmds, "_is_admin", lambda uid: True)
    called = []
    async def send(bot, uid):
        called.append(uid)
    monkeypatch.setattr(preview, "send_preview", send)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=-100))
    asyncio.run(service_cmds.cmd_emoji_preview(message, object()))
    assert called == [123]


def test_long_llm_text_is_split_before_html_escaping():
    async def run():
        calls = []
        class Message:
            async def edit_text(self, text, **kwargs):
                calls.append(("edit", text, kwargs))
            async def answer(self, text, **kwargs):
                calls.append(("answer", text, kwargs))
        content = "💐 Идеальное свидание — <Аня>\n\n" + "&💬<" * 3000
        keyboard = main.main_menu_kb()
        await ui.edit_plain_ui_long(Message(), content, "date", keyboard)
        assert calls[0][0] == "edit" and all(c[0] == "answer" for c in calls[1:])
        assert sum(c[1].count("<tg-emoji ") for c in calls) == 1
        assert all(c[2]["parse_mode"] == "HTML" for c in calls)
        assert calls[-1][2]["reply_markup"] is keyboard
        assert all(c[2]["reply_markup"] is None for c in calls[:-1])
        import html
        for _, text, _ in calls:
            assert len(html.unescape(ui.re.sub(r"<tg-emoji[^>]*>|</tg-emoji>", "", text)).encode("utf-16-le")) <= 4096 * 2
    asyncio.run(run())


def test_rich_message_fallback_keeps_other_formatting():
    rich = f"<p><b>{ui.icon('analysis')} Анализ</b></p><blockquote>💬 Цитата</blockquote>"
    method = SendRichMessage(chat_id=1, rich_message=InputRichMessage(html=rich))
    assert ui._has_ui_icons(method)
    plain = ui.ordinary_method(method)
    assert plain.rich_message.html == "<p><b>🔬 Анализ</b></p><blockquote>💬 Цитата</blockquote>"
    assert method.rich_message.html == rich


def test_tariff_buttons_keep_payment_payload_and_label(monkeypatch):
    from handlers import subscription
    monkeypatch.setattr(ui, "UI_CUSTOM_EMOJI", False)
    plain = subscription.stars_tariff_kb()
    monkeypatch.setattr(ui, "UI_CUSTOM_EMOJI", True)
    themed = subscription.stars_tariff_kb()
    assert [b.callback_data for row in themed.inline_keyboard for b in row] == [
        b.callback_data for row in plain.inline_keyboard for b in row]
    restored = ui.ordinary_method(SendMessage(chat_id=1, text="Тарифы", reply_markup=themed))
    assert restored.reply_markup.model_dump() == plain.model_dump()


def test_premium_feature_list_uses_matching_custom_icons():
    from handlers import premium_screen
    text = premium_screen._PREMIUM_FEATURES_HTML
    assert text.startswith("<blockquote>") and text.endswith("</blockquote>")
    assert text.count("<tg-emoji ") == 4
    for role in ("reply", "analysis", "date", "deleted"):
        assert ui.ICONS[role].id in text


@pytest.mark.parametrize("prefix,role", [("🎁", "gift"), ("⭐", "stars"), ("💎", "premium")])
def test_premium_expiry_icon_is_not_escaped(monkeypatch, prefix, role):
    from handlers import premium_screen
    async def premium(*args):
        return True
    monkeypatch.setattr(premium_screen, "_is_premium", premium)
    monkeypatch.setattr(premium_screen, "_premium_expiry_line", lambda tid: f"{prefix} Срок <тест> & дата")
    text = asyncio.run(premium_screen._premium_status_text(object(), "1"))
    assert f'{ui.icon(role)} Срок &lt;тест&gt; &amp; дата' in text
    assert "&lt;tg-emoji" not in text


@pytest.mark.parametrize("edit", [False, True])
def test_help_custom_icons_and_commands_preserved(edit):
    from handlers import service_cmds
    calls = []
    class Message:
        async def answer(self, text, **kwargs):
            calls.append(("answer", text, kwargs))
        async def edit_text(self, text, **kwargs):
            calls.append(("edit", text, kwargs))
    asyncio.run(service_cmds._show_help(Message(), edit=edit))
    action, text, kwargs = calls[0]
    assert action == ("edit" if edit else "answer")
    assert kwargs["parse_mode"] == "HTML"
    for role in ("support", "reply", "analysis", "date", "media", "edited", "premium", "gift", "settings", "more"):
        assert ui.icon(role) in text
    for command in ("/reply", "/deep_analysis", "/invite", "/contacts", "/connect", "/progress", "/redeem", "/myref", "/premium", "/rebuild", "/delete", "/start", "/help"):
        assert command in text
    assert "&lt;tg-emoji" not in text
