"""compatibility_metrics.py — детерминированные метрики «Анализа собеседника».
Каждая функция берёт rows (список {"date": ISO-строка, "direction":
"in"/"out", "text": str}, вся история контакта — business + imported).

Возвращает СЫРЫЕ ТИПИЗИРОВАННЫЕ ЧИСЛА (не готовые строки) — форматирование в
текст факта/короткого значения отдельным слоем ниже (_format_*), чтобы числа
считались один раз, а не дублировались между «что посчитали» и «что показали».

Работает на том, что реально доступно через Business API прямо сейчас: text,
date, direction. БЕЗ реакций, БЕЗ длительности голосовых, БЕЗ фото — эти поля
физически не собираются (см. raw_meta в main.py: только length/has_emoji/voice).

Модуль целиком БЕЗ LLM: каждая метрика считается по тексту детерминированно.
Раньше исключением была секция «Тепло» (словарь тёплых слов + LLM-проверка
неоднозначных кандидатов) — убрана целиком, см. блок «5. Тепло» ниже."""
import calendar
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from features import _looks_junky


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


def _as_utc(dt: datetime) -> datetime:
    """Даты в rows бывают и с tz, и без (см. business_messages.date) — для
    сравнения с datetime.now(timezone.utc) (см. volume_trend: определение
    незавершённого периода) наивные считаем уже UTC, тот же принцип, что и
    в main._relative_label."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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


# ── 5. Кто чаще задаёт вопросы ──────────────────────────────────────────────
# На этом месте была секция «Тепло» (словарь тёплых слов + LLM-проверка
# неоднозначных кандидатов). Убрана целиком, НЕ из-за отдельных багов, а
# потому что метод принципиально не работает: словарь не видит контекст, и
# любое совпадение стема засчитывалось теплом независимо от того, к кому
# оно обращено — «обожаю этот фильм», «молодец, успел среагировать», «я
# обожаю её» одинаково попадали в тёплые. Точечные фиксы (границы слова,
# проверка направленности глаголов по соседним словам, LLM-подтверждение
# неоднозначной похвалы) каждый раз ловили свой частный случай и оставляли
# следующий — потому что контекст в принципе не восстанавливается словарём.
# Старый код оставлен закомментированным ниже целиком (вместе со словарями
# в config.py и llm.classify_ambiguous_praise) — на случай, если решим
# вернуться к теме уже не словарным методом.
#
# Вместо неё — детерминированная метрика того же класса, что остальные оси:
# кто чаще задаёт вопросы (см. question_balance ниже).

# Вопрос ищется В ЛЮБОМ месте сообщения, не только в конце: «а ты как? я
# норм» — тоже вопрос. Считаем ПО СООБЩЕНИЯМ (сообщение либо вопрос, либо
# нет), а не по числу «?» — поэтому «ты где???» это один вопрос, а не три,
# без отдельной обработки серий знаков.
_QUESTION_RE = re.compile(r"\?")

# Ниже какого отношения долей считаем, что спрашивают примерно поровну —
# 1.25 (25% разницы) выбран как «заметно на глаз»: при 8% против 9% вывод
# «спрашивает чаще» был бы шумом, а не наблюдением.
_QUESTION_PARITY_RATIO = 1.25


def question_balance(rows: list[dict]) -> tuple[int, int, int, int, list[tuple[str, str]]]:
    """(вопросов автора, вопросов собеседника, всего сообщений автора,
    всего сообщений собеседника, примеры-вопросы).

    Доли считаются вызывающим кодом из этих же чисел (compute_all) — важна
    именно доля, а не абсолют: 120 вопросов на 2000 сообщений и 60 на 300 —
    это «реже» и «чаще», хотя абсолютное число говорит обратное.

    Примеры — (direction, текст сообщения), до 2 штук, самые свежие первыми,
    у того, кто спрашивает чаще по доле; если разница в долях меньше
    _QUESTION_PARITY_RATIO — по одному от каждого, чтобы не создавать
    впечатление перекоса там, где его нет. Мусор отсекается тем же
    _looks_junky, что и цитаты в остальных осях: голый «?», «???» без
    текста и ссылки в примеры не попадают."""
    msgs = [r for r in rows if r.get("text")]
    author_total = sum(1 for r in msgs if r["direction"] == "out")
    contact_total = len(msgs) - author_total

    author_q = sum(
        1 for r in msgs if r["direction"] == "out" and _QUESTION_RE.search(r["text"])
    )
    contact_q = sum(
        1 for r in msgs if r["direction"] == "in" and _QUESTION_RE.search(r["text"])
    )

    author_share = author_q / author_total if author_total else 0.0
    contact_share = contact_q / contact_total if contact_total else 0.0
    examples = _collect_question_examples(rows, author_share, contact_share)
    return author_q, contact_q, author_total, contact_total, examples


def _collect_question_examples(
    rows: list[dict], author_share: float, contact_share: float,
) -> list[tuple[str, str]]:
    """До 2 реальных вопросов, свежие первыми. Кому принадлежат — зависит от
    перекоса долей (см. докстринг question_balance): при заметной разнице
    оба примера у того, кто спрашивает чаще, при сопоставимых долях — по
    одному с каждой стороны."""
    hi, lo = max(author_share, contact_share), min(author_share, contact_share)
    if lo > 0:
        parity = hi / lo < _QUESTION_PARITY_RATIO
    else:
        # Одна сторона не задаёт вопросов вообще — это уже перекос, не паритет
        # (кроме случая, когда вопросов нет ни у кого: примеров всё равно не будет).
        parity = hi == 0
    leader = "out" if author_share >= contact_share else "in"

    if parity:
        # По одному с каждой стороны: сначала лидер (пусть и незначительный),
        # чтобы порядок примеров не выглядел случайным.
        wanted = {leader: 1, ("in" if leader == "out" else "out"): 1}
    else:
        wanted = {leader: 2}

    examples: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in reversed(_sorted_texted(rows)):  # с конца — свежие сообщения первыми
        if len(examples) >= 2:
            break
        direction = r["direction"]
        if wanted.get(direction, 0) <= 0:
            continue
        text = r["text"].strip()
        if not _QUESTION_RE.search(text) or _looks_junky(text):
            continue
        # Один и тот же вопрос дважды («как дела?» человек задаёт регулярно)
        # выглядел бы как недоработка, а не как два примера — дедупим по
        # нормализованному тексту, берём следующий подходящий.
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        wanted[direction] -= 1
        examples.append((direction, text))
    return examples


# # ── 5. Тепло ───────────────────────────────────────────────────────────────
# # Конфликтную сторону метрики убрали целиком (была источником ложных
# # срабатываний на сарказм/смех и сомнительных к показу цитат мата) — считаем
# # только тёплую лексику.
#
# def _weeks_span(rows: list[dict]) -> float:
#     """Сколько недель охватывает переписка (по датам с текстом) — минимум 1,
#     чтобы частота «в неделю» не улетала в небо на паре сообщений за один день."""
#     dated = _sorted_texted(rows)
#     if len(dated) < 2:
#         return 1.0
#     first, last = _parse_dt(dated[0]["date"]), _parse_dt(dated[-1]["date"])
#     if not first or not last:
#         return 1.0
#     return max((last - first).days / 7.0, 1.0)
#
#
# def _classify_warm(text: str) -> tuple[dict[str, int], bool, str | None]:
#     """Один разбор сообщения на все категории сразу — ЕДИНАЯ точка
#     классификации, которую используют и подсчёт (warmth), и сбор примеров
#     (_collect_warm_examples), и safety-проверка перед добавлением в примеры.
#     Раньше баг «пример не содержит тёплых слов» бывал именно от того, что
#     текст для примера брался из другого списка/сообщения, чем то, где
#     реально нашлось совпадение — общая функция на одном и том же text
#     структурно исключает этот класс ошибки.
#
#     Возвращает:
#     - {стем: число вхождений} — WARM_LEXICON (вокативные обращения, без
#       проверки направленности — она тут не нужна, само обращение и есть
#       адресат) + FEELING_VERBS, но ТОЛЬКО те вхождения глагола, у которых
#       рядом (см. _NEARBY_CHARS) нашлось «тебя»/«тебе»/«тобой» — остальные
#       вхождения глагола либо явно не про собеседника (рядом «её»/«его»/
#       «их» или неодушевлённый объект — фильм/музыка/работа и т.п., тогда
#       просто не считаются вообще), либо неопределённые (тогда см. ниже)
#     - есть ли тёплый эмодзи (точное совпадение символа)
#     - стем-кандидат на LLM-проверку, если прямого совпадения/эмодзи не
#       нашлось: из AMBIGUOUS_PRAISE_WORDS (общая похвала вроде «молодец») ИЛИ
#       из FEELING_VERBS с неопределённой направленностью (глагол есть, но
#       рядом нет ни «тебя», ни явно постороннего объекта)."""
#     stem_hits: dict[str, int] = {}
#     for stem, pattern in _WARM_PATTERNS.items():
#         found = pattern.findall(text)
#         if found:
#             stem_hits[stem] = len(found)
#
#     feeling_ambiguous_stem: str | None = None
#     for stem, pattern in _FEELING_PATTERNS.items():
#         for m in pattern.finditer(text):
#             if _has_nearby(text, _SECOND_PERSON_NEAR_RE, m):
#                 stem_hits[stem] = stem_hits.get(stem, 0) + 1
#             elif _has_nearby(text, _THIRD_PARTY_OR_OBJECT_RE, m):
#                 continue  # явно не про собеседника — не считаем вообще
#             elif feeling_ambiguous_stem is None:
#                 feeling_ambiguous_stem = stem  # неопределённость — кандидат на LLM
#
#     has_emoji = any(e in text for e in WARM_EMOJI)
#
#     ambiguous_stem = None
#     if not stem_hits and not has_emoji:
#         for stem, pattern in _AMBIGUOUS_PATTERNS.items():
#             if pattern.search(text):
#                 ambiguous_stem = stem
#                 break
#         if ambiguous_stem is None:
#             ambiguous_stem = feeling_ambiguous_stem
#
#     return stem_hits, has_emoji, ambiguous_stem
#
#
# def _message_is_warm(
#     text: str, direction: str, confirmed_ambiguous: set[tuple[str, str]] | None,
# ) -> bool:
#     """(а) прямое совпадение WARM_LEXICON, ИЛИ (б) тёплый эмодзи, ИЛИ
#     (в) неоднозначная похвала, но ТОЛЬКО если LLM её подтвердила
#     (confirmed_ambiguous содержит (direction, text)). Без подтверждения
#     (confirmed_ambiguous=None — кандидаты ещё не проверены LLM, или
#     подтверждения не было) неоднозначные слова тёплыми НЕ считаются."""
#     stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)
#     if stem_hits or has_emoji:
#         return True
#     if ambiguous_stem and confirmed_ambiguous is not None:
#         return (direction, text) in confirmed_ambiguous
#     return False
#
#
# def _collect_warm_examples(
#     rows: list[dict], confirmed_ambiguous: set[tuple[str, str]] | None,
# ) -> list[tuple[str, str]]:
#     """До 2 самых СВЕЖИХ тёплых сообщений, (direction, текст), отфильтрованы
#     через ту же _looks_junky, что и цитаты в features.initiative_axis (не
#     пропускает голые ссылки/эмодзи/однобуквенный мусор)."""
#     examples: list[tuple[str, str]] = []
#     for r in reversed(_sorted_texted(rows)):  # с конца — свежие сообщения первыми
#         if len(examples) >= 2:
#             break
#         text = r["text"]
#         if _looks_junky(text):
#             continue
#         if not _message_is_warm(text, r["direction"], confirmed_ambiguous):
#             continue
#         # Safety-проверка (не полагаемся только на факт, что _message_is_warm
#         # уже True выше) — независимая перепроверка ИМЕННО того текста,
#         # который сейчас пойдёт в примеры, прямо перед append. Ловит
#         # регрессию, если будущая правка случайно подставит не тот text/r.
#         stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)
#         confirmed = bool(
#             ambiguous_stem and confirmed_ambiguous
#             and (r["direction"], text) in confirmed_ambiguous
#         )
#         if not (stem_hits or has_emoji or confirmed):
#             logging.warning(
#                 "warmth: сообщение прошло отбор, но повторная проверка не "
#                 "находит совпадения — не беру в примеры: %r", text[:80],
#             )
#             continue
#         examples.append((r["direction"], text.strip()))
#     return examples
#
#
# @dataclass
# class WarmthResult:
#     warm_n: int
#     warm_pct: float
#     warm_examples: list[tuple[str, str]]
#     weeks: float
#     # {"out": Counter(canon -> count), "in": Counter(...)} — occurrence-подсчёт
#     # по словам (не по сообщениям), для топ-слов; естественного места в
#     # карточке пока нет, просто отдаём числом на будущее.
#     occurrences: dict[str, Counter] = field(default_factory=lambda: {"out": Counter(), "in": Counter()})
#     # (direction, text, canon) — сообщения с AMBIGUOUS_PRAISE_WORDS, ещё НЕ
#     # проверенные LLM. Непусто только при вызове с confirmed_ambiguous=None
#     # (первый, «разведочный» проход) — см. main.py-оркестрацию в докстринге
#     # warmth() ниже.
#     ambiguous_candidates: list[tuple[str, str, str]] = field(default_factory=list)
#
#     def __iter__(self):
#         # Обратная совместимость со старым контрактом (warm_n, warm_pct,
#         # warm_examples, weeks) = warmth(rows) — main.compute_all распаковывает
#         # именно так.
#         return iter((self.warm_n, self.warm_pct, self.warm_examples, self.weeks))
#
#
# def warmth(
#     rows: list[dict], confirmed_ambiguous: set[tuple[str, str]] | None = None,
# ) -> WarmthResult:
#     """Считает тёплые сообщения. Два прохода при неоднозначных словах похвалы
#     («молодец»/«умница» и т.п. — см. AMBIGUOUS_PRAISE_WORDS в config.py):
#
#     1) warmth(rows) — confirmed_ambiguous не передан. Неоднозначные слова НЕ
#        засчитываются, зато собираются в result.ambiguous_candidates.
#     2) Если ambiguous_candidates непусты — main.py прогоняет их тексты через
#        llm.classify_ambiguous_praise (батч, один вызов LLM на контакт) и
#        строит set подтверждённых (direction, text).
#     3) warmth(rows, confirmed_ambiguous=тот_set) — финальный, авторитетный
#        результат: подтверждённые неоднозначные фразы теперь считаются тёплыми.
#
#     Если ambiguous_candidates после шага 1 пуст — шаг 3 не нужен, результат
#     шага 1 уже финальный (экономит LLM-вызов на подавляющем большинстве
#     контактов, где таких слов вообще не было).
#
#     % (warm_pct) — как раньше читается main.py напрямую для «лучшая
#     совместимость» между контактами, в текст карточки НЕ идёт (доля от всей
#     переписки занижает результат — см. compute_all)."""
#     msgs = [r for r in rows if r.get("text")]
#     total = len(msgs)
#     if total == 0:
#         return WarmthResult(warm_n=0, warm_pct=0.0, warm_examples=[], weeks=1.0)
#
#     occurrences: dict[str, Counter] = {"out": Counter(), "in": Counter()}
#     ambiguous_candidates: list[tuple[str, str, str]] = []
#     warm_n = 0
#
#     for r in msgs:
#         text = r["text"]
#         direction = r["direction"]
#         stem_hits, has_emoji, ambiguous_stem = _classify_warm(text)
#
#         for stem, count in stem_hits.items():
#             occurrences[direction][_CANON_BY_STEM[stem]] += count
#
#         is_warm = bool(stem_hits) or has_emoji
#         if not is_warm and ambiguous_stem:
#             if confirmed_ambiguous is not None:
#                 is_warm = (direction, text) in confirmed_ambiguous
#             else:
#                 ambiguous_candidates.append(
#                     (direction, text, _CANON_BY_STEM[ambiguous_stem])
#                 )
#
#         if is_warm:
#             warm_n += 1
#
#     warm_examples = _collect_warm_examples(rows, confirmed_ambiguous)
#     weeks = _weeks_span(rows)
#     return WarmthResult(
#         warm_n=warm_n, warm_pct=warm_n / total, warm_examples=warm_examples,
#         weeks=weeks, occurrences=occurrences, ambiguous_candidates=ambiguous_candidates,
#     )
#


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
    # Незавершённый период (месяц/неделя/день, который на момент подсчёта ещё
    # идёт в реальном календаре — см. volume_trend) занижает total чисто
    # механически: в нём физически не могло накопиться столько же сообщений,
    # сколько в уже закрытом периоде, даже без изменения темпа общения.
    # is_complete=True для всех периодов, кроме, возможно, самого последнего.
    is_complete: bool = True
    # Сколько дней ФАКТИЧЕСКИ отражено в total: для завершённого периода —
    # календарная длина периода целиком; для незавершённого — от начала
    # периода до даты ПОСЛЕДНЕГО сообщения в нём (не «до сегодня» — иначе
    # метрика зависела бы от момента запуска подсчёта, а не только от rows,
    # см. докстринг модуля). Основа для daily_avg ниже.
    days_elapsed: int = 1
    # Календарная длина периода целиком (30 для сентября, 7 для недели) —
    # для подписи «N дней из M, ещё не завершён» под таблицей; не влияет на
    # daily_avg, только на отображение.
    calendar_days: int = 1

    @property
    def total(self) -> int:
        return self.n_author + self.n_contact

    @property
    def daily_avg(self) -> float:
        """Среднее сообщений в день — то, что реально сравнимо между
        завершённым и незавершённым периодом (в отличие от total)."""
        return self.total / self.days_elapsed if self.days_elapsed else 0.0


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


