"""Проверка ответа модели (инцидент 2026-10-11: в «Идеальном свидании» юзер
получил сырые рассуждения Gemma на английском вместо идеи)."""
import asyncio

import httpx
import pytest

import llm
from llm import ProviderError, validate_llm_output

# Начало и обрывок ответа из инцидента.
GARBAGE = (
    'Confident dating coach. "Thou/you" (ты). Interlocutor: the girl. *Correction: The user says '
    "she likes sushi, so I will suggest a sushi place. Draft: 💐 Идеальное свидание ... a gift that shows *listening"
)
GOOD_IDEA = (
    "💐 Идеальное свидание\n\nОна упоминала, что любит суши и давно хотела на мастер-класс, "
    "поэтому позови её на мастер-класс по роллам в субботу.\n\n===ПОДАРКИ===\n\n"
    "🎁 Как порадовать\n\nСудя по тому, что она говорила про керамику, — зайдёт набор для лепки."
)


def test_garbage_from_incident_is_rejected():
    assert validate_llm_output(GARBAGE, "ideal_date") is not None


def test_good_russian_idea_passes():
    assert validate_llm_output(GOOD_IDEA, "ideal_date") is None


def test_missing_gifts_marker_is_rejected():
    assert "маркер" in validate_llm_output("💐 Идеальное свидание\n\nСходите в кино на новый фильм.", "ideal_date")


def test_empty_is_rejected():
    assert validate_llm_output("   ", "ideal_date") == "пустой ответ"


def test_reasoning_marker_in_russian_text_is_rejected():
    assert "рассуждения" in validate_llm_output("Привет! Wait, давай подумаем ещё раз о том, что ответить.")


def test_untagged_russian_text_passes():
    assert validate_llm_output("🧬 Голос: пишет коротко, со строчной буквы.") is None


def _gemini_resp(parts, finish="STOP"):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": finish}]},
                          request=httpx.Request("POST", "https://x"))


def _run_gemini(monkeypatch, resp):
    async def fake_post(provider, key, client, url, **kw):
        return resp
    monkeypatch.setattr(llm, "_tracked_post", fake_post)
    llm._llm_call_tag.set("ideal_date")
    return asyncio.run(llm.GeminiProvider()._ask_with_key("p", 100, "key1234", "gemini-3.5-flash-lite"))


def test_gemini_max_tokens_is_error(monkeypatch):
    with pytest.raises(ProviderError, match="MAX_TOKENS"):
        _run_gemini(monkeypatch, _gemini_resp([{"text": GOOD_IDEA}], finish="MAX_TOKENS"))


def test_gemini_skips_thought_parts(monkeypatch):
    out = _run_gemini(monkeypatch, _gemini_resp([{"text": "The user wants...", "thought": True}, {"text": GOOD_IDEA}]))
    assert out == GOOD_IDEA


def test_gemini_garbage_is_error(monkeypatch):
    with pytest.raises(ProviderError, match="невалидный"):
        _run_gemini(monkeypatch, _gemini_resp([{"text": GARBAGE}]))


class _Fake(llm.LLMProvider):
    def __init__(self, name, text):
        self.name, self.text = name, text

    async def ask(self, prompt, max_tokens):
        return llm._check_output(self.name, "", self.text)


def test_ask_falls_through_garbage_to_next_provider(monkeypatch):
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [_Fake("Gemini", GARBAGE), _Fake("OpenRouter", GOOD_IDEA)])
    seen = []
    llm.set_invalid_output_hook(lambda p, t, r: seen.append((p, t)))
    try:
        assert asyncio.run(llm._ask("p", 100, tag="ideal_date")) == GOOD_IDEA
    finally:
        llm.set_invalid_output_hook(None)
    assert seen == [("Gemini", "ideal_date")]


def test_ask_raises_when_all_providers_return_garbage(monkeypatch):
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [_Fake("Gemini", GARBAGE), _Fake("OpenRouter", GARBAGE)])
    with pytest.raises(RuntimeError):
        asyncio.run(llm._ask("p", 100, tag="ideal_date"))


def test_garbage_is_not_saved_to_ideal_date_cache(monkeypatch):
    import main
    saved = []

    async def failing_build(*a, **k):
        raise RuntimeError("Все LLM-провайдеры недоступны")

    async def no_card(*a, **k):
        return ""

    monkeypatch.setattr(main, "build_ideal_date", failing_build)
    monkeypatch.setattr(main, "_gen_interaction_card", no_card)
    monkeypatch.setattr(main, "get_ideal_date", lambda cid: None)
    monkeypatch.setattr(main, "_ideal_date_samples", lambda *a: {"contact_sample": ["x"] * 30, "my_sample": [], "features_summary": ""})
    monkeypatch.setattr(main, "save_ideal_date", lambda *a: saved.append(a))
    with pytest.raises(RuntimeError):
        asyncio.run(main._gen_ideal_date(1, "1"))
    assert saved == []


def test_gemma_goes_last_for_regular_calls():
    llm._llm_fast.set(False)
    cascade = llm.GeminiProvider()._cascade_for_call()
    assert cascade[-2:] == ["gemma-4-26b-a4b-it", "gemma-4-31b-it"]
    assert not cascade[0].startswith("gemma-")
