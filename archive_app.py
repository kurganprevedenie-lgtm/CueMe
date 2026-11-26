"""Private Telegram Mini App for saved conversations."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import ClientError, web
from aiogram.exceptions import TelegramAPIError

import storage


_ASSETS = Path(__file__).with_name("archive_web")
_WALLPAPER = "wallpaper-v3.webp"
_PAGE_SIZE = 50
_FILE_LIMIT = 20 * 1024 * 1024
_PREVIEW_LIMIT = 384 * 1024
_PROFILE_ID = re.compile(r"user([1-9][0-9]{0,15})")
_CHAT_KEY = re.compile(r"c[1-9][0-9]{0,17}|b[0-9a-f]{16}")
_IMPORTED_DATE = """CASE WHEN instr(substr(date, 11), '+') = 0
                         AND instr(substr(date, 11), '-') = 0 AND substr(date, -1) <> 'Z'
                    THEN datetime(date, '-3 hours') ELSE datetime(date) END"""

# В старых версиях удаления без Premium фиксировались в тизерах. Эти строки
# тоже считаются удалёнными, даже если самой метки в business_messages ещё нет.
_NOTICE_DELETED = """notice_deleted AS (
    SELECT j.value AS id, MAX(n.created_at) AS deleted_at
    FROM deleted_reveals n
    JOIN json_each(CASE WHEN json_valid(n.message_row_ids) THEN n.message_row_ids ELSE '[]' END) j
    WHERE n.owner_user_id = :owner AND j.type = 'integer'
    GROUP BY j.value
)"""

# В импорте нет Telegram ID сообщения. Скрываем совпадения в том же чате
# по направлению, тексту и секунде, учитывая возможный часовой пояс экспорта.
_DELETED_COPIES = """deleted_copies AS MATERIALIZED (
    SELECT r.contact_id, b.direction, trim(b.text) AS body, unixepoch(b.date) AS stamp
    FROM business_messages b
    JOIN business_chat_refs r ON r.owner_user_id = b.owner_user_id AND r.chat_ref = b.chat_ref
    LEFT JOIN notice_deleted n ON n.id = b.id
    WHERE NOT :allow_deleted AND b.owner_user_id = :owner
      AND (b.deleted_at IS NOT NULL OR n.id IS NOT NULL) AND trim(b.text) <> ''
)"""
_VISIBLE_IMPORT = """(:allow_deleted OR NOT EXISTS (
    SELECT 1 FROM deleted_copies d
    WHERE d.contact_id = i.contact_id AND d.direction = i.direction AND d.body = trim(i.text)
      AND (d.stamp = unixepoch(i.date) OR (
          instr(substr(i.date, 11), '+') = 0 AND instr(substr(i.date, 11), '-') = 0
          AND substr(i.date, -1) <> 'Z'
          AND unixepoch(i.date) - d.stamp BETWEEN -43200 AND 50400
          AND (unixepoch(i.date) - d.stamp) % 900 = 0
      ))
))"""
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'self'; connect-src 'self'; img-src 'self' blob: data:; media-src blob:; base-uri 'none'"
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


def list_chats(owner: str, *, allow_deleted: bool = True) -> list[dict]:
    with storage._conn() as conn:
        rows = conn.execute(f"""
            WITH {_NOTICE_DELETED}, {_DELETED_COPIES}, live AS (
                SELECT b.chat_ref, b.id, MAX(datetime(b.date)) AS recent
                FROM business_messages b LEFT JOIN notice_deleted n ON n.id = b.id
                WHERE b.owner_user_id = :owner
                  AND (:allow_deleted OR (b.deleted_at IS NULL AND n.id IS NULL))
                GROUP BY b.chat_ref
            ), mapped AS (
                SELECT r.contact_id, live.id, live.recent,
                       ROW_NUMBER() OVER (PARTITION BY r.contact_id ORDER BY live.recent DESC, live.id DESC) AS rank
                FROM live JOIN business_chat_refs r ON r.chat_ref = live.chat_ref
                WHERE r.owner_user_id = :owner
            ), imported AS (
                SELECT i.contact_id, i.id, MAX({_IMPORTED_DATE}) AS recent
                FROM contacts c JOIN imported_messages i ON i.contact_id = c.id
                WHERE c.user_telegram_id = :owner AND c.archived_into_contact_id IS NULL
                  AND {_VISIBLE_IMPORT} GROUP BY i.contact_id
            ), inbox AS (
                SELECT 'c' || c.id AS id, COALESCE(NULLIF(c.display_name, ''), 'Собеседник') AS name,
                       MAX(COALESCE(m.recent, ''), COALESCE(i.recent, '')) AS recent,
                       CASE WHEN COALESCE(m.recent, '') >= COALESCE(i.recent, '') THEN m.id END AS live_id,
                       CASE WHEN COALESCE(m.recent, '') < COALESCE(i.recent, '') THEN i.id END AS import_id,
                       CASE WHEN m.id IS NOT NULL THEN c.original_from_id END AS profile
                FROM contacts c LEFT JOIN mapped m ON m.contact_id = c.id AND m.rank = 1
                LEFT JOIN imported i ON i.contact_id = c.id
                WHERE c.user_telegram_id = :owner AND c.archived_into_contact_id IS NULL
                UNION ALL
                SELECT 'b' || live.chat_ref, 'Собеседник', live.recent, live.id, NULL, NULL FROM live
                WHERE NOT EXISTS (SELECT 1 FROM business_chat_refs r
                                  WHERE r.owner_user_id = :owner AND r.chat_ref = live.chat_ref)
            )
            SELECT inbox.id, inbox.name, inbox.recent, inbox.profile, b.id AS message_id,
                   COALESCE(substr(b.text, 1, 180), substr(i.text, 1, 180)) AS preview,
                   COALESCE(b.direction, i.direction) AS direction,
                   b.content_type, b.media_json, b.photo_file_id, b.video_file_id,
                   COALESCE(b.deleted_at, n.deleted_at) AS deleted_at,
                   b.video_note_file_id, b.voice_file_id
            FROM inbox LEFT JOIN business_messages b ON b.id = inbox.live_id AND b.owner_user_id = :owner
            LEFT JOIN notice_deleted n ON n.id = b.id
            LEFT JOIN imported_messages i ON i.id = inbox.import_id
            WHERE recent <> '' ORDER BY recent DESC, inbox.id
        """, {"owner": owner, "allow_deleted": allow_deleted}).fetchall()
    chats = []
    for row in rows:
        kind, media, file_id = _attachment(row)
        chats.append({
            "id": row["id"], "name": row["name"], "recent": row["recent"].replace(" ", "T") + "Z",
            "preview": (row["preview"] or "").strip() or _KINDS.get(kind, ""),
            "kind": _KINDS.get(kind),
            "out": row["direction"] == "out",
            "deleted": bool(row["deleted_at"]),
            "avatar": bool(_PROFILE_ID.fullmatch(row["profile"] or "")),
            "thumbnail": str(row["message_id"]) if _thumbnail_id(kind, media, file_id) else None,
        })
    return chats


_KINDS = {
    "photo": "Фото", "video": "Видео", "video_note": "Кружок", "voice": "Голосовое",
    "audio": "Аудио", "document": "Файл", "sticker": "Стикер", "animation": "GIF",
    "location": "Геопозиция", "venue": "Место", "contact": "Контакт", "poll": "Опрос",
    "dice": "Кубик", "other": "Сообщение",
}
_PREVIEWS = {
    "photo": "image", "sticker": "image", "animation": "video",
    "video": "video", "video_note": "video", "voice": "audio", "audio": "audio",
}
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm",
    ".oga": "audio/ogg", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
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
    if not isinstance(file_id, str) or not file_id:
        for legacy in ("photo", "video", "video_note", "voice"):
            if row[f"{legacy}_file_id"]:
                return legacy, media, row[f"{legacy}_file_id"]
    return kind, media, file_id if isinstance(file_id, str) else None


def _thumbnail_id(kind: str, media: dict, file_id: str | None) -> str | None:
    if media.get("has_spoiler") or kind not in ("photo", "video", "video_note", "animation"):
        return None
    thumbnail = media.get("thumbnail_file_id")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    return file_id if kind == "photo" else None


def _avatar_user(owner: str, contact: int) -> int | None:
    with storage._conn() as conn:
        rows = conn.execute("""
            SELECT c.original_from_id, r.chat_ref FROM contacts c
            JOIN business_chat_refs r ON r.contact_id = c.id AND r.owner_user_id = c.user_telegram_id
            WHERE c.id = ? AND c.user_telegram_id = ? AND c.archived_into_contact_id IS NULL
        """, (contact, owner)).fetchall()
    for row in rows:
        match = _PROFILE_ID.fullmatch(row["original_from_id"])
        if match and hashlib.sha256(match[1].encode()).hexdigest()[:16] == row["chat_ref"]:
            return int(match[1])
    return None


_MEDIA_COLUMNS = """content_type, media_json, photo_file_id, video_file_id,
                    video_note_file_id, voice_file_id"""


def chat_page(owner: str, chat: str, before: tuple, deleted: bool, *, allow_deleted: bool = True) -> dict:
    if deleted and not allow_deleted:
        return {"messages": [], "next": None}
    params = {
        "owner": owner, "target": chat[1:], "deleted": deleted, "allow_deleted": allow_deleted,
        "stamp": before[0], "source": before[1], "id": before[2], "limit": _PAGE_SIZE + 1,
    }
    if chat.startswith("c"):
        scope = """chat_ref IN (SELECT chat_ref FROM business_chat_refs
                                WHERE owner_user_id = :owner AND contact_id = :target)"""
        target = int(chat[1:])
        imported = f"""
            UNION ALL SELECT 0, id, direction, text, {_IMPORTED_DATE},
                NULL, NULL, NULL, NULL, NULL, NULL, NULL
            FROM imported_messages i WHERE contact_id = :target AND {_VISIBLE_IMPORT}"""
    else:
        scope = """chat_ref = :target AND NOT EXISTS (
                    SELECT 1 FROM business_chat_refs r
                    WHERE r.owner_user_id = business_messages.owner_user_id
                      AND r.chat_ref = business_messages.chat_ref)"""
        imported = ""
    with storage._conn() as conn:
        if chat.startswith("c") and not conn.execute(
            "SELECT 1 FROM contacts WHERE id = ? AND user_telegram_id = ? AND archived_into_contact_id IS NULL",
            (target, owner),
        ).fetchone():
            return {"messages": [], "next": None}
        rows = conn.execute(f"""
            WITH {_NOTICE_DELETED}, {_DELETED_COPIES}, messages AS (
                SELECT 1 AS source, business_messages.id, direction, text, datetime(date) AS stamp,
                       COALESCE(business_messages.deleted_at, n.deleted_at) AS deleted_at, {_MEDIA_COLUMNS}
                FROM business_messages LEFT JOIN notice_deleted n ON n.id = business_messages.id
                WHERE owner_user_id = :owner AND {scope}
                  AND (:allow_deleted OR (business_messages.deleted_at IS NULL AND n.id IS NULL))
                  AND (NOT :deleted OR business_messages.deleted_at IS NOT NULL OR n.id IS NOT NULL)
                {imported if not deleted else ""}
            )
            SELECT * FROM messages
            WHERE (COALESCE(stamp, ''), source, id) < (:stamp, :source, :id)
            ORDER BY COALESCE(stamp, '') DESC, source DESC, id DESC LIMIT :limit
        """, params).fetchall()
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
        preview = _PREVIEWS.get(kind) if file_id else None
        if kind == "sticker":
            preview = "video" if media.get("is_video") else None if media.get("is_animated") else preview
        if kind == "animation" and media.get("mime_type") == "image/gif":
            preview = "image"
        messages.append({
            "id": row["id"], "source": row["source"], "out": row["direction"] == "out",
            "text": row["text"], "date": row["stamp"].replace(" ", "T") + "Z" if row["stamp"] else None,
            "deleted": bool(row["deleted_at"]), "kind": _KINDS.get(kind), "details": details,
            "attachment": bool(file_id), "preview": preview,
            "thumbnail": preview == "video" and bool(_thumbnail_id(kind, media, file_id)),
            "round": kind == "video_note",
        })
    next_page = None
    if len(rows) > _PAGE_SIZE:
        last = rows[_PAGE_SIZE - 1]
        next_page = [last["stamp"] or "", last["source"], last["id"]]
    return {"messages": messages, "next": next_page}


def _file_id(owner: str, row_id: int, *, thumbnail: bool = False, allow_deleted: bool = True) -> str | None:
    with storage._conn() as conn:
        row = conn.execute(
            f"""WITH {_NOTICE_DELETED}
            SELECT {_MEDIA_COLUMNS} FROM business_messages b
            LEFT JOIN notice_deleted n ON n.id = b.id
            WHERE b.id = :id AND b.owner_user_id = :owner
              AND (:allow_deleted OR (b.deleted_at IS NULL AND n.id IS NULL))""",
            {"id": row_id, "owner": owner, "allow_deleted": allow_deleted},
        ).fetchone()
    if not row:
        return None
    kind, media, file_id = _attachment(row)
    return _thumbnail_id(kind, media, file_id) if thumbnail else file_id


def register_archive(app: web.Application, bot, is_premium) -> None:
    database_slots = asyncio.Semaphore(3)
    download_slots = asyncio.Semaphore(2)
    preview_slots = asyncio.Semaphore(3)
    avatars: OrderedDict[tuple[str, int], tuple[float, tuple[bytes, str] | None]] = OrderedDict()
    logo = base64.b64encode((_ASSETS / "logo-v2.webp").read_bytes()).decode("ascii")
    index = (_ASSETS / "index.html").read_text(encoding="utf-8").replace(
        "{{LOGO_URL}}", f"data:image/webp;base64,{logo}",
    )

    async def authorized(request: web.Request) -> str:
        owner = telegram_user(request.headers.get("X-Telegram-Init-Data", ""), bot.token, time.time())
        if owner is None:
            raise web.HTTPUnauthorized(headers=_HEADERS)
        return owner

    async def home(request: web.Request) -> web.Response:
        return web.Response(text=index, content_type="text/html", headers=_HEADERS)

    async def asset(request: web.Request) -> web.FileResponse:
        name = request.match_info["name"]
        headers = _HEADERS
        if name == _WALLPAPER:
            headers = {
                **_HEADERS,
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Type": "image/webp",
            }
        return web.FileResponse(_ASSETS / name, headers=headers)

    async def chats(request: web.Request) -> web.Response:
        owner = await authorized(request)
        allow_deleted = await is_premium(bot, owner)
        async with database_slots:
            data = await asyncio.to_thread(list_chats, owner, allow_deleted=allow_deleted)
        return web.json_response({"chats": data, "can_view_deleted": allow_deleted}, headers=_HEADERS)

    async def messages(request: web.Request) -> web.Response:
        owner = await authorized(request)
        allow_deleted = await is_premium(bot, owner)
        deleted = request.query.get("deleted") == "1"
        if deleted and not allow_deleted:
            raise web.HTTPForbidden(headers=_HEADERS)
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
                chat_page, owner, request.match_info["chat"], tuple(before), deleted,
                allow_deleted=allow_deleted,
            )
        return web.json_response(data, headers=_HEADERS)

    async def read_file(file_id: str, limit: int, timeout: int) -> tuple[bytes, str]:
        file = await bot.get_file(file_id)
        if not file.file_path:
            raise web.HTTPNotFound(headers=_HEADERS)
        if file.file_size is None or file.file_size > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=file.file_size or 0, headers=_HEADERS)
        data = io.BytesIO()
        await bot.download_file(file.file_path, destination=data, timeout=timeout)
        extension = Path(file.file_path).suffix
        extension = extension if re.fullmatch(r"\.[a-zA-Z0-9]{1,8}", extension) else ""
        return data.getvalue(), extension

    def preview_response(saved: tuple[bytes, str] | None) -> web.Response:
        if saved is None:
            raise web.HTTPNotFound(headers=_HEADERS)
        data, extension = saved
        mime = _MEDIA_TYPES.get(extension.lower(), "")
        if not mime.startswith("image/"):
            raise web.HTTPNotFound(headers=_HEADERS)
        return web.Response(body=data, content_type=mime, headers=_HEADERS)

    async def avatar(request: web.Request) -> web.Response:
        owner = await authorized(request)
        contact = int(request.match_info["id"])
        async with database_slots:
            user_id = await asyncio.to_thread(_avatar_user, owner, contact)
        if user_id is None:
            raise web.HTTPNotFound(headers=_HEADERS)
        key = (owner, user_id)
        async with preview_slots:
            cached = avatars.get(key)
            if cached and cached[0] > time.monotonic():
                avatars.move_to_end(key)
                return preview_response(cached[1])
            saved = None
            try:
                async with asyncio.timeout(7):
                    photos = await bot.get_user_profile_photos(user_id, limit=1)
                    if photos.photos and photos.photos[0]:
                        smallest = min(photos.photos[0], key=lambda photo: photo.width * photo.height)
                        saved = await read_file(smallest.file_id, 128 * 1024, 5)
            except (TelegramAPIError, ClientError, TimeoutError, web.HTTPException):
                pass
            avatars[key] = (time.monotonic() + (600 if saved else 120), saved)
            avatars.move_to_end(key)
            if len(avatars) > 128:
                avatars.popitem(last=False)
        return preview_response(saved)

    async def thumbnail(request: web.Request) -> web.Response:
        owner = await authorized(request)
        allow_deleted = await is_premium(bot, owner)
        async with database_slots:
            file_id = await asyncio.to_thread(
                _file_id, owner, int(request.match_info["id"]), thumbnail=True, allow_deleted=allow_deleted,
            )
        if not file_id:
            raise web.HTTPNotFound(headers=_HEADERS)
        async with preview_slots:
            try:
                async with asyncio.timeout(7):
                    saved = await read_file(file_id, _PREVIEW_LIMIT, 5)
            except (TelegramAPIError, ClientError, TimeoutError, web.HTTPException):
                raise web.HTTPNotFound(headers=_HEADERS) from None
        return preview_response(saved)

    async def download(request: web.Request) -> web.Response:
        owner = await authorized(request)
        allow_deleted = await is_premium(bot, owner)
        async with database_slots:
            file_id = await asyncio.to_thread(
                _file_id, owner, int(request.match_info["id"]), allow_deleted=allow_deleted,
            )
        if not file_id:
            raise web.HTTPNotFound(headers=_HEADERS)
        async with download_slots:
            try:
                async with asyncio.timeout(30):
                    data, extension = await read_file(file_id, _FILE_LIMIT, 25)
            except TelegramAPIError as exc:
                if "file is too big" in str(exc).lower():
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=_FILE_LIMIT, actual_size=_FILE_LIMIT + 1, headers=_HEADERS,
                    ) from None
                raise web.HTTPBadGateway(headers=_HEADERS) from None
            except (ClientError, TimeoutError):
                raise web.HTTPBadGateway(headers=_HEADERS) from None
        return web.Response(
            body=data, content_type=_MEDIA_TYPES.get(extension.lower(), "application/octet-stream"),
            headers={**_HEADERS, "Content-Disposition": f'attachment; filename="cueme-media{extension}"'},
        )

    app.router.add_get("/archive/", home)
    app.router.add_get(r"/archive/{name:app\.css|app\.js|wallpaper-v3\.webp}", asset)
    app.router.add_get("/archive/api/chats", chats, allow_head=False)
    app.router.add_get(f"/archive/api/chats/{{chat:{_CHAT_KEY.pattern}}}", messages, allow_head=False)
    app.router.add_get(r"/archive/api/avatars/c{id:[1-9][0-9]{0,17}}", avatar, allow_head=False)
    app.router.add_get(r"/archive/api/thumbs/{id:[1-9][0-9]{0,17}}", thumbnail, allow_head=False)
    app.router.add_get(r"/archive/api/files/{id:[1-9][0-9]{0,17}}", download, allow_head=False)
