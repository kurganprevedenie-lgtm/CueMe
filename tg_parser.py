import json
import re
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


# ── Ручная вставка переписки (copy-paste без JSON) ────────────────────────────

# Заголовок блока при копировании из Telegram Desktop: «Имя, [15.03.2026 12:34]»
_TG_COPY_HEADER_RE = re.compile(r"^(.{1,64}?),\s*\[[^\]]{4,40}\]\s*$")
# Построчный формат «Имя: текст». Имя короткое и без URL-подобного мусора.
_PREFIX_LINE_RE = re.compile(r"^([^:\n]{1,32}):\s+(.+)$")

# Метки «своей» стороны, которые люди чаще всего используют при разметке.
_SELF_MARKERS = {"я", "me", "you", "i", "ты"}


def _norm_speaker(name: str) -> str:
    return name.strip().strip("«»\"'").lower()


def _paste_pairs(text: str) -> list[tuple[str, str]]:
    """Разбирает вставленный текст в список (спикер, сообщение).
    Пустой список — не удалось выделить спикеров."""
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Формат 1: блоки Telegram Desktop («Имя, [дата]» + строки текста ниже)
    pairs: list[tuple[str, str]] = []
    current: Optional[str] = None
    buf: list[str] = []
    header_hits = 0
    for ln in lines:
        m = _TG_COPY_HEADER_RE.match(ln)
        if m:
            header_hits += 1
            if current is not None and buf:
                pairs.append((current, "\n".join(buf).strip()))
            current = m.group(1).strip()
            buf = []
        elif current is not None and ln.strip():
            buf.append(ln.strip())
    if current is not None and buf:
        pairs.append((current, "\n".join(buf).strip()))
    if header_hits >= 2 and pairs:
        return pairs

    # Формат 2: построчно «Имя: текст»
    pairs = []
    for ln in lines:
        m = _PREFIX_LINE_RE.match(ln.strip())
        if m:
            pairs.append((m.group(1).strip(), m.group(2).strip()))
    # Требуем, чтобы префиксы были у большинства непустых строк — иначе это,
    # скорее всего, просто текст с случайным двоеточием, а не разметка.
    nonempty = sum(1 for ln in lines if ln.strip())
    if pairs and nonempty and len(pairs) >= max(2, nonempty // 2):
        return pairs

    return []


def manual_paste_speakers(text: str) -> list[str]:
    """Уникальные спикеры вставленного текста (в порядке появления) — для
    кнопок «кто из них ты?», когда авто-определение не сработало."""
    seen: list[str] = []
    for speaker, _ in _paste_pairs(text):
        if speaker not in seen:
            seen.append(speaker)
    return seen


def parse_manual_paste(
    text: str, my_name: Optional[str] = None
) -> tuple[list[str], list[str], str]:
    """Парсит вручную вставленный кусок переписки.

    Возвращает (my_messages, contact_messages, status):
      "ok"        — успешно разделили стороны;
      "need_side" — два спикера, но непонятно кто из них автор: нужно
                    спросить пользователя и перевызвать с my_name;
      "empty"     — не удалось выделить структуру переписки вообще.
    """
    pairs = _paste_pairs(text)
    if not pairs:
        return [], [], "empty"

    speakers = list(dict.fromkeys(s for s, _ in pairs))

    def _split(self_names: set[str]) -> tuple[list[str], list[str]]:
        my, contact = [], []
        for speaker, msg in pairs:
            (my if _norm_speaker(speaker) in self_names else contact).append(msg)
        return my, contact

    if my_name is not None:
        my, contact = _split({_norm_speaker(my_name)})
        if my:
            return my, contact, "ok"
        return [], [], "empty"  # переданное имя не совпало ни с одним спикером

    self_found = {_norm_speaker(s) for s in speakers if _norm_speaker(s) in _SELF_MARKERS}
    if self_found:
        my, contact = _split(self_found)
        return my, contact, "ok"

    if len(speakers) == 1:
        # Одна сторона без метки «Я» — непонятно, автор это или собеседник.
        return [], [], "need_side"

    if len(speakers) == 2:
        return [], [], "need_side"

    # Три и больше спикеров — групповой чат, на MVP не поддерживаем.
    return [], [], "empty"
