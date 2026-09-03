"""compatibility_metrics.py — детерминированные метрики «Анализа собеседника».
Каждая функция берёт rows (список {"date": ISO-строка, "direction":
"in"/"out", "text": str}, вся история контакта — business + imported).

Возвращает СЫРЫЕ ТИПИЗИРОВАННЫЕ ЧИСЛА (не готовые строки) — форматирование в
текст факта/короткого значения отдельным слоем ниже (_format_*), чтобы числа
считались один раз, а не дублировались между «что посчитали» и «что показали».

Работает на том, что реально доступно через Business API прямо сейчас: text,
date, direction. БЕЗ реакций, БЕЗ длительности голосовых, БЕЗ фото — эти поля
физически не собираются (см. raw_meta в main.py: только length/has_emoji/voice).

Модуль почти целиком БЕЗ LLM — единственное исключение: warmth() опционально
принимает уже готовый набор LLM-подтверждённых «неоднозначных похвал»
(confirmed_ambiguous), сам LLM не зовёт. Сам вызов — в llm.classify_
ambiguous_praise, оркестрация (сначала найти кандидатов, потом досчитать
warmth() с подтверждениями) — в main.py."""
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import AMBIGUOUS_PRAISE_WORDS, FEELING_VERBS, WARM_EMOJI, WARM_LEXICON
from features import _looks_junky

# Каноническое имя по стему — для occurrence-подсчёта, не важно, вокативное
# слово это или глагол чувств (см. warmth()).
_CANON_BY_STEM = {**WARM_LEXICON, **FEELING_VERBS, **AMBIGUOUS_PRAISE_WORDS}

# Матчинг — по границе слова С ЛЕВОЙ стороны стема, не по вхождению
# подстроки: стемы — намеренно префиксы словоформ («любим» должен матчить
# «любимая»), поэтому \b только слева (\w* справа сам найдёт конец слова).
_WARM_PATTERNS = {
    stem: re.compile(rf"\b{re.escape(stem)}\w*", re.IGNORECASE)
    for stem in WARM_LEXICON
}
_AMBIGUOUS_PATTERNS = {
    stem: re.compile(rf"\b{re.escape(stem)}\w*", re.IGNORECASE)
    for stem in AMBIGUOUS_PRAISE_WORDS
}
_FEELING_PATTERNS = {
    stem: re.compile(rf"\b{re.escape(stem)}\w*", re.IGNORECASE)
    for stem in FEELING_VERBS
}

# Направленность глагола чувств (FEELING_VERBS) — ищется в окне ±_NEARBY_CHARS
# символов вокруг совпадения (примерно 5-6 слов в каждую сторону), не по
# всему сообщению целиком: «тебя»/«её» в другом конце длинного сообщения не
# должны определять направленность конкретного глагола.
_NEARBY_CHARS = 40
_SECOND_PERSON_NEAR_RE = re.compile(r"\b(тебя|тебе|тобой)\b", re.IGNORECASE)
_THIRD_PARTY_OR_OBJECT_RE = re.compile(
    r"\b(её|его|их|"
    r"фильм\w*|музык\w*|сериал\w*|игр\w*|книг\w*|готовит\w*|песн\w*|"
    r"работ\w*|учёб\w*|учеб\w*|погод\w*|природ\w*|кофе|чай)\b",
    re.IGNORECASE,
)


def _has_nearby(text: str, pattern: re.Pattern, match: re.Match) -> bool:
    start = max(0, match.start() - _NEARBY_CHARS)
    end = min(len(text), match.end() + _NEARBY_CHARS)
    return bool(pattern.search(text[start:end]))


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


# ── 5. Тепло ───────────────────────────────────────────────────────────────
# Конфликтную сторону метрики убрали целиком (была источником ложных
# срабатываний на сарказм/смех и сомнительных к показу цитат мата) — считаем
# только тёплую лексику.

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


