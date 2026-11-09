import asyncio

import pytest
import httpx

import llm
import llm_usage


@pytest.fixture
def paid(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_PAID_API_KEY", "paid-test")
    monkeypatch.setattr(llm, "GROQ_PAID_PRIORITY_ENABLED", True)
    monkeypatch.setattr(llm, "GROQ_API_KEYS", ["free-test", "paid-test"])
    monkeypatch.setattr(llm, "_forced", None)
    monkeypatch.setattr(llm, "_groq_cooldown", {})


def test_selected_functions_only_and_no_background(paid):
    for tag in ("variants", "live", "compat"):
        assert llm._priority_groq_for_call(tag)
    for tag in ("ideal_date", "style_card", "interaction_card", "overall_style", "my_style_contact", "transcription", None):
        assert not llm._priority_groq_for_call(tag)
    with llm_usage.action_scope("background", "test"):
        assert not llm._priority_groq_for_call("compat")


def test_disabled_by_default_or_without_key(paid, monkeypatch):
    monkeypatch.setattr(llm, "GROQ_PAID_PRIORITY_ENABLED", False)
    assert not llm._priority_groq_for_call("variants")
    monkeypatch.setattr(llm, "GROQ_PAID_PRIORITY_ENABLED", True)
    monkeypatch.setattr(llm, "GROQ_PAID_API_KEY", "")
    assert not llm._priority_groq_for_call("variants")


def test_paid_key_never_in_rotating_free_pool(paid):
    for _ in range(4):
        assert llm._groq_keys_rotated() == ["free-test"]


def test_priority_is_120b_once(paid, monkeypatch):
    calls = []
    async def ask(self, prompt, max_tokens, key, model=None):
        calls.append((key, model))
        return "ответ"
    monkeypatch.setattr(llm.GroqProvider, "_ask_with_key", ask)
    assert asyncio.run(llm.PriorityGroqProvider().ask("test", 100)) == "ответ"
    assert calls == [("paid-test", "openai/gpt-oss-120b")]


def test_limit_cooldown_does_not_retry_paid(paid, monkeypatch):
    calls = []
    async def ask(self, *args):
        calls.append(1)
        exc = llm.RateLimitError("limit")
        exc.cooldown = 30
        raise exc
    monkeypatch.setattr(llm.GroqProvider, "_ask_with_key", ask)
    async def run():
        for _ in range(2):
            with pytest.raises(llm.RateLimitError):
                await llm.PriorityGroqProvider().ask("test", 100)
    asyncio.run(run())
    assert len(calls) == 1


def test_paid_success_skips_free_cascade(paid, monkeypatch):
    async def answer(self, *args):
        return "ответ 120b"
    monkeypatch.setattr(llm.PriorityGroqProvider, "ask", answer)
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [])
    assert asyncio.run(llm._ask("test", tag="variants", fast=True)) == "ответ 120b"


def test_paid_failure_preserves_free_fallback(paid, monkeypatch):
    class Free:
        name = "Groq"
        async def ask(self, *args):
            return "бесплатный резерв"
    async def failure(self, *args):
        raise llm.ProviderError("temporary")
    monkeypatch.setattr(llm.PriorityGroqProvider, "ask", failure)
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [Free()])
    assert asyncio.run(llm._ask("test", tag="compat")) == "бесплатный резерв"


def test_whisper_never_receives_dedicated_paid_key(paid, monkeypatch):
    used = []
    async def post(self, url, **kwargs):
        used.append(kwargs["headers"]["Authorization"])
        assert kwargs["data"]["model"] == "whisper-large-v3-turbo"
        return httpx.Response(200, json={"text": "расшифровка"})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(llm.transcribe_audio(b"fake")) == "расшифровка"
    assert used == ["Bearer free-test"]
