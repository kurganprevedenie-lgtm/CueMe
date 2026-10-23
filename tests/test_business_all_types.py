"""Business API: все типы сообщений сохраняются и показываются владельцу при
удалении собеседником (main._extract_business_content → business_messages
→ main._send_deleted_content / _send_deleted_batch). Bot замокан — запросов
в Telegram нет, проверяем, каким методом и с какими аргументами бот бы
отправил."""
import asyncio
from datetime import datetime, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import main
import storage

OWNER = "100"
CONN = "conn1"
CHAT_ID = 555
CHAT_REF = main._chat_ref(CHAT_ID)
NAME = "Лиза"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    with storage._conn() as c:
        c.execute(
            "INSERT INTO business_connections (connection_id, owner_user_id, can_reply, is_enabled, created_at) "
            "VALUES (?, ?, 1, 1, '2026-10-01T00:00:00+00:00')", (CONN, OWNER),
        )
    yield


def _f(fid: str) -> dict:
    return {"file_id": fid, "file_unique_id": "u_" + fid}


_POLL = {
    "id": "p1", "question": "Куда идём?", "total_voter_count": 0, "is_closed": False,
    "is_anonymous": True, "type": "regular", "allows_multiple_answers": False,
    "allows_revoting": False, "members_only": False,
    "options": [{"text": "Кино", "voter_count": 0, "persistent_id": "o1"},
                {"text": "Бар", "voter_count": 0, "persistent_id": "o2"}],
}

# тип → (поля сообщения, метод отправки при удалении, аргумент с file_id, что должно быть в подписи)
CASES = {
    "photo": ({"photo": [{**_f("PH_S"), "width": 90, "height": 90}, {**_f("PH_L"), "width": 1280, "height": 960}]},
              "send_photo", "photo", "удалил(а) фото"),
    "video": ({"video": {**_f("VID"), "width": 1, "height": 1, "duration": 5}}, "send_video", "video", "удалил(а) видео"),
    "animation": ({"animation": {**_f("GIF"), "width": 1, "height": 1, "duration": 3, "file_name": "cat.mp4"},
                   "document": {**_f("GIF"), "file_name": "cat.mp4"}}, "send_animation", "animation", "удалил(а) гифку"),
    "document": ({"document": {**_f("DOC"), "file_name": "договор.pdf", "mime_type": "application/pdf"}},
                 "send_document", "document", "удалил(а) файл «договор.pdf»"),
    "audio": ({"audio": {**_f("AU"), "duration": 200, "performer": "Земфира", "title": "Хочешь"}},
              "send_audio", "audio", "удалил(а) аудио «Земфира — Хочешь»"),
    "voice": ({"voice": {**_f("VO"), "duration": 7}}, "send_voice", "voice", "удалил(а) голосовое"),
    "video_note": ({"video_note": {**_f("VN"), "length": 240, "duration": 4}}, "send_video_note", None,
                   "удалил(а) видеосообщение (кружок)"),
    "sticker": ({"sticker": {**_f("STK"), "type": "regular", "width": 512, "height": 512,
                             "is_animated": False, "is_video": False, "emoji": "😂", "set_name": "Cats"}},
                "send_sticker", None, "удалил(а) стикер 😂"),
    "location": ({"location": {"latitude": 55.75, "longitude": 37.61}}, "send_location", None, "удалил(а) геопозицию"),
    "venue": ({"venue": {"location": {"latitude": 55.75, "longitude": 37.61}, "title": "Кофейня", "address": "Тверская, 1"},
               "location": {"latitude": 55.75, "longitude": 37.61}}, "send_venue", None, "удалил(а) место «Кофейня»"),
    "contact": ({"contact": {"phone_number": "+79990000000", "first_name": "Оля"}}, "send_contact", None,
                "удалил(а) контакт"),
    "poll": ({"poll": _POLL}, "send_message", None, "удалил(а) опрос: «Куда идём?»\n• Кино\n• Бар"),
    "dice": ({"dice": {"emoji": "🎲", "value": 4}}, "send_message", None, "удалил(а) кубик 🎲: выпало 4"),
    "text": ({"text": "привет"}, "send_message", None, "удалил(а) сообщение:\n«привет»"),
}

_CHECKLIST = {"title": "Купить", "tasks": [
    {"id": 1, "text": "вино"},
    {"id": 2, "text": "сыр", "completion_date": 1761000000},
]}


def test_checklist_saved_and_shown_as_text(db):
    row = _save(_msg(60, checklist=_CHECKLIST))
    assert row["content_type"] == "other"
    assert main._has_deleted_content(row)
    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.calls[0][2]["text"] == f"🗑 {NAME} удалил(а) список задач: «Купить»\n⬜ вино\n✅ сыр"


