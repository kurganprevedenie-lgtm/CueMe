"""Private Telegram Mini App for saved conversations."""

import asyncio
import hashlib
import hmac
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.exceptions import TelegramAPIError

import storage


_ASSETS = Path(__file__).with_name("archive_web")
_PAGE_SIZE = 50
_FILE_LIMIT = 20 * 1024 * 1024
_CHAT_KEY = re.compile(r"c[1-9][0-9]{0,17}|b[0-9a-f]{16}")
_IMPORTED_DATE = """CASE WHEN instr(substr(date, 11), '+') = 0
                         AND instr(substr(date, 11), '-') = 0 AND substr(date, -1) <> 'Z'
                    THEN datetime(date, '-3 hours') ELSE datetime(date) END"""
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'self'; connect-src 'self'; img-src 'self' blob:; base-uri 'none'"
    ),
}


def telegram_user(init_data: str, token: str, now: float) -> str | None:
    if not init_data or len(init_data) > 8192:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            return None
        received = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", received):
            return None
        if not -30 <= now - int(fields["auth_date"]) <= 3600:
            return None
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        user_id = json.loads(fields["user"])["id"]
        if type(user_id) is int and 0 < user_id < 2**52:
            return str(user_id)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    return None


def mark_deleted(owner_id: str, connection_id: str, chat_ref: str, ids: list[int]) -> None:
    deleted_at = datetime.now(timezone.utc).isoformat()
    with storage._conn() as conn:
        conn.executemany(
            """UPDATE business_messages SET deleted_at = ?
               WHERE connection_id = ? AND chat_ref = ? AND tg_message_id = ?
                 AND owner_user_id = ? AND deleted_at IS NULL""",
            ((deleted_at, connection_id, chat_ref, message_id, owner_id) for message_id in ids),
        )


def list_chats(owner: str) -> list[dict]:
    with storage._conn() as conn:
        rows = conn.execute(f"""
            WITH live AS (
                SELECT b.chat_ref, MAX(datetime(b.date)) AS recent
                FROM business_messages b WHERE b.owner_user_id = ? GROUP BY b.chat_ref
            ), inbox AS (
                SELECT 'c' || c.id AS id, COALESCE(NULLIF(c.display_name, ''), 'Собеседник') AS name,
                       MAX(COALESCE((SELECT MAX(recent) FROM live JOIN business_chat_refs r
                                     ON r.chat_ref = live.chat_ref
                                     WHERE r.owner_user_id = ? AND r.contact_id = c.id), ''),
                           COALESCE((SELECT MAX({_IMPORTED_DATE})
                                     FROM imported_messages WHERE contact_id = c.id), '')) AS recent
                FROM contacts c WHERE c.user_telegram_id = ? AND c.archived_into_contact_id IS NULL
                UNION ALL
                SELECT 'b' || live.chat_ref, 'Собеседник', live.recent FROM live
                WHERE NOT EXISTS (SELECT 1 FROM business_chat_refs r
                                  WHERE r.owner_user_id = ? AND r.chat_ref = live.chat_ref)
            ) SELECT * FROM inbox WHERE recent <> '' ORDER BY recent DESC, id
        """, (owner, owner, owner, owner)).fetchall()
    return [{**row, "recent": row["recent"].replace(" ", "T") + "Z"} for row in rows]


_KINDS = {
    "photo": "Фото", "video": "Видео", "video_note": "Кружок", "voice": "Голосовое",
    "audio": "Аудио", "document": "Файл", "sticker": "Стикер", "animation": "GIF",
    "location": "Геопозиция", "venue": "Место", "contact": "Контакт", "poll": "Опрос",
    "dice": "Кубик", "other": "Сообщение",
}


def _attachment(row) -> tuple[str, dict, str | None]:
    try:
        media = json.loads(row["media_json"] or "{}")
    except (ValueError, TypeError):
        media = {}
    if not isinstance(media, dict):
        media = {}
    kind = row["content_type"] or "text"
    file_id = media.get("file_id")
    if not file_id:
        for legacy in ("photo", "video", "video_note", "voice"):
            if row[f"{legacy}_file_id"]:
                return legacy, media, row[f"{legacy}_file_id"]
    return kind, media, file_id if isinstance(file_id, str) else None


_MEDIA_COLUMNS = """content_type, media_json, photo_file_id, video_file_id,
                    video_note_file_id, voice_file_id"""


