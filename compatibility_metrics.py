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
from features import _looks_junky

# НЕ анкерим на границу слова (сознательно, после проверки — см. докстринг
# ниже): словари хранят стемы («ссор», «злит», «люблю», «целую»), которые
# по-русски регулярно стоят ПОСЛЕ приставки — «поссорились», «разозлит»,
# «обозлит», «полюблю», «поцелую» — всё это НАСТОЯЩИЕ теплые/конфликтные
# формы, но стем в них НЕ в начале слова. \b или лукбихайнд «не после буквы»
# перед стемом отсёк бы все эти случаи как побочный эффект — то есть сломал
# бы то, что словарь и так, а не то, что было в баг-репорте. Разбор реального
# примера («ХПХПХПХПХПХПХПХ Я ДУМАЛА ТЫ РАССТРОИЛСЯ» матчит «расстроил» —
# формально корректный матч, «расстроился» ДЕЙСТВИТЕЛЬНО начинается с этого
# стема) показал, что причина ложного срабатывания — не позиция подстроки, а
# смеховой контекст сообщения целиком. Поэтому границу оставляем прежней
# (substring), а настоящий фикс — фильтр смехового спама ниже.
_WARMTH_RE = re.compile("|".join(re.escape(w) for w in WARMTH_WORDS), re.IGNORECASE)
_CONFLICT_RE = re.compile("|".join(re.escape(w) for w in CONFLICT_WORDS), re.IGNORECASE)

# Смеховой спам («хахаха», «ахахах», «ХПХПХПХПХП», «кекекек») — общий шаблон
# «короткий слог 2-4 символа подряд 3+ раза», а не перечисление вариантов
# написания смеха: ловит любые вариации, не только заранее угаданные. Плюс
# несколько типовых smeh-маркеров, которые сами по себе не повторяются
# («кек», «лол», «ору») и потому под общий паттерн не подпадают.
_LAUGH_REPEAT_RE = re.compile(r"([a-zа-яё]{2,4})\1{2,}", re.IGNORECASE)
_LAUGH_WORD_RE = re.compile(r"\b(кек+|лол+|ору{2,})\b", re.IGNORECASE)


def _is_laugh_spam(text: str) -> bool:
    """Сообщение похоже на смеховой спам, а не на осмысленный текст — такое
    НЕ считаем конфликтом, даже если формально задело словарное слово (см.
    warmth_conflict: «ХПХПХПХПХПХПХПХ Я ДУМАЛА ТЫ РАССТРОИЛСЯ» матчит стем
    «расстроил», но по сути это смех, а не ссора)."""
    return bool(_LAUGH_REPEAT_RE.search(text) or _LAUGH_WORD_RE.search(text))


# 2-е лицо в сообщении — сигнал, что конфликтное слово адресовано СОБЕСЕДНИКУ
# напрямую («ты заебал»), а не просто упомянуто в другом контексте. Используется
# как приоритет при выборе, какие 2 примера показать (не жёсткий фильтр —
# сообщение без «ты» всё ещё считается конфликтом, просто ниже приоритетом
# для цитаты).
_SECOND_PERSON_RE = re.compile(
    r"\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоего|твоей|твоим|твоих|твою)\b",
    re.IGNORECASE,
)


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

def _weeks_span(rows: list[dict]) -> float:
    """Сколько недель охватывает переписка (по датам с текстом) — минимум 1,
    чтобы частота «в неделю» не улетала в небо на паре сообщений за один день."""
    dated = _sorted_texted(rows)
    if len(dated) < 2:
        return 1.0
    first, last = _parse_dt(dated[0]["date"]), _parse_dt(dated[-1]["date"])
    if not first or not last:
        return 1.0
    return max((last - first).days / 7.0, 1.0)