def test_unsupported_kind_says_so(db):
    row = _save(_msg(61, story={"chat": {"id": 1, "type": "private"}, "id": 7}))
    assert row["content_type"] == "other"
    assert not main._has_deleted_content(row)  # в тизер не берём — показать нечего
    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.calls[0][2]["text"] == f"🗑 {NAME} удалил(а) историю — показать его содержимое бот не может"


def _msg(message_id: int = 10, **extra) -> Message:
    return Message.model_validate({
        "message_id": message_id, "date": datetime(2026, 10, 23, tzinfo=timezone.utc),
        "chat": {"id": CHAT_ID, "type": "private", "username": "lisa_k", "first_name": "Лиза"},
        "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Лиза"},
        **extra,
    })


def _save(msg: Message, text: str | None = None, **legacy) -> object:
    """Как handle_business_message: текст/подпись в text, тип — через _extract_business_content."""
    storage.save_business_message(
        connection_id=CONN, owner_user_id=OWNER, chat_ref=CHAT_REF, direction="in",
        text=text if text is not None else (msg.text or msg.caption),
        date=msg.date.isoformat(), tg_message_id=msg.message_id, raw_meta={},
        **legacy, **main._extract_business_content(msg),
    )
    return storage.get_business_message_by_tg_id(CONN, CHAT_REF, msg.message_id)


class FakeBot:
    """Пишет вызовы. fail — методы, которые падают «протухшим file_id»;
    reject_custom_emoji — Telegram отклоняет custom_emoji в форматировании."""

    def __init__(self, fail=(), reject_custom_emoji=False):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = set(fail)
        self.reject_custom_emoji = reject_custom_emoji

    def __getattr__(self, method):
        if not method.startswith("send_"):
            raise AttributeError(method)

        async def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if method in self.fail:
                raise TelegramBadRequest(method=None, message="Bad Request: wrong file identifier")
            ents = kwargs.get("entities") or kwargs.get("caption_entities") or []
            if self.reject_custom_emoji and any(e.type == "custom_emoji" for e in ents):
                raise TelegramBadRequest(method=None, message="Bad Request: can't use custom emoji")
            return True
        return call

    def methods(self):
        return [c[0] for c in self.calls]

    def texts(self):
        return [c[2].get("text") or c[2].get("caption") or (c[1][1] if len(c[1]) > 1 else None) for c in self.calls]


def _run(coro):
    return asyncio.run(coro)


# ── сохранение и показ каждого типа ───────────────────────────────────────────

@pytest.mark.parametrize("ctype", list(CASES))
def test_each_type_saved_and_shown_on_delete(db, ctype):
    fields, method, file_arg, expected = CASES[ctype]
    row = _save(_msg(**fields))
    assert row["content_type"] == ctype

    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.methods()[0] == method
    if file_arg:
        assert bot.calls[0][2][file_arg] == storage.json.loads(row["media_json"])["file_id"]
    assert any(t and f"🗑 {NAME} {expected}" in t for t in bot.texts()), bot.calls


def test_persist_business_message_passes_content(db):
    """Путь handle_business_message: _persist_business_message(content=...)."""
    msg = _msg(50, **CASES["sticker"][0], forward_origin={
        "type": "hidden_user", "date": datetime(2026, 10, 1, tzinfo=timezone.utc), "sender_user_name": "Аноним"})
    main._persist_business_message(
        conn_id=CONN, owner_id=OWNER, chat_ref=CHAT_REF, direction="in", text=None, is_voice=False,
        date=msg.date.isoformat(), tg_message_id=50, contact_tg_id=str(CHAT_ID),
        chat_first_name="Лиза", chat_last_name=None, chat_username="lisa_k", sender_username="lisa_k",
        content=main._extract_business_content(msg),
    )
    row = storage.get_business_message_by_tg_id(CONN, CHAT_REF, 50)
    assert row["content_type"] == "sticker"
    assert storage.json.loads(row["forward_info"])["sender_user_name"] == "Аноним"


def test_media_metadata_saved_without_text(db):
    row = _save(_msg(**CASES["document"][0]))
    media = storage.json.loads(row["media_json"])
    assert media["file_name"] == "договор.pdf" and media["mime_type"] == "application/pdf"
    # стикеры/файлы в анализ не подмешиваются: текста у такой строки нет
    assert row["text"] is None
    sticker = _save(_msg(11, **CASES["sticker"][0]))
    assert sticker["text"] is None
    assert storage.json.loads(sticker["media_json"])["set_name"] == "Cats"