def chat_page(owner: str, chat: str, before: tuple, deleted: bool) -> dict:
    if chat.startswith("c"):
        scope = """chat_ref IN (SELECT chat_ref FROM business_chat_refs
                                WHERE owner_user_id = ? AND contact_id = ?)"""
        target = int(chat[1:])
        scope_args = (owner, target)
        imported = f"""
            UNION ALL SELECT 0, id, direction, text, {_IMPORTED_DATE},
                NULL, NULL, NULL, NULL, NULL, NULL, NULL
            FROM imported_messages WHERE contact_id = ?"""
        imported_args = () if deleted else (target,)
    else:
        scope = """chat_ref = ? AND NOT EXISTS (
                    SELECT 1 FROM business_chat_refs r
                    WHERE r.owner_user_id = business_messages.owner_user_id
                      AND r.chat_ref = business_messages.chat_ref)"""
        scope_args = (chat[1:],)
        imported = ""
        imported_args = ()
    with storage._conn() as conn:
        if chat.startswith("c") and not conn.execute(
            "SELECT 1 FROM contacts WHERE id = ? AND user_telegram_id = ? AND archived_into_contact_id IS NULL",
            (target, owner),
        ).fetchone():
            return {"messages": [], "next": None}
        rows = conn.execute(f"""
            WITH messages AS (
                SELECT 1 AS source, id, direction, text, datetime(date) AS stamp,
                       deleted_at, {_MEDIA_COLUMNS}
                FROM business_messages WHERE owner_user_id = ? AND {scope}
                    AND (? = 0 OR deleted_at IS NOT NULL)
                {imported if not deleted else ""}
            )
            SELECT * FROM messages
            WHERE (COALESCE(stamp, ''), source, id) < (?, ?, ?)
            ORDER BY COALESCE(stamp, '') DESC, source DESC, id DESC LIMIT ?
        """, (owner, *scope_args, deleted, *imported_args, *before, _PAGE_SIZE + 1)).fetchall()
    messages = []
    for row in rows[:_PAGE_SIZE]:
        kind, media, file_id = _attachment(row)
        details = None
        if kind in ("location", "venue") and media.get("latitude") is not None and media.get("longitude") is not None:
            details = f"{media['latitude']}, {media['longitude']}"
        elif kind == "poll":
            details = media.get("question")
        elif kind == "document":
            details = media.get("file_name")
        messages.append({
            "id": row["id"], "source": row["source"], "out": row["direction"] == "out",
            "text": row["text"], "date": row["stamp"].replace(" ", "T") + "Z" if row["stamp"] else None,
            "deleted": bool(row["deleted_at"]), "kind": _KINDS.get(kind), "details": details,
            "attachment": bool(file_id),
        })
    next_page = None
    if len(rows) > _PAGE_SIZE:
        last = rows[_PAGE_SIZE - 1]
        next_page = [last["stamp"] or "", last["source"], last["id"]]
    return {"messages": messages, "next": next_page}


def _file_id(owner: str, row_id: int) -> str | None:
    with storage._conn() as conn:
        row = conn.execute(
            f"SELECT {_MEDIA_COLUMNS} FROM business_messages WHERE id = ? AND owner_user_id = ?",
            (row_id, owner),
        ).fetchone()
    return _attachment(row)[2] if row else None


def register_archive(app: web.Application, bot, is_premium) -> None:
    database_slots = asyncio.Semaphore(3)
    download_slots = asyncio.Semaphore(2)

    async def authorized(request: web.Request) -> str:
        owner = telegram_user(request.headers.get("X-Telegram-Init-Data", ""), bot.token, time.time())
        if owner is None:
            raise web.HTTPUnauthorized(headers=_HEADERS)
        if not await is_premium(bot, owner):
            raise web.HTTPForbidden(headers=_HEADERS)
        return owner

    async def asset(request: web.Request) -> web.FileResponse:
        name = request.match_info.get("name", "index.html")
        return web.FileResponse(_ASSETS / name, headers=_HEADERS)

    async def chats(request: web.Request) -> web.Response:
        owner = await authorized(request)
        async with database_slots:
            data = await asyncio.to_thread(list_chats, owner)
        return web.json_response({"chats": data}, headers=_HEADERS)

    async def messages(request: web.Request) -> web.Response:
        owner = await authorized(request)
        try:
            before = json.loads(request.query.get("before", '["~",1,1]'))
            if (not isinstance(before, list) or len(before) != 3
                    or not isinstance(before[0], str) or len(before[0]) > 19
                    or type(before[1]) is not int or before[1] not in (0, 1)
                    or type(before[2]) is not int or not 0 < before[2] < 2**63):
                raise ValueError
        except (ValueError, RecursionError):
            raise web.HTTPBadRequest(headers=_HEADERS) from None
        async with database_slots:
            data = await asyncio.to_thread(
                chat_page, owner, request.match_info["chat"], tuple(before), request.query.get("deleted") == "1",
            )
        return web.json_response(data, headers=_HEADERS)

    async def download(request: web.Request) -> web.Response:
        owner = await authorized(request)
        async with database_slots:
            file_id = await asyncio.to_thread(_file_id, owner, int(request.match_info["id"]))
        if not file_id:
            raise web.HTTPNotFound(headers=_HEADERS)
        async with download_slots:
            try:
                async with asyncio.timeout(30):
                    file = await bot.get_file(file_id)
                    if not file.file_path or file.file_size is None or file.file_size > _FILE_LIMIT:
                        raise web.HTTPRequestEntityTooLarge(max_size=_FILE_LIMIT, actual_size=file.file_size or 0,
                                                            headers=_HEADERS)
                    data = io.BytesIO()
                    await bot.download_file(file.file_path, destination=data, timeout=25)
            except (TelegramAPIError, TimeoutError):
                raise web.HTTPBadGateway(headers=_HEADERS) from None
        extension = Path(file.file_path).suffix
        extension = extension if re.fullmatch(r"\.[a-zA-Z0-9]{1,8}", extension) else ""
        return web.Response(
            body=data.getvalue(), content_type="application/octet-stream",
            headers={**_HEADERS, "Content-Disposition": f'attachment; filename="cueme-media{extension}"'},
        )

    app.router.add_get("/archive/", asset)
    app.router.add_get(r"/archive/{name:app\.css|app\.js}", asset)
    app.router.add_get("/archive/api/chats", chats, allow_head=False)
    app.router.add_get(f"/archive/api/chats/{{chat:{_CHAT_KEY.pattern}}}", messages, allow_head=False)
    app.router.add_get(r"/archive/api/files/{id:[1-9][0-9]{0,17}}", download, allow_head=False)