def _classify_warm(text: str) -> tuple[dict[str, int], bool, str | None]:
    """Один разбор сообщения на все категории сразу — ЕДИНАЯ точка
    классификации, которую используют и подсчёт (warmth), и сбор примеров
    (_collect_warm_examples), и safety-проверка перед добавлением в примеры.
    Раньше баг «пример не содержит тёплых слов» бывал именно от того, что
    текст для примера брался из другого списка/сообщения, чем то, где
    реально нашлось совпадение — общая функция на одном и том же text
    структурно исключает этот класс ошибки.

    Возвращает:
    - {стем: число вхождений} — WARM_LEXICON (вокативные обращения, без
      проверки направленности — она тут не нужна, само обращение и есть
      адресат) + FEELING_VERBS, но ТОЛЬКО те вхождения глагола, у которых
      рядом (см. _NEARBY_CHARS) нашлось «тебя»/«тебе»/«тобой» — остальные
      вхождения глагола либо явно не про собеседника (рядом «её»/«его»/
      «их» или неодушевлённый объект — фильм/музыка/работа и т.п., тогда
      просто не считаются вообще), либо неопределённые (тогда см. ниже)
    - есть ли тёплый эмодзи (точное совпадение символа)
    - стем-кандидат на LLM-проверку, если прямого совпадения/эмодзи не
      нашлось: из AMBIGUOUS_PRAISE_WORDS (общая похвала вроде «молодец») ИЛИ
      из FEELING_VERBS с неопределённой направленностью (глагол есть, но
      рядом нет ни «тебя», ни явно постороннего объекта)."""
    stem_hits: dict[str, int] = {}
    for stem, pattern in _WARM_PATTERNS.items():
        found = pattern.findall(text)
        if found:
            stem_hits[stem] = len(found)

    feeling_ambiguous_stem: str | None = None
    for stem, pattern in _FEELING_PATTERNS.items():
        for m in pattern.finditer(text):
            if _has_nearby(text, _SECOND_PERSON_NEAR_RE, m):
                stem_hits[stem] = stem_hits.get(stem, 0) + 1
            elif _has_nearby(text, _THIRD_PARTY_OR_OBJECT_RE, m):
                continue  # явно не про собеседника — не считаем вообще
            elif feeling_ambiguous_stem is None:
                feeling_ambiguous_stem = stem  # неопределённость — кандидат на LLM

    has_emoji = any(e in text for e in WARM_EMOJI)

    ambiguous_stem = None
    if not stem_hits and not has_emoji:
        for stem, pattern in _AMBIGUOUS_PATTERNS.items():
            if pattern.search(text):
                ambiguous_stem = stem
                break
        if ambiguous_stem is None:
            ambiguous_stem = feeling_ambiguous_stem

    return stem_hits, has_emoji, ambiguous_stem


def _message_is_warm(
    text: str, direction: str, confirmed_ambiguous: set[tuple[str, str]] | None,
) -> bool:
    """(а) прямое совпадение WARM_LEXICON, ИЛИ (б) тёплый эмодзи, ИЛИ
    (в) неоднозначная похвала, но ТОЛЬКО если LLM её подтвердила
    (confirmed_ambiguous содержит (direction, text)). Без подтверждения
    (confirmed_ambiguous=None — кандидаты ещё не проверены LLM, или
    подтверждения не было) неоднозначные слова тёплыми НЕ считаются."""
    stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)
    if stem_hits or has_emoji:
        return True
    if ambiguous_stem and confirmed_ambiguous is not None:
        return (direction, text) in confirmed_ambiguous
    return False


def _collect_warm_examples(
    rows: list[dict], confirmed_ambiguous: set[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    """До 2 самых СВЕЖИХ тёплых сообщений, (direction, текст), отфильтрованы
    через ту же _looks_junky, что и цитаты в features.initiative_axis (не
    пропускает голые ссылки/эмодзи/однобуквенный мусор)."""
    examples: list[tuple[str, str]] = []
    for r in reversed(_sorted_texted(rows)):  # с конца — свежие сообщения первыми
        if len(examples) >= 2:
            break
        text = r["text"]
        if _looks_junky(text):
            continue
        if not _message_is_warm(text, r["direction"], confirmed_ambiguous):
            continue
        # Safety-проверка (не полагаемся только на факт, что _message_is_warm
        # уже True выше) — независимая перепроверка ИМЕННО того текста,
        # который сейчас пойдёт в примеры, прямо перед append. Ловит
        # регрессию, если будущая правка случайно подставит не тот text/r.
        stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)
        confirmed = bool(
            ambiguous_stem and confirmed_ambiguous
            and (r["direction"], text) in confirmed_ambiguous
        )
        if not (stem_hits or has_emoji or confirmed):
            logging.warning(
                "warmth: сообщение прошло отбор, но повторная проверка не "
                "находит совпадения — не беру в примеры: %r", text[:80],
            )
            continue
        examples.append((r["direction"], text.strip()))
    return examples


@dataclass
class WarmthResult:
    warm_n: int
    warm_pct: float
    warm_examples: list[tuple[str, str]]
    weeks: float
    # {"out": Counter(canon -> count), "in": Counter(...)} — occurrence-подсчёт
    # по словам (не по сообщениям), для топ-слов; естественного места в
    # карточке пока нет, просто отдаём числом на будущее.
    occurrences: dict[str, Counter] = field(default_factory=lambda: {"out": Counter(), "in": Counter()})
    # (direction, text, canon) — сообщения с AMBIGUOUS_PRAISE_WORDS, ещё НЕ
    # проверенные LLM. Непусто только при вызове с confirmed_ambiguous=None
    # (первый, «разведочный» проход) — см. main.py-оркестрацию в докстринге
    # warmth() ниже.
    ambiguous_candidates: list[tuple[str, str, str]] = field(default_factory=list)

    def __iter__(self):
        # Обратная совместимость со старым контрактом (warm_n, warm_pct,
        # warm_examples, weeks) = warmth(rows) — main.compute_all распаковывает
        # именно так.
        return iter((self.warm_n, self.warm_pct, self.warm_examples, self.weeks))


def warmth(
    rows: list[dict], confirmed_ambiguous: set[tuple[str, str]] | None = None,
) -> WarmthResult:
    """Считает тёплые сообщения. Два прохода при неоднозначных словах похвалы
    («молодец»/«умница» и т.п. — см. AMBIGUOUS_PRAISE_WORDS в config.py):

    1) warmth(rows) — confirmed_ambiguous не передан. Неоднозначные слова НЕ
       засчитываются, зато собираются в result.ambiguous_candidates.
    2) Если ambiguous_candidates непусты — main.py прогоняет их тексты через
       llm.classify_ambiguous_praise (батч, один вызов LLM на контакт) и
       строит set подтверждённых (direction, text).
    3) warmth(rows, confirmed_ambiguous=тот_set) — финальный, авторитетный
       результат: подтверждённые неоднозначные фразы теперь считаются тёплыми.

    Если ambiguous_candidates после шага 1 пуст — шаг 3 не нужен, результат
    шага 1 уже финальный (экономит LLM-вызов на подавляющем большинстве
    контактов, где таких слов вообще не было).

    % (warm_pct) — как раньше читается main.py напрямую для «лучшая
    совместимость» между контактами, в текст карточки НЕ идёт (доля от всей
    переписки занижает результат — см. compute_all)."""
    msgs = [r for r in rows if r.get("text")]
    total = len(msgs)
    if total == 0:
        return WarmthResult(warm_n=0, warm_pct=0.0, warm_examples=[], weeks=1.0)

    occurrences: dict[str, Counter] = {"out": Counter(), "in": Counter()}
    ambiguous_candidates: list[tuple[str, str, str]] = []
    warm_n = 0

    for r in msgs:
        text = r["text"]
        direction = r["direction"]
        stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)

        for stem, count in stem_hits.items():
            occurrences[direction][_CANON_BY_STEM[stem]] += count

        is_warm = bool(stem_hits) or has_emoji
        if not is_warm and ambiguous_stem:
            if confirmed_ambiguous is not None:
                is_warm = (direction, text) in confirmed_ambiguous
            else:
                ambiguous_candidates.append(
                    (direction, text, _CANON_BY_STEM[ambiguous_stem])
                )

        if is_warm:
            warm_n += 1

    warm_examples = _collect_warm_examples(rows, confirmed_ambiguous)
    weeks = _weeks_span(rows)
    return WarmthResult(
        warm_n=warm_n, warm_pct=warm_n / total, warm_examples=warm_examples,
        weeks=weeks, occurrences=occurrences, ambiguous_candidates=ambiguous_candidates,
    )


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


