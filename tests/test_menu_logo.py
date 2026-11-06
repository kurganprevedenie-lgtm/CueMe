"""Логотип сохраняется при возвращении из текстовых экранов."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InputMediaPhoto
from aiogram.methods import EditMessageMedia

import main
import ui_emoji
from handlers import redeem_menu as menu


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(menu, "is_legacy_kb_cleared", lambda tid: True)
    monkeypatch.setattr(menu, "MAIN_MENU_LOGO_PATH", "")
    monkeypatch.setattr(menu, "MAIN_MENU_LOGO_FILE_ID", "saved-logo")


class Message:
    def __init__(self, edit_error=None, send_error=None):
        self.chat = SimpleNamespace(id=1)
        self.calls = []
        self.edit_error = edit_error
        self.send_error = send_error

    async def edit_media(self, media, **kwargs):
        self.calls.append(("edit_media", media, kwargs))
        if self.edit_error:
            raise self.edit_error

    async def answer_photo(self, **kwargs):
        self.calls.append(("answer_photo", kwargs))
        if self.send_error:
            raise self.send_error

    async def delete(self):
        self.calls.append(("delete",))

    async def edit_text(self, text, **kwargs):
        self.calls.append(("edit_text", text, kwargs))


def test_return_restores_photo_in_same_message(configured):
    target = Message()
    asyncio.run(menu._send_main_menu(target, edit=True))
    assert [c[0] for c in target.calls] == ["edit_media"]
    _, media, kwargs = target.calls[0]
    assert isinstance(media, InputMediaPhoto) and media.media == "saved-logo"
    assert media.caption == menu._MAIN_MENU_TEXT and media.parse_mode == "HTML"
    assert [len(row) for row in kwargs["reply_markup"].inline_keyboard] == [1, 1, 1, 1, 2]


def test_new_menu_also_has_same_logo(configured):
    target = Message()
    asyncio.run(menu._send_main_menu(target))
    assert target.calls[0][0] == "answer_photo"
    assert target.calls[0][1]["photo"] == "saved-logo"


def test_uneditable_screen_replaced_only_after_photo_sent(configured):
    target = Message(edit_error=TelegramBadRequest(method=None, message="message can't be edited"))
    asyncio.run(menu._send_main_menu(target, edit=True))
    assert [c[0] for c in target.calls] == ["edit_media", "answer_photo", "delete"]


def test_failed_photo_send_does_not_delete_old_screen(configured):
    target = Message(edit_error=TelegramBadRequest(method=None, message="message can't be edited"),
                     send_error=TelegramNetworkError(method=None, message="timeout"))
    with pytest.raises(TelegramNetworkError):
        asyncio.run(menu._send_main_menu(target, edit=True))
    assert "delete" not in [c[0] for c in target.calls]


def test_network_error_during_edit_does_not_send_duplicate_menu(configured):
    target = Message(edit_error=TelegramNetworkError(method=None, message="timeout"))
    with pytest.raises(TelegramNetworkError):
        asyncio.run(menu._send_main_menu(target, edit=True))
    assert [c[0] for c in target.calls] == ["edit_media"]


def test_identical_menu_is_not_resent(configured):
    target = Message(edit_error=TelegramBadRequest(method=None, message="Bad Request: message is not modified"))
    asyncio.run(menu._send_main_menu(target, edit=True))
    assert [c[0] for c in target.calls] == ["edit_media"]


def test_configured_local_file_precedes_file_id(configured, monkeypatch, tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"test-image")
    monkeypatch.setattr(menu, "MAIN_MENU_LOGO_PATH", str(logo))
    target = Message()
    asyncio.run(menu._send_main_menu(target, edit=True))
    assert isinstance(target.calls[0][1].media, FSInputFile)
    assert target.calls[0][1].media.path == logo


def test_existing_server_logo_used_without_extra_env_setting(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(menu, "MAIN_MENU_LOGO_FILE_ID", "")
    monkeypatch.setattr(menu, "__file__", str(tmp_path / "handlers" / "redeem_menu.py"))
    (tmp_path / "CueMeLogo1.png").write_bytes(b"test-image")
    target = Message()
    asyncio.run(menu._send_main_menu(target, edit=True))
    assert isinstance(target.calls[0][1].media, FSInputFile)


def test_photo_caption_emoji_fallback():
    method = EditMessageMedia(chat_id=1, message_id=2,
        media=InputMediaPhoto(media="saved-logo", caption=ui_emoji.icon("welcome") + " Меню", parse_mode="HTML"))
    assert ui_emoji._has_ui_icons(method)
    plain = ui_emoji.ordinary_method(method)
    assert plain.media.caption == "👋 Меню" and plain.media.media == "saved-logo"
    assert method.media.caption.startswith("<tg-emoji")
