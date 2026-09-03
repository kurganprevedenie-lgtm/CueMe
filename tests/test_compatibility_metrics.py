from datetime import datetime, timedelta

from compatibility_metrics import compute_all, warmth_conflict

_BASE = datetime(2026, 1, 1)


def _row(offset_minutes, direction, text):
    return {
        "date": (_BASE + timedelta(minutes=offset_minutes)).isoformat(),
        "direction": direction,
        "text": text,
    }


def test_laugh_spam_excluded_from_conflict_count_and_examples():
    rows = [
        _row(0, "in", "ХПХПХПХПХПХПХПХ Я ДУМАЛА ТЫ РАССТРОИЛСЯ"),  # смех, не конфликт
        _row(1, "in", "заебали"),  # настоящий конфликт
    ]
    warm_n, warm_pct, conf_n, conf_pct, warm_ex, conf_ex, weeks = warmth_conflict(rows)
    assert conf_n == 1  # смеховое сообщение не засчитано
    texts = [t for _, t in conf_ex]
    assert not any("РАССТРОИЛСЯ" in t for t in texts)
    assert any("заебали" in t for t in texts)


def test_conflict_examples_prioritize_second_person_address():
    rows = [
        _row(0, "in", "бесит это все"),  # конфликт, но без адресата
        _row(1, "out", "ты заебал на меня бочку катить"),  # конфликт, адресован собеседнику
    ]
    _, _, conf_n, _, _, conf_ex, _ = warmth_conflict(rows)
    assert conf_n == 2
    assert conf_ex[0][1] == "ты заебал на меня бочку катить"


def test_warmth_words_include_common_compliments():
    rows = [_row(0, "in", "ты молодец, все супер"), _row(1, "in", "умница моя")]
    warm_n, *_ = warmth_conflict(rows)
    assert warm_n == 2


def test_prefixed_verb_forms_still_match():
    # Намеренное решение: НЕ анкерить матчинг на границу слова — иначе ломаются
    # частые русские приставочные формы («по-»/«раз-»/«об-» + стем словаря),
    # где стем НЕ в начале слова, но слово всё равно про тепло/конфликт.
    rows = [
        _row(0, "in", "мы поссорились вчера"),      # по+ссор(ились)
        _row(1, "in", "он на тебя разозлится"),      # раз+злит(ся)
        _row(2, "in", "я тебя полюблю ещё сильнее"),  # по+люблю
    ]
    warm_n, _, conf_n, *_ = warmth_conflict(rows)
    assert conf_n == 2   # поссорились, разозлится
    assert warm_n == 1   # полюблю


def test_fact_text_shows_absolute_count_not_percent():
    rows = (
        [_row(i, "in", f"люблю тебя {i}") for i in range(5)]
        + [_row(1000 + i, "in", f"бесит {i}") for i in range(2)]
    )
    metrics = compute_all(rows)
    fact = metrics["warmth_conflict"]["fact"]
    assert "%" not in fact
    assert "Тёплых сообщений — 5" in fact
    assert "Конфликтных — 2" in fact
    assert "раз" in fact  # частота в неделю