def test_photo_caption_entities_spoiler_album_and_reply_saved(db):
    msg = _msg(
        photo=[{**_f("PH"), "width": 1, "height": 1}], caption="смотри",
        caption_entities=[{"type": "bold", "offset": 0, "length": 6}],
        has_media_spoiler=True, media_group_id="ALB",
        reply_to_message={"message_id": 3, "date": datetime(2026, 10, 22, tzinfo=timezone.utc),
                          "chat": {"id": CHAT_ID, "type": "private"}, "text": "а"},
    )
    row = _save(msg)
    assert storage.json.loads(row["entities_json"]) == [{"type": "bold", "offset": 0, "length": 6}]
    assert storage.json.loads(row["media_json"])["has_spoiler"] is True
    assert row["media_group_id"] == "ALB" and row["reply_to_tg_message_id"] == 3

    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    kw = bot.calls[0][2]
    assert kw["has_spoiler"] is True
    # жирный сдвинут на длину префикса «🗑 Лиза удалил(а) фото:\n«»
    prefix = f"🗑 {NAME} удалил(а) фото:\n«"
    assert kw["caption"] == prefix + "смотри»"
    assert kw["caption_entities"][0].offset == main._utf16_len(prefix)


# ── откаты ────────────────────────────────────────────────────────────────────

def test_dead_file_id_falls_back_to_text_with_type(db):
    row = _save(_msg(**CASES["document"][0]))
    bot = FakeBot(fail={"send_document"})
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.methods() == ["send_document", "send_message"]
    assert bot.calls[-1][2]["text"] == f"🗑 {NAME} удалил(а) файл «договор.pdf»\n(сам файл уже недоступен)"


def test_dead_sticker_falls_back_to_text(db):
    row = _save(_msg(**CASES["sticker"][0]))
    bot = FakeBot(fail={"send_sticker"})
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.calls[-1][0] == "send_message"
    assert "удалил(а) стикер 😂" in bot.calls[-1][2]["text"]


def test_custom_emoji_sent_then_retried_without_entities(db):
    text = "привет 😎 жирный"
    msg = _msg(text=text, entities=[
        {"type": "custom_emoji", "offset": 7, "length": 2, "custom_emoji_id": "5368"},
        {"type": "bold", "offset": 10, "length": 6},
    ])
    row = _save(msg)

    ok_bot = FakeBot()
    _run(main._send_deleted_content(ok_bot, OWNER, NAME, row))
    assert len(ok_bot.calls) == 1
    ents = ok_bot.calls[0][2]["entities"]
    shift = main._utf16_len(f"🗑 {NAME} удалил(а) сообщение:\n«")
    assert [(e.type, e.offset) for e in ents] == [("custom_emoji", 7 + shift), ("bold", 10 + shift)]
    assert ents[0].custom_emoji_id == "5368"

    picky_bot = FakeBot(reject_custom_emoji=True)
    _run(main._send_deleted_content(picky_bot, OWNER, NAME, row))
    assert len(picky_bot.calls) == 2
    assert "entities" in picky_bot.calls[0][2] and "entities" not in picky_bot.calls[1][2]
    assert picky_bot.calls[1][2]["text"] == f"🗑 {NAME} удалил(а) сообщение:\n«{text}»"


# ── старые строки (до content_type) ───────────────────────────────────────────

def _save_legacy(tg_id: int, text: str | None, **file_ids):
    storage.save_business_message(
        connection_id=CONN, owner_user_id=OWNER, chat_ref=CHAT_REF, direction="in",
        text=text, date="2026-09-01T00:00:00+00:00", tg_message_id=tg_id, raw_meta={}, **file_ids,
    )
    return storage.get_business_message_by_tg_id(CONN, CHAT_REF, tg_id)


def test_legacy_photo_row_shown_as_before(db):
    row = _save_legacy(1, "старая подпись", photo_file_id="OLD_PH")
    assert row["content_type"] is None
    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, row))
    assert bot.methods() == ["send_photo"]
    assert bot.calls[0][2]["photo"] == "OLD_PH"
    assert bot.calls[0][2]["caption"] == f"🗑 {NAME} удалил(а) фото:\n«старая подпись»"


def test_legacy_rows_other_types_and_empty(db):
    for tg_id, col, method in ((2, "video_file_id", "send_video"), (3, "voice_file_id", "send_voice"),
                               (4, "video_note_file_id", "send_video_note")):
        row = _save_legacy(tg_id, None, **{col: "OLD"})
        bot = FakeBot()
        _run(main._send_deleted_content(bot, OWNER, NAME, row))
        assert bot.methods()[0] == method

    text_row = _save_legacy(5, "просто текст")
    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, text_row))
    assert bot.calls[0][2]["text"] == f"🗑 {NAME} удалил(а) сообщение:\n«просто текст»"

    empty = _save_legacy(6, None)
    bot = FakeBot()
    _run(main._send_deleted_content(bot, OWNER, NAME, empty))
    assert bot.calls[0][2]["text"] == f"🗑 {NAME} удалил(а) сообщение (текст недоступен)"
    assert not main._has_deleted_content(empty)
    assert main._has_deleted_content(_save_legacy(7, None, photo_file_id="P"))