def _period_bounds(dt: datetime, granularity: str) -> tuple[datetime, datetime]:
    """(начало периода, конец периода) — оба началом/концом суток, тот же
    tzinfo, что у dt. Конец — последняя секунда последнего календарного дня
    периода (для месяца — реальное число дней в конкретном месяце, через
    calendar.monthrange, не «30 дней всегда»)."""
    if granularity == "month":
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        end = dt.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    elif granularity == "week":
        start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def volume_trend(rows: list[dict]) -> VolumeTrend:
    """Динамика объёма по периодам (адаптивная группировка) + отдельно пиковый
    и последний период — для сравнения «было/стало» в тексте вывода.

    Незавершённость определяется ТОЛЬКО у последнего периода (он один может
    ещё идти на момент подсчёта карточки) — сравнивается календарный конец
    периода с datetime.now(): если период кончится в будущем — он физически
    не мог накопить столько же сообщений, сколько уже закрытые периоды
    (см. Period.is_complete/days_elapsed/daily_avg выше). Единственное место
    в модуле, где участвует datetime.now(), а не только rows — иначе саму
    незавершённость (по определению зависящую от «сейчас ли ещё идёт
    календарный месяц») в принципе не определить."""
    msgs = _sorted_texted(rows)
    if len(msgs) < 4:
        return VolumeTrend(granularity="day")

    first_dt, last_dt = _parse_dt(msgs[0]["date"]), _parse_dt(msgs[-1]["date"])
    days = max(1, (last_dt - first_dt).days) if first_dt and last_dt else 1
    granularity = _pick_granularity(days)

    buckets: dict[str, Period] = {}
    order: list[str] = []
    bucket_bounds: dict[str, tuple[datetime, datetime]] = {}
    bucket_last_dt: dict[str, datetime] = {}
    for r in msgs:
        dt = _parse_dt(r["date"])
        if not dt:
            continue
        key, label = _period_key(dt, granularity)
        if key not in buckets:
            buckets[key] = Period(label=label, n_author=0, n_contact=0)
            order.append(key)
            bucket_bounds[key] = _period_bounds(dt, granularity)
        if r["direction"] == "out":
            buckets[key].n_author += 1
        else:
            buckets[key].n_contact += 1
        prev_last = bucket_last_dt.get(key)
        if prev_last is None or dt > prev_last:
            bucket_last_dt[key] = dt

    now = datetime.now(timezone.utc)
    for key in order:
        p = buckets[key]
        period_start, period_end = bucket_bounds[key]
        p.calendar_days = (period_end.date() - period_start.date()).days + 1
        if _as_utc(period_end) < now:
            p.is_complete = True
            p.days_elapsed = p.calendar_days
        else:
            p.is_complete = False
            last_with_data = bucket_last_dt[key]
            p.days_elapsed = max(1, (last_with_data.date() - period_start.date()).days + 1)

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


