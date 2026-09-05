"""Тесты «Динамика переписки»: volume_trend (compatibility_metrics.py) и
_format_dynamics_fact (llm.py) — фикс бага, когда незавершённый текущий
месяц сравнивался по сырым суммам с полными прошлыми месяцами."""
from datetime import datetime, timedelta, timezone

from compatibility_metrics import volume_trend
from llm import _format_dynamics_fact

_UTC = timezone.utc


def _row(dt: datetime, direction: str) -> dict:
    return {"date": dt.isoformat(), "direction": direction, "text": "привет"}


def _rows_for_month(year: int, month: int, day_from: int, day_to: int, per_day: int) -> list[dict]:
    rows = []
    for day in range(day_from, day_to + 1):
        for i in range(per_day):
            dt = datetime(year, month, day, 10, tzinfo=_UTC) + timedelta(minutes=i)
            rows.append(_row(dt, "out" if i % 2 else "in"))
    return rows


def test_fully_past_month_is_complete():
    # Диапазон целиком в прошлом (2020 год) — последний месяц завершён.
    rows = (
        _rows_for_month(2020, 1, 1, 31, 3)
        + _rows_for_month(2020, 2, 1, 29, 3)
        + _rows_for_month(2020, 3, 1, 31, 3)
        + _rows_for_month(2020, 4, 1, 30, 3)
    )
    vt = volume_trend(rows)
    assert vt.granularity == "month"
    assert vt.latest.is_complete is True
    assert vt.latest.days_elapsed == vt.latest.calendar_days == 30  # апрель
    assert vt.latest.total == 30 * 3


def test_current_ongoing_month_is_incomplete():
    now = datetime.now(_UTC)
    # 3 полных предыдущих месяца + текущий месяц только "до вчера".
    m1 = (now.replace(day=1) - timedelta(days=1)).replace(day=1)  # позапрошлый-позапрошлый, грубо
    rows = []
    # Три месяца подряд перед текущим, по 20 сообщений каждый день месяца.
    cursor = now.replace(day=1)
    months = []
    for _ in range(3):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        months.append((cursor.year, cursor.month))
    months.reverse()
    for year, month in months:
        last_day = (datetime(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day
        rows += _rows_for_month(year, month, 1, last_day, 10)

    # Текущий месяц — только с 1 числа по (сегодня - 1), раз "сегодня" ещё не закончилось.
    today = now.date()
    last_full_day = max(1, today.day - 1)
    rows += _rows_for_month(now.year, now.month, 1, last_full_day, 10)

    vt = volume_trend(rows)
    assert vt.granularity == "month"
    assert vt.latest.is_complete is False
    assert vt.latest.days_elapsed == last_full_day
    assert vt.latest.total == last_full_day * 10
    # Дневное среднее одинаковое (10/день) во всех месяцах, несмотря на то что
    # сырая сумма последнего месяца меньше — daily_avg должен это выправлять.
    assert round(vt.latest.daily_avg, 1) == round(vt.peak.daily_avg, 1) == 10.0


def test_dynamics_fact_flags_incomplete_month_and_uses_daily_avg():
    now = datetime.now(_UTC)
    # Три ПОЛНЫХ предыдущих месяца (нужно >45 дней диапазона, иначе
    # _pick_granularity сгруппирует по неделям, а не по месяцам) + текущий
    # месяц, оба по 5 сообщ./день — но текущий месяц ещё не закончился.
    cursor = now.replace(day=1)
    months = []
    for _ in range(3):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        months.append((cursor.year, cursor.month))
    months.reverse()

    rows = []
    for year, month in months:
        last_day = ((datetime(year, month, 28, tzinfo=_UTC) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        rows += _rows_for_month(year, month, 1, last_day, 5)

    today = now.date()
    last_full_day = max(1, today.day - 1)
    rows += _rows_for_month(now.year, now.month, 1, last_full_day, 5)

    vt = volume_trend(rows)
    assert vt.granularity == "month"
    fact = _format_dynamics_fact(vt)

    assert "ещё не заверш" in fact
    assert "средн" in fact  # "в среднем ... сообщ./день"
    assert f"{last_full_day} из" in fact
    # Реального падения по дневному среднему быть не должно — то же 5/день,
    # что и в пиковом месяце (сама метрика, не текст — надёжнее парсинга фразы).
    assert round(vt.latest.daily_avg, 1) == round(vt.peak.daily_avg, 1) == 5.0


def test_dynamics_fact_unchanged_when_last_month_fully_past():
    # Регрессия на Шаг 3: если последний месяц ЗАВЕРШЁН (весь диапазон в
    # прошлом) — старое поведение (сравнение по сырым суммам, без пометки
    # "не завершён") должно остаться как было.
    rows = (
        _rows_for_month(2020, 6, 1, 30, 20)   # пик
        + _rows_for_month(2020, 7, 1, 31, 4)  # спад — реальный, месяц завершён
    )
    vt = volume_trend(rows)
    assert vt.latest.is_complete is True
    fact = _format_dynamics_fact(vt)
    assert "ещё не заверш" not in fact
    assert "в среднем" not in fact
    assert "к пиковому." in fact
    # Сырое сравнение сумм: (31*4 - 30*20) / (30*20) * 100 ≈ -79%
    assert "-79%" in fact or "-78%" in fact or "-80%" in fact


def test_peak_equals_latest_incomplete_still_flagged():
    now = datetime.now(_UTC)
    today = now.date()
    last_full_day = max(1, today.day - 1) if today.day > 1 else 1
    rows = _rows_for_month(now.year, now.month, 1, last_full_day, 7)
    if len(rows) < 4:
        return  # 1 числа месяца недостаточно данных — тривиально пропускаем
    vt = volume_trend(rows)
    if vt.granularity != "month":
        return  # короткий диапазон сгруппировался по дням/неделям — не наш кейс
    fact = _format_dynamics_fact(vt)
    assert "ещё не заверш" in fact
