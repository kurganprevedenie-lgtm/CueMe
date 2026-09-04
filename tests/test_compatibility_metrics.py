"""Тесты секции «Кто чаще задаёт вопросы» (compatibility_metrics.question_balance).

Заменила секцию «Тепло» — её тесты удалены вместе с метрикой (словарный метод
не видел контекст, см. комментарий в compatibility_metrics, блок «5»).
"""
from datetime import datetime, timedelta

from compatibility_metrics import compute_all, question_balance

_BASE = datetime(2026, 1, 1)


def _row(offset_minutes, direction, text):
    return {
        "date": (_BASE + timedelta(minutes=offset_minutes)).isoformat(),
        "direction": direction,
        "text": text,
    }


def test_counts_questions_per_side():
    rows = [
        _row(0, "out", "привет"),
        _row(1, "out", "как ты?"),
        _row(2, "in", "норм"),
        _row(3, "in", "а ты где?"),
        _row(4, "in", "когда увидимся?"),
    ]
    a_q, c_q, a_total, c_total, _ = question_balance(rows)
    assert (a_q, a_total) == (1, 2)
    assert (c_q, c_total) == (2, 3)


def test_question_not_only_at_the_end():
    # «а ты как? я норм» — вопрос, хотя «?» в середине сообщения.
    rows = [_row(0, "out", "а ты как? я норм")]
    a_q, *_ = question_balance(rows)
    assert a_q == 1


def test_repeated_question_marks_are_one_question():
    # Считаем ПО СООБЩЕНИЯМ, поэтому «ты где???» — один вопрос, а не три.
    rows = [_row(0, "out", "ты где???"), _row(1, "out", "ага")]
    a_q, _, a_total, *_ = question_balance(rows)
    assert (a_q, a_total) == (1, 2)


def test_fact_compares_shares_not_absolute_numbers():
    # У автора вопросов больше в абсолюте (3 против 2), но доля ниже
    # (3/10 = 30% против 2/4 = 50%) — вывод должен быть про собеседника.
    rows = (
        [_row(i, "out", "вопрос?" if i < 3 else "текст") for i in range(10)]
        + [_row(100 + i, "in", "вопрос?" if i < 2 else "текст") for i in range(4)]
    )
    fact = compute_all(rows)["questions"]["fact"]
    assert fact.startswith("Вопросы чаще задаёт собеседник")
    assert "30%" in fact and "50%" in fact


def test_fact_reports_parity_when_shares_are_close():
    rows = (
        [_row(i, "out", "вопрос?" if i < 5 else "текст") for i in range(10)]
        + [_row(100 + i, "in", "вопрос?" if i < 5 else "текст") for i in range(10)]
    )
    fact = compute_all(rows)["questions"]["fact"]
    assert "примерно поровну" in fact


def test_fact_when_one_side_never_asks():
    rows = [_row(0, "out", "как дела?"), _row(1, "in", "норм"), _row(2, "in", "ок")]
    fact = compute_all(rows)["questions"]["fact"]
    assert fact.startswith("Вопросы задаёшь только ты")
    assert "ни одного" in fact


def test_no_questions_at_all():
    rows = [_row(0, "out", "привет"), _row(1, "in", "здарова")]
    metrics = compute_all(rows)["questions"]
    assert metrics["short"] == "—"
    assert "нет ни с одной стороны" in metrics["fact"]
    assert "examples" not in metrics


def test_examples_come_from_the_leader_and_are_real_questions():
    rows = (
        [_row(i, "out", "текст") for i in range(10)]
        + [_row(100, "in", "ты сегодня свободна?")]
        + [_row(101, "in", "во сколько встречаемся?")]
    )
    *_, examples = question_balance(rows)
    assert len(examples) == 2
    assert all(direction == "in" for direction, _ in examples)
    assert all("?" in text for _, text in examples)
    # Свежие первыми
    assert examples[0][1] == "во сколько встречаемся?"


def test_examples_one_from_each_side_when_shares_are_close():
    rows = (
        [_row(i, "out", "твой вопрос?" if i < 5 else "текст") for i in range(10)]
        + [_row(100 + i, "in", "мой вопрос?" if i < 5 else "текст") for i in range(10)]
    )
    *_, examples = question_balance(rows)
    assert {direction for direction, _ in examples} == {"out", "in"}


def test_junky_questions_are_not_quoted():
    # Голый «?», «???» и ссылка со знаком вопроса — не цитаты (_looks_junky).
    rows = [
        _row(0, "in", "?"),
        _row(1, "in", "???"),
        _row(2, "in", "https://example.com/?a=1"),
        _row(3, "in", "ты придёшь завтра?"),
    ]
    *_, examples = question_balance(rows)
    assert examples == [("in", "ты придёшь завтра?")]


def test_same_question_is_not_quoted_twice():
    # «как дела?» человек задаёт регулярно — два одинаковых примера выглядели
    # бы недоработкой, берём следующий по свежести отличающийся вопрос.
    rows = [
        _row(0, "in", "ты придёшь?"),
        _row(1, "in", "как дела?"),
        _row(2, "in", "Как дела?"),
        _row(3, "out", "норм"),
    ]
    *_, examples = question_balance(rows)
    texts = [t for _, t in examples]
    assert len(texts) == len(set(t.lower() for t in texts))


def test_long_question_is_truncated_in_card_suffix():
    import main

    long_q = "а вот скажи мне пожалуйста " * 5 + "что ты думаешь?"
    suffix = main._quote_examples_suffix({"examples": [("in", long_q)]})
    assert "…" in suffix
    assert len(suffix) < len(long_q) + 40


def test_card_section_replaces_warmth():
    rows = [_row(0, "out", "как ты?"), _row(1, "in", "норм")]
    metrics = compute_all(rows)
    assert "warmth_conflict" not in metrics
    assert metrics["questions"]["label"] == "Кто чаще задаёт вопросы"
    # Порядок секций: новая стоит там же, где стояло «Тепло» —
    # между «Долгие паузы» и «Совпадение по времени».
    keys = [k for k in metrics if not k.startswith("_")]
    assert keys.index("questions") == keys.index("long_pauses") + 1
    assert keys.index("circadian") == keys.index("questions") + 1
