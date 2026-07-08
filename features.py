import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from tg_parser import Message, ParsedChat

SESSION_GAP = timedelta(hours=4)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001FA00-\U0001FA9F"
    "\U00002702-\U000027B0]+",
    flags=re.UNICODE,
)
_FORMAL_RE = re.compile(r"\b(вы|ваш|ваша|ваше|ваши|вам|вас|вами)\b", re.IGNORECASE)
_INFORMAL_RE = re.compile(r"\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои)\b", re.IGNORECASE)


@dataclass
class SideFeatures:
    total_messages: int
    avg_message_length: float          # символов, только текстовые
    avg_response_latency_sec: Optional[float]  # None если нет ни одного ответа
    question_ratio: float              # доля сообщений с ?
    emoji_per_message: float           # среднее эмодзи на сообщение
    initiative_ratio: float            # доля сессий, начатых этой стороной
    photo_ratio: float                 # фото / все сообщения
    formality: str                     # "formal" | "informal" | "mixed" | "unknown"


@dataclass
class ChatFeatures:
    my: SideFeatures
    contact: SideFeatures


# ── helpers ──────────────────────────────────────────────────────────────────

def _avg(values: list) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _count_emojis(text: str) -> int:
    return sum(len(m.group()) for m in _EMOJI_RE.finditer(text))


def _formality(messages: list) -> str:
    formal = sum(len(_FORMAL_RE.findall(m.text)) for m in messages)
    informal = sum(len(_INFORMAL_RE.findall(m.text)) for m in messages)
    total = formal + informal
    if total == 0:
        return "unknown"
    ratio = formal / total
    if ratio > 0.7:
        return "formal"
    if ratio < 0.3:
        return "informal"
    return "mixed"


def _split_sessions(all_msgs: list) -> list:
    if not all_msgs:
        return []
    sessions, current = [], [all_msgs[0]]
    for msg in all_msgs[1:]:
        if msg.date - current[-1].date > SESSION_GAP:
            sessions.append(current)
            current = []
        current.append(msg)
    sessions.append(current)
    return sessions


def _response_latencies(all_msgs: list, side_id: str, other_id: str) -> list:
    latencies = []
    for i, msg in enumerate(all_msgs):
        if i == 0 or msg.from_id != side_id:
            continue
        prev = all_msgs[i - 1]
        if prev.from_id != other_id:
            continue
        gap = (msg.date - prev.date).total_seconds()
        if gap < SESSION_GAP.total_seconds():
            latencies.append(gap)
    return latencies


# ── public API ────────────────────────────────────────────────────────────────

def extract_features(chat: ParsedChat) -> ChatFeatures:
    my_id = chat.meta.my_id
    contact_id = chat.meta.contact_id

    all_msgs: list[Message] = sorted(
        chat.my_messages + chat.contact_messages, key=lambda m: m.date
    )

    sessions = _split_sessions(all_msgs)
    total_sessions = len(sessions)

    def _initiative(side_id: str) -> float:
        if total_sessions == 0:
            return 0.0
        started = sum(1 for s in sessions if s[0].from_id == side_id)
        return started / total_sessions

    def _side_features(msgs: list, side_id: str, other_id: str) -> SideFeatures:
        total = len(msgs)
        text_msgs = [m for m in msgs if m.text]
        latencies = _response_latencies(all_msgs, side_id, other_id)

        return SideFeatures(
            total_messages=total,
            avg_message_length=_avg([len(m.text) for m in text_msgs]) or 0.0,
            avg_response_latency_sec=_avg(latencies),
            question_ratio=(sum(1 for m in text_msgs if m.text.rstrip().endswith("?")) / len(text_msgs)) if text_msgs else 0.0,
            emoji_per_message=_avg([_count_emojis(m.text) for m in text_msgs]) or 0.0,
            initiative_ratio=_initiative(side_id),
            photo_ratio=sum(1 for m in msgs if m.media_type == "photo") / total if total else 0.0,
            formality=_formality(text_msgs),
        )

    return ChatFeatures(
        my=_side_features(chat.my_messages, my_id, contact_id),
        contact=_side_features(chat.contact_messages, contact_id, my_id),
    )