def _fmt_ratio_times(ratio: float) -> str:
    """«в 3 раза» / «в 5 раз» / «в 2.7 раза» — целое склоняется по числу,
    дробное всегда «раза» (та же норма, что была у _fmt_times_per_week)."""
    if abs(ratio - round(ratio)) < 0.05:
        n = round(ratio)
        return f"в {n} {_ru_count_word(n, 'раз', 'раза', 'раз')}"
    return f"в {ratio:.1f} раза"


def _questions_fact(
    a_q: int, c_q: int, a_total: int, c_total: int, a_share: float, c_share: float,
) -> str:
    """Одно предложение в стиле остальных осей: кто спрашивает чаще и
    насколько. Сравниваем ДОЛИ, а не абсолютные числа — при разном объёме
    сообщений абсолют вводит в заблуждение (120 вопросов на 2000 сообщений
    реже, чем 60 на 300)."""
    author_own = f"{a_q} из {a_total} твоих сообщений ({a_share:.0%})"
    contact_own = f"{c_q} из {c_total} сообщений собеседника ({c_share:.0%})"
    author_leads = a_share >= c_share

    hi, lo = max(a_share, c_share), min(a_share, c_share)
    if lo == 0:
        if author_leads:
            return f"Вопросы задаёшь только ты: {author_own}, у собеседника — ни одного."
        return f"Вопросы задаёт только собеседник: {contact_own}, у тебя — ни одного."

    if hi / lo < _QUESTION_PARITY_RATIO:
        return f"Вопросы задаёте примерно поровну: {author_own}, {contact_own}."

    times = _fmt_ratio_times(hi / lo)
    if author_leads:
        return f"Вопросы чаще задаёшь ты: {author_own} против {contact_own} — {times} чаще."
    return f"Вопросы чаще задаёт собеседник: {contact_own} против {author_own} — {times} чаще."


