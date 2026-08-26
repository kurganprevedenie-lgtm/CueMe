"""compatibility_metrics.py — детерминированные метрики «Анализа собеседника»,
БЕЗ LLM. Каждая функция берёт rows (список {"date": ISO-строка, "direction":
"in"/"out", "text": str}, вся история контакта — business + imported).

Возвращает СЫРЫЕ ТИПИЗИРОВАННЫЕ ЧИСЛА (не готовые строки) — форматирование в
текст факта/короткого значения отдельным слоем ниже (_format_*), чтобы числа
считались один раз, а не дублировались между «что посчитали» и «что показали».

Работает на том, что реально доступно через Business API прямо сейчас: text,
date, direction. БЕЗ реакций, БЕЗ длительности голосовых, БЕЗ фото — эти поля
физически не собираются (см. raw_meta в main.py: только length/has_emoji/voice).
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import CONFLICT_WORDS, WARMTH_WORDS

_WARMTH_RE = re.compile("|".join(re.escape(w) for w in WARMTH_WORDS), re.IGNORECASE)
_CONFLICT_RE = re.compile("|".join(re.escape(w) for w in CONFLICT_WORDS), re.IGNORECASE)


def _sorted_texted(rows: list[dict]) -> list[dict]:
    return sorted(
        (r for r in rows if r.get("text") and r.get("date")),
        key=lambda r: r["date"],
    )


def _parse_dt(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


# ── 1. Баланс ──────────────────────────────────────────────────────────────

def balance(rows: list[dict]) -> tuple[int, int, str]:
    """(сообщений автора, сообщений собеседника, "55/45")."""
    msgs = [r for r in rows if r.get("text")]
    n_author = sum(1 for r in msgs if r["direction"] == "out")
    n_contact = len(msgs) - n_author
    total = n_author + n_contact
    if total == 0:
        return 0, 0, "—"
    ratio = f"{round(n_author / total * 100)}/{round(n_contact / total * 100)}"
    return n_author, n_contact, ratio


# ── 2. Скорость ответа, раздельно по направлениям ───────────────────────────

def response_speed_median(rows: list[dict]) -> tuple[float | None, float | None]:
    """(медиана сек — как быстро АВТОР отвечает собеседнику,
        медиана сек — как быстро СОБЕСЕДНИК отвечает автору).
    Только пары со сменой направления и разрывом <3ч (иначе это уже не
    «ответ», а новое сообщение после паузы — см. initiation_after_pause)."""
    msgs = _sorted_texted(rows)
    cap = timedelta(hours=3)
    author_gaps: list[float] = []   # cur=out отвечает на prev=in
    contact_gaps: list[float] = []  # cur=in отвечает на prev=out
    for prev, cur in zip(msgs, msgs[1:]):
        if cur["direction"] == prev["direction"]:
            continue
        prev_dt, cur_dt = _parse_dt(prev["date"]), _parse_dt(cur["date"])
        if not prev_dt or not cur_dt:
            continue
        delta = cur_dt - prev_dt
        if not (timedelta(0) <= delta < cap):
            continue
        sec = delta.total_seconds()
        (author_gaps if cur["direction"] == "out" else contact_gaps).append(sec)

    def _median(xs: list[float]) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    return _median(author_gaps), _median(contact_gaps)


# ── 3. Инициатива после паузы, по ВСЕЙ истории ──────────────────────────────

def initiation_after_pause(rows: list[dict], pause_hours: int = 4) -> tuple[int, int]:
    """(сколько раз ПОСЛЕ паузы ≥pause_hours первым написал автор,
        сколько раз первым написал собеседник). Без ограничения объёма —
    единственная метрика, где важна каждая пауза за всё время, не выборка."""
    msgs = _sorted_texted(rows)
    gap = timedelta(hours=pause_hours)
    author_first = 0
    contact_first = 0
    for prev, cur in zip(msgs, msgs[1:]):
        prev_dt, cur_dt = _parse_dt(prev["date"]), _parse_dt(cur["date"])
        if not prev_dt or not cur_dt:
            continue
        if cur_dt - prev_dt >= gap:
            if cur["direction"] == "in":
                contact_first += 1
            else:
                author_first += 1
    return author_first, contact_first


# ── 4. Долгие паузы ──────────────────────────────────────────────────────────

def long_pauses(rows: list[dict], threshold_hours: int = 24) -> tuple[int, list[str]]:
    """(количество разрывов ≥threshold_hours, даты сообщений, прервавших паузу —
    YYYY-MM-DD, самые свежие последними)."""
    msgs = _sorted_texted(rows)
    dates: list[str] = []
    for prev, cur in zip(msgs, msgs[1:]):
        prev_dt, cur_dt = _parse_dt(prev["date"]), _parse_dt(cur["date"])
        if not prev_dt or not cur_dt:
            continue
        if cur_dt - prev_dt >= timedelta(hours=threshold_hours):
            dates.append(cur["date"][:10])
    return len(dates), dates


# ── 5. Тепло / конфликт ──────────────────────────────────────────────────────

def warmth_conflict(rows: list[dict]) -> tuple[int, float, int, float]:
    """(тёплых сообщений, % тёплых, конфликтных сообщений, % конфликтных) —
    % от общего объёма сообщений (обе стороны вместе)."""
    msgs = [r for r in rows if r.get("text")]
    total = len(msgs)
    if total == 0:
        return 0, 0.0, 0, 0.0
    warm = sum(1 for r in msgs if _WARMTH_RE.search(r["text"].lower()))
    conflict = sum(1 for r in msgs if _CONFLICT_RE.search(r["text"].lower()))
    return warm, warm / total, conflict, conflict / total


# ── 6. Циркадное совпадение ──────────────────────────────────────────────────

_NIGHT_OWL_HOURS = set(range(22, 24)) | set(range(0, 4))
_EARLY_BIRD_HOURS = set(range(6, 10))


def circadian_overlap(rows: list[dict]) -> tuple[int | None, int | None, str]:
    """(пиковый час автора, пиковый час собеседника, текстовая оценка —
    "совпадают"/"частично"/"разные"). None у часов, если данных не хватило."""
    msgs = [r for r in rows if r.get("text") and r.get("date")]
    my_hours: Counter = Counter()
    ct_hours: Counter = Counter()
    for r in msgs:
        dt = _parse_dt(r["date"])
        if not dt:
            continue
        (my_hours if r["direction"] == "out" else ct_hours)[dt.hour] += 1

    if not my_hours or not ct_hours:
        return None, None, "недостаточно данных"

    my_peak = my_hours.most_common(1)[0][0]
    ct_peak = ct_hours.most_common(1)[0][0]
    overlap = abs(my_peak - ct_peak)
    overlap = min(overlap, 24 - overlap)  # по кругу суток
    label = "совпадают" if overlap <= 2 else "частично совпадают" if overlap <= 5 else "разные"
    return my_peak, ct_peak, label


def _hour_label(hour: int) -> str:
    if hour in _NIGHT_OWL_HOURS:
        return "сова"
    if hour in _EARLY_BIRD_HOURS:
        return "жаворонок"
    return "день"


# ── 7. Динамика объёма с адаптивной группировкой ────────────────────────────

@dataclass
class Period:
    label: str
    n_author: int
    n_contact: int

    @property
    def total(self) -> int:
        return self.n_author + self.n_contact


@dataclass
class VolumeTrend:
    granularity: str  # "month" | "week" | "day"
    periods: list[Period] = field(default_factory=list)
    peak: Period | None = None
    latest: Period | None = None


def _pick_granularity(days: int) -> str:
    """>45 дней — по месяцам, 8-45 — по неделям, <8 — по дням. Пороги: переписка
    короче 8 дней помесячно дала бы 1-2 периода без всякой динамики; длиннее
    45 дней понедельно дала бы захламлённый список из 6+ строк в таблице."""
    if days > 45:
        return "month"
    if days >= 8:
        return "week"
    return "day"


def _period_key(dt: datetime, granularity: str) -> tuple[str, str]:
    """(ключ для группировки, человекочитаемая подпись)."""
    if granularity == "month":
        return dt.strftime("%Y-%m"), dt.strftime("%m.%Y")
    if granularity == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}", f"нед. {iso[1]}"
    return dt.strftime("%Y-%m-%d"), dt.strftime("%d.%m")


def volume_trend(rows: list[dict]) -> VolumeTrend:
    """Динамика объёма по периодам (адаптивная группировка) + отдельно пиковый
    и последний период — для сравнения «было/стало» в тексте вывода."""
    msgs = _sorted_texted(rows)
    if len(msgs) < 4:
        return VolumeTrend(granularity="day")

    first_dt, last_dt = _parse_dt(msgs[0]["date"]), _parse_dt(msgs[-1]["date"])
    days = max(1, (last_dt - first_dt).days) if first_dt and last_dt else 1
    granularity = _pick_granularity(days)

    buckets: dict[str, Period] = {}
    order: list[str] = []
    for r in msgs:
        dt = _parse_dt(r["date"])
        if not dt:
            continue
        key, label = _period_key(dt, granularity)
        if key not in buckets:
            buckets[key] = Period(label=label, n_author=0, n_contact=0)
            order.append(key)
        if r["direction"] == "out":
            buckets[key].n_author += 1
        else:
            buckets[key].n_contact += 1

    periods = [buckets[k] for k in order]
    peak = max(periods, key=lambda p: p.total) if periods else None
    latest = periods[-1] if periods else None
    return VolumeTrend(granularity=granularity, periods=periods, peak=peak, latest=latest)


# ── Форматирование: сырые числа → короткое значение + факт для LLM ─────────

def _fmt_seconds(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f} сек"
    if sec < 3600:
        return f"{sec / 60:.0f} мин"
    return f"{sec / 3600:.1f} ч"


def compute_all(rows: list[dict]) -> dict[str, dict]:
    """Считает все метрики и сразу форматирует (short, fact) для каждой —
    возвращает {key: {"label", "short", "fact"}}, тот же контракт, что
    ждут main.py/llm.py, но числа внутри fact теперь и абсолютные, и %,
    посчитанные из типизированных функций выше, не строками напрямую."""
    out: dict[str, dict] = {}

    n_author, n_contact, ratio = balance(rows)
    total = n_author + n_contact
    out["balance"] = {
        "label": "Баланс",
        "short": ratio if total else "—",
        "fact": (
            f"{n_author} сообщений от тебя, {n_contact} от собеседника "
            f"(соотношение {ratio})" if total else "Сообщений нет — баланс посчитать не на чем."
        ),
    }

    med_author, med_contact = response_speed_median(rows)
    if med_author is None and med_contact is None:
        out["response_speed"] = {
            "label": "Скорость ответов", "short": "—",
            "fact": "Пар «ответ в пределах 3ч» не набралось — темп посчитать не на чем.",
        }
    else:
        a_s = _fmt_seconds(med_author) if med_author is not None else "—"
        c_s = _fmt_seconds(med_contact) if med_contact is not None else "—"
        out["response_speed"] = {
            "label": "Скорость ответов", "short": f"ты {a_s} / она {c_s}",
            "fact": (
                f"Медианное время ответа автора собеседнику — {a_s}, "
                f"собеседника автору — {c_s}."
            ),
        }

    a_first, c_first = initiation_after_pause(rows)
    pauses = a_first + c_first
    if pauses == 0:
        out["initiation"] = {
            "label": "Инициатива после паузы", "short": "—",
            "fact": "Пауз ≥4ч в переписке не было.",
        }
    else:
        out["initiation"] = {
            "label": "Инициатива после паузы",
            "short": f"{c_first}/{a_first} собеседник/ты",
            "fact": (
                f"После паузы ≥4ч первым писал собеседник {c_first} раз, ты — "
                f"{a_first} раз (всего {pauses} пауз)."
            ),
        }

    lp_count, lp_dates = long_pauses(rows)
    out["long_pauses"] = {
        "label": "Долгие паузы", "short": str(lp_count),
        "fact": (
            f"Разрывов ≥24ч без сообщений — {lp_count}"
            + (f", последний {lp_dates[-1]}." if lp_dates else ".")
        ),
    }

    warm_n, warm_pct, conf_n, conf_pct = warmth_conflict(rows)
    out["warmth_conflict"] = {
        "label": "Тепло / конфликт",
        "short": f"💚{warm_n} / 🚩{conf_n}",
        # числом, не только текстом внутри fact/short — main.py читает это
        # напрямую для «лучшая совместимость» в «Анализ своего стиля»
        # (сравнение между контактами), а не парсит регуляркой готовый текст.
        "warmth_pct": warm_pct,
        "fact": (
            f"Тёплая лексика — в {warm_n} сообщениях ({warm_pct:.0%} от объёма), "
            f"конфликтная — в {conf_n} ({conf_pct:.0%})."
        ),
    }

    my_peak, ct_peak, overlap_label = circadian_overlap(rows)
    if my_peak is None:
        out["circadian"] = {
            "label": "Совпадение по времени", "short": "—",
            "fact": "Данных по времени сообщений недостаточно.",
        }
    else:
        out["circadian"] = {
            "label": "Совпадение по времени", "short": overlap_label,
            "fact": (
                f"Твой пик активности — {my_peak}:00 ({_hour_label(my_peak)}), у "
                f"собеседника — {ct_peak}:00 ({_hour_label(ct_peak)}), пики {overlap_label}."
            ),
        }

    out["_volume_trend"] = volume_trend(rows)
    return out