def compute_all(
    rows: list[dict], confirmed_ambiguous: set[tuple[str, str]] | None = None,
) -> dict[str, dict]:
    """Считает все метрики и сразу форматирует (short, fact) для каждой —
    возвращает {key: {"label", "short", "fact"}}, тот же контракт, что
    ждут main.py/llm.py, но числа внутри fact теперь и абсолютные, и %,
    посчитанные из типизированных функций выше, не строками напрямую.

    confirmed_ambiguous — прокидывается в warmth() как есть (LLM-подтверждённые
    неоднозначные похвалы, см. warmth() докстринг); None на первом,
    «разведочном» проходе — main.py вызывает compute_all дважды только если
    у warmth() нашлись ambiguous_candidates."""
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

    warmth_result = warmth(rows, confirmed_ambiguous=confirmed_ambiguous)
    warm_n, warm_pct = warmth_result.warm_n, warmth_result.warm_pct
    warm_examples, weeks = warmth_result.warm_examples, warmth_result.weeks
    out["warmth_conflict"] = {
        "label": "Тепло",
        "short": f"💚{warm_n}",
        # % оставлен как ЧИСЛО в словаре (не в тексте fact ниже) — main.py
        # читает warmth_pct напрямую для «лучшая совместимость» в «Анализ
        # своего стиля» (сравнение между контактами), а не парсит регуляркой
        # готовый текст. В САМ fact доля от объёма больше не идёт: доля от
        # ВСЕЙ переписки (включая бытовую) занижает результат — 132 тёплых
        # сообщения могут дать 2%, хотя абсолютно это много (см. warmth()).
        "warmth_pct": warm_pct,
        "fact": (
            f"Тёплых сообщений — {warm_n}, в среднем {_fmt_times_per_week(warm_n / weeks)} "
            f"(переписка идёт {_fmt_weeks_span(weeks)}). Частота тут не мера качества "
            "отношений: в новых или бурных парах обычно больше тёплых слов, а в давних "
            "стабильных тепло часто проявляется делами, а не текстом."
        ),
        # Реальные цитаты — main.py дописывает их отдельной строкой поверх
        # fact/interpretation (см. _warmth_examples_suffix), в LLM-промпт
        # интерпретации НЕ идут (там участвует только fact).
        "warm_examples": warm_examples,
        # occurrence-подсчёт по словам (не по сообщениям) — для топ-слов,
        # естественного места в карточке пока нет, просто отдаём числом.
        "warm_occurrences": {
            "out": dict(warmth_result.occurrences["out"]),
            "in": dict(warmth_result.occurrences["in"]),
        },
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
