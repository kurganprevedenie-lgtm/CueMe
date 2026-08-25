"""compatibility_metrics.py — детерминированные метрики «Анализа собеседника»,
БЕЗ LLM. Каждая функция берёт rows (список {"date": ISO-строка, "direction":
"in"/"out", "text": str}, вся история контакта — business + imported) и
возвращает (short, fact):
  - short — короткое значение для ячейки таблицы в Rich Message
  - fact  — точный факт с цифрами, идёт в промпт LLM как готовые данные
            (интерпретацию живым языком коуча пишет build_compatibility_interpretation
            в llm.py — сама метрика ничего не интерпретирует, только считает)

Работает на том, что реально доступно через Business API прямо сейчас: text,
date, direction. БЕЗ реакций, БЕЗ длительности голосовых, БЕЗ фото — эти поля
физически не собираются (см. raw_meta в main.py: только length/has_emoji/voice).
"""
import re
from collections import Counter
from datetime import datetime, timedelta

from config import CONFLICT_WORDS, WARMTH_WORDS

_WARMTH_RE = re.compile("|".join(re.escape(w) for w in WARMTH_WORDS), re.IGNORECASE)
_CONFLICT_RE = re.compile("|".join(re.escape(w) for w in CONFLICT_WORDS), re.IGNORECASE)


def _sorted_texted(rows: list[dict]) -> list[dict]:
    return sorted(
        (r for r in rows if r.get("text") and r.get("date")),
        key=lambda r: r["date"],
    )


def balance(rows: list[dict]) -> tuple[str, str]:
    """Баланс сообщений — % каждой стороны."""
    msgs = [r for r in rows if r.get("text")]
    total = len(msgs)
    if total == 0:
        return "—", "Сообщений нет — баланс посчитать не на чем."
    my = sum(1 for r in msgs if r["direction"] == "out")
    ct = total - my
    my_pct = my / total
    ct_pct = ct / total
    return (
        f"{my_pct:.0%} / {ct_pct:.0%}",
        f"Ты написал {my} сообщений ({my_pct:.0%} от {total}), собеседник — "
        f"{ct} ({ct_pct:.0%}).",
    )


