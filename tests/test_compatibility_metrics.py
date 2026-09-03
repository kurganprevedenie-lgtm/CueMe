from datetime import datetime, timedelta

from compatibility_metrics import compute_all, warmth

_BASE = datetime(2026, 1, 1)


def _row(offset_minutes, direction, text):
    return {
        "date": (_BASE + timedelta(minutes=offset_minutes)).isoformat(),
        "direction": direction,
        "text": text,
    }


def test_warmth_words_include_common_compliments():
    rows = [_row(0, "in", "ты молодец, все супер"), _row(1, "in", "умница моя")]
    warm_n, *_ = warmth(rows)
    assert warm_n == 2


def test_prefixed_verb_forms_still_match():
    # Намеренное решение: НЕ анкерить матчинг на границу слова — иначе ломаются
    # частые русские приставочные формы («по-» + стем словаря), где стем НЕ в
    # начале слова, но слово всё равно про тепло («полюблю» = по+люблю).
    rows = [_row(0, "in", "я тебя полюблю ещё сильнее")]
    warm_n, *_ = warmth(rows)
    assert warm_n == 1


def test_examples_filtered_and_freshest_first():
    rows = [
        _row(0, "in", "http://example.com"),  # ссылка с "любов" не бывает, просто мусор-контроль
        _row(1, "in", "я тебя люблю"),
        _row(2, "in", "ты моя зая"),
    ]
    warm_n, warm_pct, warm_ex, weeks = warmth(rows)
    assert warm_n == 2
    assert warm_ex[0] == ("in", "ты моя зая")  # свежее — первое


def test_fact_text_shows_absolute_count_not_percent():
    rows = [_row(i, "in", f"люблю тебя {i}") for i in range(5)]
    metrics = compute_all(rows)
    fact = metrics["warmth_conflict"]["fact"]
    assert "%" not in fact
    assert "Тёплых сообщений — 5" in fact
    assert "раз" in fact  # частота в неделю
    assert metrics["warmth_conflict"]["label"] == "Тепло"


def test_no_conflict_fields_left():
    rows = [_row(0, "in", "бесит это все")]  # раньше матчило конфликт — теперь просто нейтральный текст
    metrics = compute_all(rows)
    wc = metrics["warmth_conflict"]
    assert "conflict_examples" not in wc
    assert wc["short"] == "💚0"
