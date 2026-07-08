import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    from_id: str
    text: str
    date: datetime
    media_type: Optional[str]  # "photo", "sticker", "voice_message", etc.


@dataclass
class ChatMeta:
    contact_name: str
    contact_id: str
    my_id: str
    date_from: datetime
    date_to: datetime
    total_messages: int


@dataclass
class ParsedChat:
    my_messages: list
    contact_messages: list
    meta: ChatMeta


def _normalize_text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(
            part if isinstance(part, str) else part.get("text", "")
            for part in raw
        )
    return ""


def _detect_media_type(msg: dict) -> Optional[str]:
    if "photo" in msg:
        return "photo"
    return msg.get("media_type")  # "sticker", "voice_message", etc. or None


def parse_chat(path: str, my_id: str) -> ParsedChat:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    messages = [m for m in data["messages"] if m.get("type") == "message"]

    contact_id = None
    contact_name = None
    for m in messages:
        fid = m.get("from_id", "")
        if fid != my_id:
            contact_id = fid
            contact_name = m.get("from", fid)
            break

    if contact_id is None:
        raise ValueError("В чате не найден собеседник — возможно, my_id указан неверно.")

    my_msgs: list[Message] = []
    contact_msgs: list[Message] = []

    for m in messages:
        fid = m.get("from_id", "")
        if fid not in (my_id, contact_id):
            continue  # групповые участники — пропускаем на MVP
        msg = Message(
            from_id=fid,
            text=_normalize_text(m.get("text", "")),
            date=datetime.fromisoformat(m["date"]),
            media_type=_detect_media_type(m),
        )
        if fid == my_id:
            my_msgs.append(msg)
        else:
            contact_msgs.append(msg)

    all_msgs = my_msgs + contact_msgs
    all_msgs.sort(key=lambda x: x.date)

    meta = ChatMeta(
        contact_name=contact_name,
        contact_id=contact_id,
        my_id=my_id,
        date_from=all_msgs[0].date if all_msgs else None,
        date_to=all_msgs[-1].date if all_msgs else None,
        total_messages=len(all_msgs),
    )

    return ParsedChat(my_messages=my_msgs, contact_messages=contact_msgs, meta=meta)
