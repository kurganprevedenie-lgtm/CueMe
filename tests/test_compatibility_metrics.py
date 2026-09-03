from datetime import datetime, timedelta

from compatibility_metrics import _classify_warm, compute_all, warmth

_BASE = datetime(2026, 1, 1)


def _row(offset_minutes, direction, text):
    return {
        "date": (_BASE + timedelta(minutes=offset_minutes)).isoformat(),
        "direction": direction,
        "text": text,
    }


def test_direct_lexicon_words_count_without_llm():
    rows = [_row(0, "in", "ты моя зая"), _row(1, "in", "я тебя очень люблю")]
    result = warmth(rows)
    assert result.warm_n == 2
    assert result.ambiguous_candidates == []


def test_ambiguous_praise_not_counted_without_confirmation():
    # "молодец"/"умница" — неоднозначная похвала, БЕЗ confirmed_ambiguous не
    # засчитывается тёплой, но попадает в кандидатов на LLM-проверку.
    rows = [_row(0, "in", "ты молодец, все супер"), _row(1, "in", "умница моя")]
    result = warmth(rows)
    assert result.warm_n == 0
    assert len(result.ambiguous_candidates) == 2
    assert {c[2] for c in result.ambiguous_candidates} == {"молодец", "умница"}


def test_ambiguous_praise_counted_when_confirmed():
    rows = [_row(0, "in", "ты молодец, все супер"), _row(1, "in", "умница моя")]
    confirmed = {("in", "ты молодец, все супер")}  # только первое LLM подтвердила
    result = warmth(rows, confirmed_ambiguous=confirmed)
    assert result.warm_n == 1
    texts = [t for _, t in result.warm_examples]
    assert "ты молодец, все супер" in texts
    assert "умница моя" not in texts


def test_warm_emoji_counts_as_warm():
    rows = [_row(0, "in", "жду встречи ❤️"), _row(1, "in", "обычное сообщение без слов")]
    result = warmth(rows)
    assert result.warm_n == 1
    assert result.warm_examples[0][1] == "жду встречи ❤️"


def test_word_boundary_matching_no_substring_false_positive():
    # Реальные примеры прошлого бага: административный/новостной текст без
    # единого тёплого слова/стема не должен матчиться вообще.
    rows = [
        _row(0, "in", "Напоминание для студентов, обучающихся по договору, "
                      "об оплате за следующий семестр."),
        _row(1, "in", "Всеобщие выборы в Великобритании назначены на четверг."),
    ]
    result = warmth(rows)
    assert result.warm_n == 0
    assert result.warm_examples == []
    assert result.ambiguous_candidates == []


def test_word_boundary_left_side_only_matches_inflected_forms():
    # "любим" — стем, должен матчить "любимая"/"любимый" (словоформа), но
    # НЕ ловить "полюблю" (стем не в начале слова — по+люблю).
    rows = [
        _row(0, "in", "моя любимая, привет"),
        _row(1, "in", "я тебя полюблю ещё сильнее"),
    ]
    result = warmth(rows)
    assert result.warm_n == 1
    assert result.warm_examples[0][1] == "моя любимая, привет"


def test_examples_are_from_the_same_message_that_matched():
    # Регрессия ровно на прошлый баг: если бы примеры брались из другого
    # списка/сообщения, чем то, где найдено совпадение, эта проверка бы упала.
    rows = [
        _row(0, "in", "просто нейтральное сообщение"),
        _row(1, "in", "я тебя люблю"),
        _row(2, "in", "ты моя зая"),
    ]
    result = warmth(rows)
    assert result.warm_n == 2
    for direction, text in result.warm_examples:
        stem_hits, has_emoji, _ = _classify_warm(text)
        assert stem_hits or has_emoji


