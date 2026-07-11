import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


# ── ситуативные сигналы для генерации ответа (без LLM) ────────────────────────
# Грубые эвристики поверх последней реплики собеседника и объёма переписки.
# Дают промпту генерации ФАКТ (стадия общения, «тяжёлая» реплика), а не догадку
# модели — так правила «стадия/сложные случаи» опираются на данные.

_NEGATIVE_RE = re.compile(
    r"("
    r"не хочу|не буду|не могу|не пиши|не интересно|неинтересно|не вижу смысла|"
    r"отстань|отвали|хватит|прекрати|надоел\w*|бесишь|устал\w* от|давай не\b|"
    r"забудь|расстал\w*|разошлись|разбежались|заблокир\w*|в игнор\w*|"
    r"не трать\w* (?:моё|мое) время|мне всё равно|мне все равно"
    r")",
    re.IGNORECASE,
)

_DRY_ACKS = {
    "ок", "окей", "ok", "угу", "ага", "хм", "мгм", "ясно", "понятно", "пон",
    "ладно", "нз", "нзч", "норм", "нормально", "хз", "ну",
}


def detect_reply_situation(last_incoming: str) -> Optional[str]:
    """Пометка о «тяжёлом» кейсе по последней реплике собеседника, либо None.
    Консервативно: срабатывает только на явный негатив/отказ или на явно сухой
    односложный ответ. Цель — дать промпту факт, а не заставлять угадывать тон."""
    text = (last_incoming or "").strip()
    if not text:
        return None
    if _NEGATIVE_RE.search(text):
        return ("последняя реплика читается как отказ/холод/негатив — отвечай с "
                "достоинством, без дожима и оправданий")
    tokens = re.findall(r"\w+", text.lower())
    if len(tokens) == 1 and tokens[0] in _DRY_ACKS:
        return ("последняя реплика короткая и сухая — интерес под вопросом, не "
                "дожимай, дай лёгкий необязывающий заход")
    return None


_TOTAL_MSGS_RE = re.compile(r"(\d+)\s*сообщ")


def totals_from_summary(features_summary: str) -> Optional[tuple[int, int]]:
    """Достаёт (мои, собеседника) тоталы сообщений из строки make_features_summary
    («Пользователь: N сообщ. ... Собеседник: M сообщ. ...»). Нужно, чтобы стадия
    считалась по РЕАЛЬНОМУ объёму переписки, а не по усечённой длине семплов.
    None, если распарсить не удалось (формат сводки поменялся)."""
    nums = _TOTAL_MSGS_RE.findall(features_summary or "")
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None


def stage_hint(my_total: int, contact_total: int) -> str:
    """Стадия общения по суммарному числу сообщений (грубые корзины).
    Значения приблизительные (семплы могут быть усечены), но направление верное:
    раннее знакомство vs. установившаяся переписка."""
    total = (my_total or 0) + (contact_total or 0)
    if total < 20:
        return ("стадия: свежее знакомство — держи легко и коротко, без глубины "
                "раньше времени")
    if total < 120:
        return "стадия: общение уже идёт — можно теплее и чуть глубже"
    return ("стадия: давняя переписка — уместны глубина и тепло; при тёплой "
            "динамике можно мягко предложить встречу")


def winning_messages(dated_msgs: list[dict], max_examples: int = 3,
                     min_reply_words: int = 3) -> list[str]:
    """«Обучение на своих удачных заходах» без ML: исходящие сообщения автора, на
    которые собеседник ответил ЖИВО — быстро (в пределах сессии), не сухо и не
    негативом, содержательной репликой. Эмпирические примеры «что реально
    заходит» для few-shot в промпте генерации.

    dated_msgs: [{"date": iso-str, "direction": "out"/"in", "text": str}].
    Возвращает до max_examples текстов, свежие первыми, без повторов."""
    msgs = sorted((m for m in dated_msgs if m.get("text")),
                  key=lambda m: m.get("date") or "")
    wins: list[str] = []
    for cur, nxt in zip(msgs, msgs[1:]):
        if cur["direction"] != "out" or nxt["direction"] != "in":
            continue
        reply = (nxt["text"] or "").strip()
        # реплика «зашла»: не сухая/негатив (detect_reply_situation молчит) и содержательная
        if detect_reply_situation(reply) is not None:
            continue
        if len(re.findall(r"\w+", reply)) < min_reply_words:
            continue
        text = (cur["text"] or "").strip()
        if not (1 <= len(re.findall(r"\w+", text)) <= 40):
            continue
        # быстрый ответ — в пределах сессии (если даты парсятся)
        try:
            if datetime.fromisoformat(nxt["date"]) - datetime.fromisoformat(cur["date"]) > SESSION_GAP:
                continue
        except (ValueError, TypeError, KeyError):
            pass
        wins.append(text)

    seen: set[str] = set()
    out: list[str] = []
    for t in reversed(wins):            # свежие важнее
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= max_examples:
            break
    return out