def warmth_conflict(
    rows: list[dict],
) -> tuple[int, float, int, float, list[tuple[str, str]], list[tuple[str, str]], float]:
    """(тёплых сообщений, % тёплых, конфликтных сообщений, % конфликтных,
    примеры тёплых, примеры конфликтных, недель в переписке).

    % оставлен в возврате (main.py читает warmth_pct напрямую для сравнения
    контактов между собой в «лучшая совместимость», см. main._best_compatibility_
    contact) — но НЕ используется как заглавная метрика в тексте карточки:
    доля от ВСЕЙ переписки (включая бытовую) занижает результат — 132 тёплых
    сообщения могут дать всего 2%, хотя абсолютно это много. Текст карточки
    (main.py compute_all → fact) строится на абсолютном числе + частоте в
    неделю, weeks — общий делитель для обеих метрик.

    Конфликтные слова матчатся С ФИЛЬТРОМ смехового спама (_is_laugh_spam) —
    иначе «ХПХПХПХПХПХПХПХ Я ДУМАЛА ТЫ РАССТРОИЛСЯ» (стем «расстроил» внутри
    «расстроился») засчитывался бы как конфликт, будучи по сути смехом.
    Тёплые слова этому фильтру не подвергаются — там риск подобной ложной
    тревоги не стоит того, чтобы усложнять.

    Примеры — до 2 совпадений на категорию, (direction, текст сообщения),
    отфильтрованы через ту же _looks_junky, что и цитаты в
    features.initiative_axis (не пропускает голые ссылки/эмодзи/однобуквенный
    мусор). Тёплые — самые свежие первыми. Конфликтные — сперва адресованные
    собеседнику напрямую (есть «ты/тебя/тебе...» в том же сообщении —
    _SECOND_PERSON_RE), внутри каждой группы тоже свежие первыми: «ты заебал
    на меня бочку катить» показательнее случайного «бесит» без адресата."""
    msgs = [r for r in rows if r.get("text")]
    total = len(msgs)
    if total == 0:
        return 0, 0.0, 0, 0.0, [], [], 1.0

    warm = sum(1 for r in msgs if _WARMTH_RE.search(r["text"].lower()))
    conflict = sum(
        1 for r in msgs
        if _CONFLICT_RE.search(r["text"].lower()) and not _is_laugh_spam(r["text"])
    )

    warm_examples: list[tuple[str, str]] = []
    # (адресовано_2му_лицу, direction, текст) — сортируем по приоритету ниже,
    # затем берём первые 2.
    conflict_candidates: list[tuple[bool, str, str]] = []
    for r in reversed(_sorted_texted(rows)):  # с конца — свежие сообщения первыми
        text = r["text"]
        if _looks_junky(text):
            continue
        low = text.lower()
        if len(warm_examples) < 2 and _WARMTH_RE.search(low):
            warm_examples.append((r["direction"], text.strip()))
        if _CONFLICT_RE.search(low) and not _is_laugh_spam(text):
            addressed = bool(_SECOND_PERSON_RE.search(low))
            conflict_candidates.append((addressed, r["direction"], text.strip()))

    # stable sort по (адресовано первым), порядок появления (уже свежие→старые) сохраняется.
    conflict_candidates.sort(key=lambda c: not c[0])
    conflict_examples = [(direction, text) for _, direction, text in conflict_candidates[:2]]

    weeks = _weeks_span(rows)
    return warm, warm / total, conflict, conflict / total, warm_examples, conflict_examples, weeks


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


def _ru_count_word(n: int, one: str, few: str, many: str) -> str:
    """Склонение по числу: 1 → one, 2-4 → few, 5-20/0/остальное → many
    (стандартное русское правило с исключением 11-14)."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _fmt_times_per_week(rate: float) -> str:
    """«5 раз в неделю» / «1.5 раза в неделю» — целое склоняется по числу,
    дробное всегда «раза» (стандартная русская норма для нецелых)."""
    if abs(rate - round(rate)) < 0.05:
        n = round(rate)
        word = _ru_count_word(n, "раз", "раза", "раз")
        return f"{n} {word} в неделю"
    return f"{rate:.1f} раза в неделю"


def _fmt_weeks_span(weeks: float) -> str:
    """«идёт {фраза}» — accusative для длительности: «идёт неделю» / «идёт
    3 недели» / «идёт 25 недель»."""
    if weeks < 1.5:
        return "меньше недели"
    n = round(weeks)
    word = _ru_count_word(n, "неделю", "недели", "недель")
    return f"{n} {word}"


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

    warm_n, warm_pct, conf_n, conf_pct, warm_examples, conflict_examples, weeks = warmth_conflict(rows)
    out["warmth_conflict"] = {
        "label": "Тепло / конфликт",
        "short": f"💚{warm_n} / 🚩{conf_n}",
        # % оставлен как ЧИСЛО в словаре (не в тексте fact ниже) — main.py
        # читает warmth_pct напрямую для «лучшая совместимость» в «Анализ
        # своего стиля» (сравнение между контактами), а не парсит регуляркой
        # готовый текст. В САМ fact доля от объёма больше не идёт: доля от
        # ВСЕЙ переписки (включая бытовую) занижает результат — 132 тёплых
        # сообщения могут дать 2%, хотя абсолютно это много (см. warmth_conflict).
        "warmth_pct": warm_pct,
        "fact": (
            f"Тёплых сообщений — {warm_n}, в среднем {_fmt_times_per_week(warm_n / weeks)}. "
            f"Конфликтных — {conf_n}, в среднем {_fmt_times_per_week(conf_n / weeks)} "
            f"(переписка идёт {_fmt_weeks_span(weeks)}). Частота тут не мера качества "
            "отношений: в новых или бурных парах обычно больше и тёплых, и конфликтных "
            "слов, а в давних стабильных тепло часто проявляется делами, а не текстом."
        ),
        # Реальные цитаты — main.py дописывает их отдельной строкой поверх
        # fact/interpretation (см. _warmth_examples_suffix), в LLM-промпт
        # интерпретации НЕ идут (там участвует только fact).
        "warm_examples": warm_examples,
        "conflict_examples": conflict_examples,
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
