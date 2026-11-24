import asyncio

import pytest

import main  # импортирует обработчики в штатном порядке
import llm
from handlers import reply


def test_reply_context_uses_only_authors_voice_and_preceding_history(monkeypatch):
    history = [
        {"date": f"2026-11-24T12:{i:02}:00+00:00", "direction": "out", "text": "ну погнали"}
        for i in range(9)
    ] + [
        {"date": f"2026-11-24T12:{i:02}:00+00:00", "direction": "in", "text": "Ты уверен?."}
        for i in range(9, 18)
    ] + [
        {"date": "2026-11-24T12:18:00+00:00", "direction": "in", "text": "ты серьёзно?"},
        {"date": "2026-11-24T12:19:00+00:00", "direction": "out", "text": "будущее"},
    ]
    monkeypatch.setattr(reply, "get_all_dated_messages", lambda *_: history)
    ctx = reply._adaptive_contact_context("42", 5, "ты серьёзно?")
    assert "со строчной" in ctx["author_voice"]
    assert "опускает точку" in ctx["author_voice"]
    assert len(ctx["recent_dialogue"].splitlines()) == 6
    assert "будущее" not in ctx["recent_dialogue"]
    assert "ты серьёзно?" not in ctx["recent_dialogue"]
    assert ctx["winning"] is None


def test_adaptive_reply_and_rollback_keep_distinct_prompts_and_cache(monkeypatch):
    prompts = []

    async def ask(prompt, **kwargs):
        prompts.append(prompt)
        return "\n".join(
            f"===ВАРИАНТ===\nНАЗВАНИЕ: {name}\nТЕКСТ: хорошо"
            for name in ("Поддержать", "Обыграть", "Уточнить")
        )

    monkeypatch.setattr(llm, "_ask", ask)
    kwargs = {"author_voice": "начинай со строчной", "recent_dialogue": "Автор: пусть будет так"}
    monkeypatch.setattr(llm, "ADAPTIVE_REPLY_PROMPT", True)
    monkeypatch.setattr(reply, "ADAPTIVE_REPLY_PROMPT", True)
    variants = asyncio.run(llm.suggest_reply_variants("ты серьёзно?", "карточка", "контекст", **kwargs))
    assert [name for name, _ in variants] == ["Поддержать", "Обыграть", "Уточнить"]
    assert "ДЕФОЛТНЫЙ НАБОР" not in prompts[0]
    assert "начинай со строчной" in prompts[0]
    assert "пусть будет так" in prompts[0]
    assert prompts[0].count("===ВАРИАНТ===\nНАЗВАНИЕ:") == 3
    adaptive_key = reply._reply_cache_extra("male", kwargs)
    assert adaptive_key != reply._reply_cache_extra("male", {})

    monkeypatch.setattr(llm, "ADAPTIVE_REPLY_PROMPT", False)
    monkeypatch.setattr(reply, "ADAPTIVE_REPLY_PROMPT", False)
    asyncio.run(llm.suggest_reply_variants("ты серьёзно?", "карточка", "контекст", **kwargs))
    assert "ДЕФОЛТНЫЙ НАБОР" in prompts[1]
    assert "пусть будет так" not in prompts[1]
    assert reply._reply_cache_extra("male", kwargs) == "male"


def test_partial_adaptive_result_does_not_reach_success_path(monkeypatch):
    async def ask(*args, **kwargs):
        return "===ВАРИАНТ===\nНАЗВАНИЕ: Уточнить\nТЕКСТ: хорошо"

    monkeypatch.setattr(llm, "ADAPTIVE_REPLY_PROMPT", True)
    monkeypatch.setattr(llm, "_ask", ask)
    with pytest.raises(llm.ProviderError, match="1 из 3"):
        asyncio.run(llm.suggest_reply_variants("привет", "", ""))


def test_live_dialogue_still_returns_running_notes(monkeypatch):
    async def ask(prompt, **kwargs):
        assert kwargs["tag"] == "live"
        assert "раньше обсуждали встречу" in prompt
        assert "КОНТЕКСТ СОБЕСЕДНИКА\n#1: любит прогулки" in prompt
        assert "ДЕФОЛТНЫЙ НАБОР" not in prompt
        return (
            "\n".join(
                f"===ВАРИАНТ===\nНАЗВАНИЕ: {name}\nТЕКСТ: хорошо"
                for name in ("Подхватить", "Уточнить", "Предложить")
            )
            + "\n===ЗАМЕТКИ===\n#1: любит прогулки\n#2: свободна вечером"
        )

    monkeypatch.setattr(llm, "ADAPTIVE_REPLY_PROMPT", True)
    monkeypatch.setattr(llm, "_ask", ask)
    variants, notes = asyncio.run(llm.live_coach_step(
        "я свободна вечером", "", "#1: любит прогулки", None,
        recent_dialogue="раньше обсуждали встречу",
    ))
    assert len(variants) == 3
    assert notes.splitlines() == ["#1: любит прогулки", "#2: свободна вечером"]


@pytest.mark.parametrize("bad_response", [
    "Первый вариант: хорошо\nВторой вариант: понял\nТретий вариант: увидимся",
    "===ВАРИАНТ===\nНАЗВАНИЕ: Уточнить\nТЕКСТ: хорошо",
])
def test_variant_format_error_falls_back_to_another_provider(monkeypatch, bad_response):
    calls = []
    complete = "\n".join(
        f"===ВАРИАНТ===\nНАЗВАНИЕ: {name}\nТЕКСТ: {answer}"
        for name, answer in (("Уточнить", "когда удобно?"), ("Предложить", "давай вечером"), ("Поддержать", "как скажешь"))
    )

    class Provider:
        def __init__(self, name, response):
            self.name = name
            self.response = response

        async def ask(self, prompt, max_tokens):
            calls.append(self.name)
            return llm._check_output(self.name, "test-model", self.response)

    monkeypatch.setattr(llm, "ADAPTIVE_REPLY_PROMPT", True)
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [
        Provider("first", bad_response), Provider("reserve", complete),
    ])
    monkeypatch.setattr(llm, "_priority_groq_for_call", lambda _: False)
    monkeypatch.setattr(llm, "_invalid_output_hook", None)
    monkeypatch.setattr(llm, "_invalid_outputs", {})
    monkeypatch.setattr(llm, "_provider_stats", {})
    monkeypatch.setattr(llm.llm_usage, "emit", lambda *args, **kwargs: None)
    variants = asyncio.run(llm.suggest_reply_variants("пойдём гулять?", "", ""))
    assert calls == ["first", "reserve"]
    assert len(variants) == 3
