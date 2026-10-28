"""«Ответ с CueMe» (fast): каскад моделей Groq 120b → 20b с кулдауном на
(модель, ключ) после 429, дальше — остальные провайдеры."""
import asyncio

import httpx
import pytest

import llm

M120, M20 = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
OK_TEXT = "Привет, давай встретимся в субботу у фонтана."


class _Gemini(llm.LLMProvider):
    name = "Gemini"

    def __init__(self):
        self.calls = 0

    async def ask(self, prompt, max_tokens):
        self.calls += 1
        return "Ответ от запасного провайдера, всё хорошо."


@pytest.fixture()
def groq(monkeypatch):
    """limited — множество моделей с 429 на всех ключах; calls — (модель, ключ)
    каждого HTTP-запроса; clock — подменённое время кулдауна."""
    state = {"limited": set(), "calls": [], "clock": 1000.0, "headers": {}, "body": "rate limit"}
    monkeypatch.setattr(llm, "_groq_keys_rotated", lambda: ["key-aaaa", "key-bbbb"])
    monkeypatch.setattr(llm, "_cooldown_clock", lambda: state["clock"])
    monkeypatch.setattr(llm, "_groq_cooldown", {})
    monkeypatch.setattr(llm, "_groq_model_stats", {})
    monkeypatch.setattr(llm, "VARIANTS_GROQ_MODELS", [M120, M20])

    async def fake_post(provider, key, client, url, **kw):
        model = kw["json"]["model"]
        state["calls"].append((model, key))
        if model in state["limited"]:
            return httpx.Response(429, text=state["body"], headers=state["headers"])
        return httpx.Response(200, json={"choices": [
            {"message": {"content": f"{OK_TEXT} ({model})"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(llm, "_tracked_post", fake_post)

    gemini = _Gemini()
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [llm.GroqProvider(), gemini])
    state["gemini"] = gemini
    return state


def _ask(fast=True):
    return asyncio.run(llm._ask("p", 100, tag=None, fast=fast))


def _models(state):
    return [m for m, _ in state["calls"]]


# (а) 120b отвечает 200 — ответ от 120b
def test_120b_answers(groq):
    assert _ask().endswith(f"({M120})")
    assert _models(groq) == [M120]
    assert llm.get_groq_model_stats()["gpt-oss-120b"]["ok"] == 1


# (б) 120b на 429 на всех ключах — ответ от 20b
def test_120b_limited_falls_to_20b(groq, caplog):
    groq["limited"] = {M120}
    with caplog.at_level("INFO", logger="llm"):
        assert _ask().endswith(f"({M20})")
    assert _models(groq) == [M120, M120, M20]
    assert "Groq: gpt-oss-120b исчерпана, перехожу на gpt-oss-20b" in caplog.text
    assert "LLM [Groq gpt-oss-20b]: ok" in caplog.text


# (в) обе модели на 429 — RateLimitError из Groq, ответ от Gemini
def test_both_limited_goes_to_gemini(groq):
    groq["limited"] = {M120, M20}
    async def groq_only():
        llm._llm_fast.set(True)
        return await llm.GroqProvider().ask("p", 100)
    with pytest.raises(llm.RateLimitError):
        asyncio.run(groq_only())
    groq["calls"].clear()
    llm._groq_cooldown.clear()
    assert _ask() == "Ответ от запасного провайдера, всё хорошо."
    assert _models(groq) == [M120, M120, M20, M20] and groq["gemini"].calls == 1


# (г) в пределах кулдауна 120b не трогается — сразу 20b
def test_cooldown_skips_120b(groq):
    groq["limited"] = {M120}
    _ask()
    groq["calls"].clear()
    groq["clock"] += 30  # кулдаун по умолчанию 60 с
    assert _ask().endswith(f"({M20})")
    assert _models(groq) == [M20]


# (д) кулдаун истёк — 120b снова первой
def test_cooldown_expires_and_120b_is_tried_first(groq):
    groq["limited"] = {M120}
    _ask()
    groq["limited"] = set()
    groq["calls"].clear()
    groq["clock"] += 61
    assert _ask().endswith(f"({M120})")
    assert _models(groq) == [M120]


@pytest.mark.parametrize("headers, body, expected", [
    ({}, "rate limit", 60),
    ({"retry-after": "7"}, "rate limit", 7),
    ({"retry-after": "7"}, "Limit 200000, Used 199990 tokens per day (TPD)", 900),
    ({}, "Rate limit reached: requests per day (RPD)", 900),
    ({"retry-after": "5000"}, "rate limit", 900),
])
def test_cooldown_duration(groq, headers, body, expected):
    groq["limited"], groq["headers"], groq["body"] = {M120}, headers, body
    _ask()
    assert llm._groq_cooldown_left(M120, "key-aaaa") == pytest.approx(expected)


def test_non_429_does_not_set_cooldown(groq, monkeypatch):
    async def fake_post(provider, key, client, url, **kw):
        groq["calls"].append((kw["json"]["model"], key))
        return httpx.Response(503, text="overloaded")
    monkeypatch.setattr(llm, "_tracked_post", fake_post)
    assert _ask() == "Ответ от запасного провайдера, всё хорошо."
    assert llm._groq_cooldown == {} and _models(groq) == [M120]  # как раньше: 5xx — сразу к следующему провайдеру


# (е) не-fast вызов — только GroqProvider._MODEL, без каскада и кулдауна
def test_non_fast_uses_main_model_only(groq):
    groq["limited"] = {M120}
    assert _ask(fast=False) == "Ответ от запасного провайдера, всё хорошо."
    assert _models(groq) == [llm.GroqProvider._MODEL] * 2
    assert llm._groq_cooldown == {}


def test_empty_models_list_falls_back_to_main_model(groq, monkeypatch):
    monkeypatch.setattr(llm, "VARIANTS_GROQ_MODELS", [])
    assert _ask().endswith(f"({llm.GroqProvider._MODEL})")
