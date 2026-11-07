import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

import llm_usage as usage
from llm_pricing import price_usage
from llm_usage_report import build_report, read_events


def test_unknown_usage_is_not_zero():
    tokens = usage.extract_usage("OpenAI", {})
    assert all(value is None for value in tokens.values())
    assert price_usage("OpenAI", "gpt-6-luna", tokens)["usd"] is None


def test_reasoning_is_subset_not_double_billed():
    tokens = usage.extract_usage("OpenAI", {"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 500}, "completion_tokens_details": {"reasoning_tokens": 150}}})
    assert tokens["output_tokens"] == 200
    assert tokens["reasoning_tokens"] == 150
    assert price_usage("OpenAI", "gpt-6-luna", tokens)["usd"] == pytest.approx(0.000155)


def test_gemini_total_includes_thoughts():
    tokens = usage.extract_usage("Gemini", {"usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 180,
        "candidatesTokenCount": 30, "thoughtsTokenCount": 50}})
    assert tokens["output_tokens"] == 80
    assert tokens["reasoning_tokens"] == 50
    assert tokens["cached_input_tokens"] is None


def test_malformed_details_do_not_break_accounting():
    assert usage.extract_usage("OpenAI", {"usage": {"prompt_tokens_details": ["unexpected"]}})["cached_input_tokens"] is None


def test_deepseek_split_and_weekend():
    tokens = usage.extract_usage("DeepSeek", {"usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 250, "completion_tokens": 200}})
    cost = price_usage("DeepSeek", "deepseek-flash", tokens, datetime(2026, 11, 7, tzinfo=timezone.utc))
    assert cost["usd"] == pytest.approx((750 * .15 + 250 * .003 + 200 * .6) / 1e6)


def test_retry_privacy_and_failed_output_still_costs():
    @usage.logical_call
    async def call():
        for status in (429, 200):
            request = usage.request_start("OpenAI", "gpt-6-luna", "variants")
            response = httpx.Response(status, json={"model": "gpt-6-luna", "usage": {"prompt_tokens": 100,
                "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
                "choices": [{"message": {"content": "SECRET_CHAT"}}], "secret": "SECRET_KEY"})
            usage.request_end(request, 12, response)
        usage.validation("invalid_output")
    with usage.action_scope("user", "telegram_update"):
        asyncio.run(call())
        usage.note_outcome("error")
    events, _ = read_events()
    calls = [e for e in events if e["type"] == "api"]
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[1]["retry"]
    assert calls[1]["tariff_cost_usd"] > 0
    assert "SECRET" not in json.dumps(events)


def test_measurement_error_does_not_hide_response(monkeypatch):
    monkeypatch.setattr(usage, "extract_usage", lambda *a: 1 / 0)
    usage.request_end(usage.request_start("OpenAI", "gpt-6-luna", "test"), 1, httpx.Response(200))
    events, _ = read_events()
    assert events[-1]["type"] == "measurement_error"


def test_background_detaches_and_context_restored():
    @usage.background_usage
    async def work():
        usage.request_end(usage.request_start("Groq", "test", "interaction_card"), 1)
    with usage.action_scope("user", "telegram_update") as action:
        asyncio.run(work())
        assert action.requests == 0
    assert usage._action.get() is None
    events, _ = read_events()
    assert next(e for e in events if e["type"] == "api")["origin"] == "background"


def test_report_unknown_forecast_and_benchmark_exclusion():
    events = [dict(type="coverage_start", ts="2026-11-01T00:00:00+00:00"),
              dict(type="api", ts="2026-11-02T00:00:00+00:00", request_id="a", action_id="u", origin="user", tariff_cost_usd=None),
              dict(type="api", ts="2026-11-02T00:00:00+00:00", request_id="b", origin="benchmark", tariff_cost_usd=10),
              dict(type="action", ts="2026-11-02T00:00:00+00:00", action_id="u", origin="user", outcome="ok")]
    report = build_report(events, "2026-11-01", "2026-11-08")
    assert report["production"]["api_calls"] == 1
    assert report["forecast_30_days_tariff_usd"] is None
    assert report["average_successful_action_tariff_usd"] is None
    assert report["benchmark_excluded"]["total_tariff_usd"] == 10


def test_report_known_cost_includes_failed_attempts_and_background():
    events = [dict(type="coverage_start", ts="2026-11-01T00:00:00+00:00")]
    for ident, origin, action in (("a", "user", "u"), ("b", "user", "u"), ("c", "background", "bg")):
        events.append(dict(type="api", ts="2026-11-02T00:00:00+00:00", request_id=ident, origin=origin, action_id=action, tariff_cost_usd=1))
    events.append(dict(type="action", ts="2026-11-02T00:00:00+00:00", action_id="u", origin="user", outcome="ok"))
    report = build_report(events, "2026-11-01", "2026-11-07")
    assert report["average_successful_action_tariff_usd"] == 2
    assert report["fully_loaded_per_success_tariff_usd"] == 3
    assert report["forecast_30_days_tariff_usd"] == 15


def test_benchmark_capture_and_budget_guard(monkeypatch):
    from tools.bench_paid_models import cases, run_benchmark, reservation
    scenarios = asyncio.run(cases())
    assert len(scenarios) == 7
    assert {c["tag"] for c in scenarios} >= {"variants", "live", "ideal_date", "compat"}
    assert reservation("OpenAI", "gpt-6-luna", scenarios[0]) > 0
    with pytest.raises(ValueError):
        asyncio.run(run_benchmark(scenarios, float("nan")))


def test_api_url_does_not_include_gemini_secret():
    import llm
    assert "secret" not in llm.GeminiProvider._url("model", "secret")


def test_paid_benchmark_mocked_http_and_usage(monkeypatch):
    from tools import bench_paid_models as bench
    monkeypatch.setattr(bench, "CANDIDATES", [("OpenAI", "gpt-6-luna")])
    monkeypatch.setattr(bench, "BASES", {"OpenAI": "https://api.openai.com/v1"})
    monkeypatch.setattr(bench.config, "OPENAI_API_KEY", "FAKE_SECRET")
    async def post(self, url, **kwargs):
        assert kwargs["json"]["reasoning_effort"] == "none"
        return httpx.Response(200, json={"model": "gpt-6-luna", "choices": [{"message": {"content":
            "давай выпьем кофе в субботу, погуляем у воды и послушаем музыку\n===ПОДАРКИ===\nпачка любимого кофе и небольшой билет на джазовый вечер"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 0}}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    scenario = dict(case="test", prompt="Вымышленный диалог", tag="ideal_date", max_tokens=1200, prompt_sha256="abc")
    result = asyncio.run(bench.run_benchmark([scenario], .1, 1))
    assert len(result["rows"]) == 1
    assert result["rows"][0]["outcome"] == "format_ok_needs_human_review"
    assert result["rows"][0]["tariff_cost_usd"] > 0
    assert all(e["origin"] == "benchmark" for e in read_events()[0] if e["type"] == "api")
    assert "FAKE_SECRET" not in json.dumps(read_events()[0])


def test_cancelled_http_attempt_is_recorded(monkeypatch):
    import llm
    async def cancelled(self, *args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(httpx.AsyncClient, "post", cancelled)
    async def call():
        async with httpx.AsyncClient() as client:
            with pytest.raises(asyncio.CancelledError):
                await llm._tracked_post("Groq", "fake", client, "https://example.invalid", json={"model": "test"})
    asyncio.run(call())
    event = next(e for e in read_events()[0] if e["type"] == "api")
    assert event["error_type"] == "CancelledError"
    assert event["tariff_cost_usd"] is None


def test_budget_reserves_full_comparison_before_first_request(monkeypatch):
    from tools.bench_paid_models import cases, run_benchmark
    scenarios = asyncio.run(cases())
    with pytest.raises(ValueError, match="резерв"):
        asyncio.run(run_benchmark(scenarios, .001))


def test_middleware_retains_success_outcome_and_own_action():
    async def handler(event, data):
        usage.note_outcome("ok")
        return "delivered"
    assert asyncio.run(usage.UsageActionMiddleware()(handler, None, {})) == "delivered"
    event = next(e for e in read_events()[0] if e["type"] == "action")
    assert event["outcome"] == "ok"
    assert event["origin"] == "user"