# ── альбомы ───────────────────────────────────────────────────────────────────

def _album(n=3, group="ALB"):
    rows = []
    for i in range(n):
        fields = {"photo": [{**_f(f"P{i}"), "width": 1, "height": 1}], "media_group_id": group}
        if i == 0:
            fields["caption"] = "наш вечер"
        rows.append(_save(_msg(20 + i, **fields)))
    return rows


def test_fully_deleted_album_sent_as_media_group(db):
    rows = _album()
    bot = FakeBot()
    _run(main._send_deleted_batch(bot, OWNER, NAME, rows))
    assert bot.methods() == ["send_media_group"]
    media = bot.calls[0][2]["media"]
    assert [m.media for m in media] == ["P0", "P1", "P2"]
    assert media[0].caption == f"🗑 {NAME} удалил(а) альбом (3):\n«наш вечер»"


def test_partly_deleted_album_sent_one_by_one(db):
    rows = _album()
    bot = FakeBot()
    _run(main._send_deleted_batch(bot, OWNER, NAME, rows[:2]))  # третье фото не удалено
    assert bot.methods() == ["send_photo", "send_photo"]


def test_album_failure_falls_back_to_single_messages(db):
    rows = _album(2)
    bot = FakeBot(fail={"send_media_group"})
    _run(main._send_deleted_batch(bot, OWNER, NAME, rows))
    assert bot.methods().count("send_media_group") == 2  # с форматированием и без
    assert bot.methods()[-2:] == ["send_photo", "send_photo"]


# ── тизер без Premium ─────────────────────────────────────────────────────────

class _Chat:
    id = CHAT_ID
    username = "lisa_k"


class _Deleted:
    business_connection_id = CONN
    chat = _Chat()

    def __init__(self, ids):
        self.message_ids = ids


def _teaser(monkeypatch, ids):
    async def not_premium(bot, tid):
        return False

    async def not_excluded(*a):
        return False
    monkeypatch.setattr(main, "_is_premium", not_premium)
    monkeypatch.setattr(main, "_deleted_messages_excluded", not_excluded)
    bot = FakeBot()
    _run(main.handle_deleted_business_messages(_Deleted(ids), bot))
    return bot


def test_teaser_names_type_without_details(db, monkeypatch):
    _save(_msg(30, **CASES["sticker"][0]))
    bot = _teaser(monkeypatch, [30])
    text = bot.calls[0][2].get("text") or bot.calls[0][1][1]
    assert text.startswith("🗑 @lisa_k удалил(а) стикер\n")
    assert "😂" not in text  # эмодзи — уже содержимое, без Premium не показываем
    assert bot.calls[0][2]["reply_markup"].inline_keyboard[0][0].text == "👀 Показать"


def test_teaser_lists_types_for_several(db, monkeypatch):
    _save(_msg(31, **CASES["sticker"][0]))
    _save(_msg(32, **CASES["document"][0]))
    _save(_msg(33, text="привет"))
    bot = _teaser(monkeypatch, [31, 32, 33])
    text = bot.calls[0][2].get("text") or bot.calls[0][1][1]
    assert text.startswith("🗑 @lisa_k удалил(а) сообщения (3): стикер, файл, текст\n")
    assert "договор" not in text


def test_teaser_reveal_after_premium_shows_album_and_types(db, monkeypatch):
    _album(2)
    bot = _teaser(monkeypatch, [20, 21])
    assert "сообщения (2): фото" in (bot.calls[0][2].get("text") or bot.calls[0][1][1])
    reveal = storage.get_unrevealed_deleted_reveals(OWNER)[0]
    reveal_bot = FakeBot()
    assert _run(main._reveal_deleted(reveal_bot, OWNER, reveal)) == 2
    assert reveal_bot.methods() == ["send_media_group"]


# ── правка: новое медиа/подпись обновляет content ─────────────────────────────

def test_edit_updates_media_and_entities(db):
    _save(_msg(40, photo=[{**_f("OLD"), "width": 1, "height": 1}], caption="было"))
    edited = _msg(
        40, photo=[{**_f("NEW"), "width": 1, "height": 1}], caption="стало",
        caption_entities=[{"type": "italic", "offset": 0, "length": 5}],
    )
    edited = edited.model_copy(update={
        "business_connection_id": CONN,
        "from_user": edited.from_user.model_copy(update={"id": int(OWNER)}),  # правка владельца — без уведомления
    })
    _run(main.handle_edited_business_message(edited, FakeBot()))
    row = storage.get_business_message_by_tg_id(CONN, CHAT_REF, 40)
    assert storage.json.loads(row["media_json"])["file_id"] == "NEW"
    assert row["photo_file_id"] == "NEW"  # старая колонка тоже — её читает /export
    assert storage.json.loads(row["entities_json"])[0]["type"] == "italic"
    assert row["text"] == "стало"