# _fmt_times_per_week / _fmt_weeks_span обслуживали ТОЛЬКО текст убранной
# секции «Тепло» («N тёплых сообщений, в среднем X раз в неделю, переписка
# идёт Y недель») — вместе с ней стали не нужны. Оставлены закомментированными
# на случай, если понадобится формат «частота в неделю» другой метрике.
# def _fmt_times_per_week(rate: float) -> str:
#     """«5 раз в неделю» / «1.5 раза в неделю» — целое склоняется по числу,
#     дробное всегда «раза» (стандартная русская норма для нецелых)."""
#     if abs(rate - round(rate)) < 0.05:
#         n = round(rate)
#         word = _ru_count_word(n, "раз", "раза", "раз")
#         return f"{n} {word} в неделю"
#     return f"{rate:.1f} раза в неделю"
#
#
# def _fmt_weeks_span(weeks: float) -> str:
#     """«идёт {фраза}» — accusative для длительности: «идёт неделю» / «идёт
#     3 недели» / «идёт 25 недель»."""
#     if weeks < 1.5:
#         return "меньше недели"
#     n = round(weeks)
#     word = _ru_count_word(n, "неделю", "недели", "недель")
#     return f"{n} {word}"


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

    a_q, c_q, a_total, c_total, q_examples = question_balance(rows)
    a_share = a_q / a_total if a_total else 0.0
    c_share = c_q / c_total if c_total else 0.0
    if not a_q and not c_q:
        out["questions"] = {
            "label": "Кто чаще задаёт вопросы", "short": "—",
            "fact": "Вопросов в переписке нет ни с одной стороны.",
        }
    else:
        out["questions"] = {
            "label": "Кто чаще задаёт вопросы",
            "short": f"{c_share:.0%}/{a_share:.0%} собеседник/ты",
            "fact": _questions_fact(a_q, c_q, a_total, c_total, a_share, c_share),
            # Реальные цитаты — main.py дописывает их отдельной строкой поверх
            # fact/interpretation (см. _quote_examples_suffix), в LLM-промпт
            # интерпретации НЕ идут (там участвует только fact).
            "examples": q_examples,
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
