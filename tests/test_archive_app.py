import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import archive_app
import storage


TOKEN = "test-token"
CHAT_REF = "1234567890abcdef"


def signed_data(owner, timestamp):
    fields = {"auth_date": str(timestamp), "user": json.dumps({"id": owner}), "signature": "example"}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), hashlib.sha256)
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


@pytest.fixture
def archive_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "archive.db")
    storage.init_db()
    with storage._conn() as conn:
        conn.executemany(
            """INSERT INTO contacts(id, user_telegram_id, contact_alias, original_from_id, display_name)
               VALUES (?, ?, 'alias', 'user100', ?)""",
            [(1, "10", "Собеседник"), (2, "20", "Чужой чат")],
        )
        conn.executemany(
            "INSERT INTO business_chat_refs VALUES (?, ?, ?)",
            [("10", CHAT_REF, 1), ("20", CHAT_REF, 2)],
        )
        conn.execute("INSERT INTO imported_messages VALUES (1, 1, 'out', 'Импорт', '2026-11-20T12:00:00')")
        conn.executemany(
            """INSERT INTO business_messages
               (id, connection_id, owner_user_id, chat_ref, direction, text, date, tg_message_id, photo_file_id)
               VALUES (?, ?, ?, ?, 'in', ?, '2026-11-20T10:00:00+00:00', 1, ?)""",
            [(1, "own", "10", CHAT_REF, "Сохранено", "file"), (2, "other", "20", CHAT_REF, "Чужое", "private"),
             (3, "own", "10", "fedcba0987654321", "Без контакта", None)],
        )


def test_telegram_signature_and_age():
    data = signed_data(10, 1000)
    assert archive_app.telegram_user(data, TOKEN, 1010) == "10"
    assert archive_app.telegram_user(data.replace("10%7D", "20%7D"), TOKEN, 1010) is None
    assert archive_app.telegram_user(data, TOKEN, 4601) is None
    assert archive_app.telegram_user(data + "&auth_date=1000", TOKEN, 1010) is None


def test_page_merges_sources_and_only_marks_owned_rows(archive_db, monkeypatch):
    monkeypatch.setattr(archive_app, "_PAGE_SIZE", 1)
    archive_app.mark_deleted("10", "own", CHAT_REF, [1])
    first = archive_app.chat_page("10", "c1", ("~", 1, 1), False)
    assert first["messages"][0]["text"] == "Сохранено"
    assert first["messages"][0]["deleted"] is True
    assert first["messages"][0]["attachment"] is True
    assert first["messages"][0]["preview"] == "image"
    second = archive_app.chat_page("10", "c1", tuple(first["next"]), False)
    assert second["messages"][0]["text"] == "Импорт"
    assert second["messages"][0]["date"] == "2026-11-20T09:00:00Z"
    assert second["messages"][0]["preview"] is None
    assert second["next"] is None
    assert archive_app.chat_page("20", "c2", ("~", 1, 1), True)["messages"] == []
    chats = archive_app.list_chats("10")
    assert [chat["id"] for chat in chats] == ["bfedcba0987654321", "c1"]
    assert [chat["preview"] for chat in chats] == ["Без контакта", "Сохранено"]


def test_archive_endpoints_enforce_access(archive_db):
    async def run():
        async def is_premium(bot, owner):
            return owner == "10"

        async def get_file(file_id):
            assert file_id == "file"
            return SimpleNamespace(file_path="photos/test.jpg", file_size=4)

        async def download_file(path, destination, timeout):
            destination.write(b"demo")

        app = web.Application()
        bot = SimpleNamespace(token=TOKEN, get_file=get_file, download_file=download_file)
        archive_app.register_archive(app, bot, is_premium)
        headers = {"X-Telegram-Init-Data": signed_data(10, int(time.time()))}
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/archive/")
            assert response.status == 200
            html = await response.text()
            assert 'src="data:image/webp;base64,' in html
            assert 'rel="preload" href="wallpaper-v3.webp"' in html
            assert (await client.get("/archive/app.js")).status == 200
            wallpaper = await client.get("/archive/wallpaper-v3.webp")
            assert wallpaper.content_type == "image/webp"
            assert "immutable" in wallpaper.headers["Cache-Control"]
            assert (await client.get("/archive/api/chats")).status == 401
            blocked = {"X-Telegram-Init-Data": signed_data(20, int(time.time()))}
            assert (await client.get("/archive/api/chats", headers=blocked)).status == 403
            response = await client.get("/archive/api/chats/c2", headers=headers)
            assert (await response.json())["messages"] == []
            assert (await client.get("/archive/api/files/2", headers=headers)).status == 404
            response = await client.get("/archive/api/files/1", headers=headers)
            assert await response.read() == b"demo"
            assert response.content_type == "image/jpeg"
            assert response.headers["Content-Disposition"].startswith("attachment;")
            assert "media-src blob:" in response.headers["Content-Security-Policy"]
            assert (await client.get("/archive/api/chats/c1?before=1", headers=headers)).status == 400

    asyncio.run(run())
