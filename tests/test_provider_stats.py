"""Регрессия (2026-10-14): /apistatus и /provider отвечали «Что-то пошло не так».
В llm.py два разных словаря статистики были названы одинаково
(_provider_stats): по ключам — {(provider, key_tail): _ProviderKeyStats}
и по провайдерам — {name: dict}. Второе присваивание перетирало первое,
обе функции писали в один словарь, и каждый геттер падал на чужих записях."""
import httpx
import pytest

import llm
import main


@pytest.fixture(autouse=True)
def _clean_stats():
    # Чистим обе структуры, как бы они ни назывались (до и после фикса).
    for name in ("_provider_stats", "_key_stats"):
        d = getattr(llm, name, None)
        if isinstance(d, dict):
            d.clear()
    yield
    for name in ("_provider_stats", "_key_stats"):
        d = getattr(llm, name, None)
        if isinstance(d, dict):
            d.clear()


def _groq_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "998",
            "content-type": "application/json",
        },
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
    )


def test_key_and_provider_stats_do_not_collide():
    provider = main.PROVIDER_NAMES[0]
    llm._track_response(provider, "gsk_test_key_abcd", _groq_response())
    llm._record_stat(provider, "ok", 120.0)

    snapshot = llm.get_provider_stats_snapshot()
    assert snapshot[(provider, "abcd")]["requests_24h"] == 1
    assert snapshot[(provider, "abcd")]["headers"]["x-ratelimit-remaining-requests"] == "998"
    assert all(isinstance(k, tuple) for k in snapshot)

    stats = llm.get_provider_stats()
    assert stats[provider]["ok"] == 1
    assert stats[provider]["calls"] == 1
    assert all(isinstance(k, str) for k in stats)

    text = main._format_apistatus(snapshot)
    assert "...abcd: 1 запросов/24ч" in text
    assert "x-ratelimit-remaining-requests: 998" in text


def test_network_error_tracked_without_breaking_provider_stats():
    provider = main.PROVIDER_NAMES[0]
    llm._record_stat(provider, "error", 50.0)
    llm._track_response(provider, "gsk_test_key_wxyz", None)  # сетевая ошибка, ответа нет

    assert llm.get_provider_stats_snapshot()[(provider, "wxyz")]["errors_1h"] == 1
    assert llm.get_provider_stats()[provider]["error"] == 1