def response_speed_median(rows: list[dict]) -> tuple[str, str]:
    """Медиана задержки ответа (не среднее — устойчивее к редким выбросам) для
    пар, где сторона сменилась и разрыв <3ч. Считается по ОБОИМ направлениям
    вместе — это темп разговора в целом, не «кто быстрее»."""
    msgs = _sorted_texted(rows)
    gaps_min: list[float] = []
    cap = timedelta(hours=3)
    for prev, cur in zip(msgs, msgs[1:]):
        if cur["direction"] == prev["direction"]:
            continue
        try:
            delta = datetime.fromisoformat(cur["date"]) - datetime.fromisoformat(prev["date"])
        except (ValueError, TypeError):
            continue
        if timedelta(0) <= delta < cap:
            gaps_min.append(delta.total_seconds() / 60)

    if not gaps_min:
        return "—", "Пар «ответ в пределах 3ч» не набралось — темп посчитать не на чем."

    gaps_min.sort()
    n = len(gaps_min)
    median = gaps_min[n // 2] if n % 2 else (gaps_min[n // 2 - 1] + gaps_min[n // 2]) / 2

    if median < 1:
        short = "<1 мин"
    elif median < 60:
        short = f"{median:.0f} мин"
    else:
        short = f"{median / 60:.1f} ч"
    return short, f"Медианная задержка ответа — {short} (по {n} парам сообщений)."


def initiation_after_pause(rows: list[dict], pause_hours: int = 4) -> tuple[str, str]:
    """Кто чаще пишет первым после паузы ≥pause_hours, по ВСЕЙ истории (без
    ограничения объёма — это единственная метрика, где важна каждая пауза за
    всё время, а не выборка)."""
    msgs = _sorted_texted(rows)
    gap = timedelta(hours=pause_hours)
    pauses = 0
    my_first = 0
    ct_first = 0
    for prev, cur in zip(msgs, msgs[1:]):
        try:
            delta = datetime.fromisoformat(cur["date"]) - datetime.fromisoformat(prev["date"])
        except (ValueError, TypeError):
            continue
        if delta >= gap:
            pauses += 1
            if cur["direction"] == "in":
                ct_first += 1
            else:
                my_first += 1

    if pauses == 0:
        return "—", f"Пауз ≥{pause_hours}ч в переписке не было."

    ct_pct = ct_first / pauses
    short = f"{ct_pct:.0%} собеседник"
    return (
        short,
        f"После паузы ≥{pause_hours}ч первым пишет собеседник в {ct_first} из "
        f"{pauses} случаев ({ct_pct:.0%}), ты — в {my_first} ({my_first / pauses:.0%}).",
    )


def long_pauses(rows: list[dict], threshold_hours: int = 24) -> tuple[str, str]:
    """Количество разрывов ≥threshold_hours за всю историю."""
    msgs = _sorted_texted(rows)
    count = 0
    for prev, cur in zip(msgs, msgs[1:]):
        try:
            delta = datetime.fromisoformat(cur["date"]) - datetime.fromisoformat(prev["date"])
        except (ValueError, TypeError):
            continue
        if delta >= timedelta(hours=threshold_hours):
            count += 1
    return str(count), f"Разрывов ≥{threshold_hours}ч без сообщений за всю переписку — {count}."


def warmth_conflict_ratio(rows: list[dict]) -> tuple[str, str]:
    """% сообщений (от общего объёма, обе стороны вместе) с тёплой лексикой и
    % с конфликтной — по подстроке, нижний регистр, словари в config.py."""
    msgs = [r for r in rows if r.get("text")]
    total = len(msgs)
    if total == 0:
        return "—", "Сообщений нет — лексику посчитать не на чем."
    warm = sum(1 for r in msgs if _WARMTH_RE.search(r["text"].lower()))
    conflict = sum(1 for r in msgs if _CONFLICT_RE.search(r["text"].lower()))
    warm_pct = warm / total
    conflict_pct = conflict / total
    return (
        f"💚{warm_pct:.0%} / 🚩{conflict_pct:.0%}",
        f"Тёплая лексика (люблю/скучаю/обнимаю и т.п.) — в {warm} сообщениях "
        f"из {total} ({warm_pct:.0%}). Конфликтная (бесит/достал/обиделась и "
        f"т.п.) — в {conflict} ({conflict_pct:.0%}).",
    )


_NIGHT_OWL_HOURS = set(range(22, 24)) | set(range(0, 4))
_EARLY_BIRD_HOURS = set(range(6, 10))


def circadian_overlap(rows: list[dict]) -> tuple[str, str]:
    """Пиковые часы активности каждой стороны — совпадают ли (сова/жаворонок)."""
    msgs = [r for r in rows if r.get("text") and r.get("date")]
    my_hours: Counter = Counter()
    ct_hours: Counter = Counter()
    for r in msgs:
        try:
            hour = datetime.fromisoformat(r["date"]).hour
        except (ValueError, TypeError):
            continue
        (my_hours if r["direction"] == "out" else ct_hours)[hour] += 1

    if not my_hours or not ct_hours:
        return "—", "Данных по времени сообщений одной из сторон недостаточно."

    my_peak = my_hours.most_common(1)[0][0]
    ct_peak = ct_hours.most_common(1)[0][0]
    overlap = abs(my_peak - ct_peak)
    overlap = min(overlap, 24 - overlap)  # по кругу суток

    def _label(hour: int) -> str:
        if hour in _NIGHT_OWL_HOURS:
            return "сова"
        if hour in _EARLY_BIRD_HOURS:
            return "жаворонок"
        return "день"

    my_label, ct_label = _label(my_peak), _label(ct_peak)
    if overlap <= 2:
        short = "совпадают"
    elif overlap <= 5:
        short = "частично"
    else:
        short = "разные"
    return (
        short,
        f"Твой пик активности — {my_peak}:00 ({my_label}), у собеседника — "
        f"{ct_peak}:00 ({ct_label}), разница {overlap}ч.",
    )


def volume_trend(rows: list[dict]) -> tuple[str, str]:
    """Тренд объёма сообщений по месяцам + ОТДЕЛЬНО тренд % тёплой лексики —
    падение объёма само по себе не тревожно, тревожно если ПАРАЛЛЕЛЬНО падает
    доля тепла (не только меньше пишут, но и суше)."""
    msgs = _sorted_texted(rows)
    if len(msgs) < 10:
        return "—", "Сообщений мало для помесячного тренда."

    by_month: dict[str, list[dict]] = {}
    for r in msgs:
        key = r["date"][:7]  # YYYY-MM
        by_month.setdefault(key, []).append(r)

    months = sorted(by_month)
    if len(months) < 2:
        return "—", "Вся переписка уместилась в один месяц — тренд по месяцам не посчитать."

    counts = [len(by_month[m]) for m in months]
    warmths = [
        sum(1 for r in by_month[m] if _WARMTH_RE.search(r["text"].lower())) / len(by_month[m])
        for m in months
    ]

    half = len(months) // 2 or 1
    early_count = sum(counts[:half]) / half
    late_count = sum(counts[half:]) / max(1, len(counts) - half)
    early_warm = sum(warmths[:half]) / half
    late_warm = sum(warmths[half:]) / max(1, len(warmths) - half)

    def _trend(early: float, late: float, tol: float) -> str:
        if early == 0:
            return "рост" if late > 0 else "ровно"
        change = (late - early) / early
        if change > tol:
            return "рост"
        if change < -tol:
            return "спад"
        return "ровно"

    vol_trend = _trend(early_count, late_count, 0.15)
    warm_trend = _trend(early_warm, late_warm, 0.15)

    short = f"объём {vol_trend}, тепло {warm_trend}"
    fact = (
        f"Объём переписки по месяцам: {vol_trend} (в начале ~{early_count:.0f} "
        f"сообщ./мес, сейчас ~{late_count:.0f}). Доля тёплой лексики: {warm_trend} "
        f"({early_warm:.0%} → {late_warm:.0%})."
    )
    if vol_trend == "спад" and warm_trend != "спад":
        fact += " Меньше пишут, но не суше — это не тревожный спад."
    elif vol_trend == "спад" and warm_trend == "спад":
        fact += " Падает и объём, и тепло одновременно — это тревожнее, чем просто меньше сообщений."
    return short, fact


# Порядок и человекочитаемые названия метрик — единый источник для таблицы
# Rich Message, промпта интерпретации и кэша.
METRICS = [
    ("balance", "Баланс", balance),
    ("response_speed", "Скорость ответов", response_speed_median),
    ("initiation", "Инициатива после паузы", initiation_after_pause),
    ("long_pauses", "Долгие паузы", long_pauses),
    ("warmth_conflict", "Тепло / конфликт", warmth_conflict_ratio),
    ("circadian", "Совпадение по времени", circadian_overlap),
    ("volume_trend", "Тренд переписки", volume_trend),
]


def compute_all(rows: list[dict]) -> dict[str, dict[str, str]]:
    """Считает все 7 метрик разом. Возвращает {key: {"label", "short", "fact"}}."""
    out = {}
    for key, label, fn in METRICS:
        short, fact = fn(rows)
        out[key] = {"label": label, "short": short, "fact": fact}
    return out