def test_examples_filtered_junk_and_freshest_first():
    rows = [
        _row(0, "in", "http://example.com"),  # ссылка — мусор-контроль
        _row(1, "in", "я тебя люблю"),
        _row(2, "in", "ты моя зая"),
    ]
    result = warmth(rows)
    assert result.warm_n == 2
    assert result.warm_examples[0] == ("in", "ты моя зая")  # свежее — первое


def test_occurrence_count_separate_from_message_flag():
    # "люблю тебя люблю тебя люблю тебя" — ОДНО тёплое сообщение (флаг), но
    # 3 occurrence (у каждого вхождения "тебя" рядом — направленность на
    # собеседника подтверждена без LLM).
    rows = [_row(0, "out", "люблю тебя люблю тебя люблю тебя")]
    result = warmth(rows)
    assert result.warm_n == 1
    assert result.occurrences["out"]["люблю"] == 3


def test_fact_text_shows_absolute_count_not_percent():
    rows = [_row(i, "in", f"люблю тебя {i}") for i in range(5)]
    metrics = compute_all(rows)
    fact = metrics["warmth_conflict"]["fact"]
    assert "%" not in fact
    assert "Тёплых сообщений — 5" in fact
    assert "раз" in fact  # частота в неделю
    assert metrics["warmth_conflict"]["label"] == "Тепло"
    assert metrics["warmth_conflict"]["warm_occurrences"]["in"]["люблю"] == 5


def test_no_conflict_fields_left():
    rows = [_row(0, "in", "бесит это все")]  # раньше матчило конфликт — теперь просто нейтральный текст
    metrics = compute_all(rows)
    wc = metrics["warmth_conflict"]
    assert "conflict_examples" not in wc
    assert wc["short"] == "💚0"


def test_feeling_verb_with_second_person_counts_without_llm():
    rows = [_row(0, "in", "скучаю по тебе очень"), _row(1, "in", "я тебя обожаю")]
    result = warmth(rows)
    assert result.warm_n == 2
    assert result.ambiguous_candidates == []


def test_feeling_verb_toward_object_not_counted_and_not_sent_to_llm():
    # Реальный кейс из бага: "обожаю" направлено на фильм — не на собеседника.
    # Явно посторонний объект рядом (фильм) — исключается сразу, LLM не зовём.
    rows = [_row(0, "out", "бля как же я обожаю этот фильм")]
    result = warmth(rows)
    assert result.warm_n == 0
    assert result.ambiguous_candidates == []
    assert result.warm_examples == []


def test_feeling_verb_toward_third_person_not_counted_and_not_sent_to_llm():
    # Реальный кейс из бага: "я обожаю её" — третье лицо, не собеседник.
    rows = [_row(0, "out", "я обожаю её")]
    result = warmth(rows)
    assert result.warm_n == 0
    assert result.ambiguous_candidates == []


def test_feeling_verb_undirected_goes_to_llm_candidates():
    # Глагол есть, но ни "тебя", ни явно постороннего объекта рядом нет —
    # неопределённость, кандидат на ту же LLM-проверку, что и AMBIGUOUS_PRAISE_WORDS.
    rows = [_row(0, "in", "обожаю просто")]
    result = warmth(rows)
    assert result.warm_n == 0
    assert len(result.ambiguous_candidates) == 1
    assert result.ambiguous_candidates[0][2] == "обожаю"


def test_feeling_verb_confirmed_by_llm_counts():
    rows = [_row(0, "in", "обожаю просто")]
    confirmed = {("in", "обожаю просто")}
    result = warmth(rows, confirmed_ambiguous=confirmed)
    assert result.warm_n == 1


def test_compute_all_accepts_confirmed_ambiguous():
    rows = [_row(0, "in", "ты молодец, все супер")]
    metrics_plain = compute_all(rows)
    assert metrics_plain["warmth_conflict"]["short"] == "💚0"

    metrics_confirmed = compute_all(rows, confirmed_ambiguous={("in", "ты молодец, все супер")})
    assert metrics_confirmed["warmth_conflict"]["short"] == "💚1"
