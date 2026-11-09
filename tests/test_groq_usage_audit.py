import asyncio

import httpx

from tools.groq_usage_audit import fingerprint, inventory, month_bounds, monthly


def test_month_october_has_31_days():
    start, end = month_bounds("2026-10")
    assert (end - start).days == 31
    assert month_bounds("2026-12")[1].year == 2027


def test_absent_october_is_unknown():
    result = monthly([dict(type="coverage_start", ts="2026-11-07T00:00:00+00:00")], "2026-10")
    assert result["by_key_model"] == []
    assert result["measured_start"] is None
    assert "не восстановлен" in result["historical_total_status"]


def test_usage_date_dedup_and_unknown():
    base = dict(type="api", ts="2026-10-31T23:59:00+00:00", provider="Groq", origin="user",
                model="openai/gpt-oss-120b", request_id="one", credential_ref="ref", input_tokens=100,
                output_tokens=50, tariff_cost_usd=.01)
    unknown = dict(base, request_id="two", input_tokens=None, output_tokens=None, tariff_cost_usd=None)
    outside = dict(base, request_id="three", ts="2026-11-01T00:00:00+00:00")
    rows = monthly([base, base, unknown, outside], "2026-10")["by_key_model"]
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["input_tokens_known"] == 100
    assert rows[0]["all_attempts_tariff_usd"] is None
    assert rows[0]["known_tariff_subtotal_usd"] == .01


def test_inventory_only_get_deduplicates_and_hides_keys(monkeypatch):
    seen = []
    async def get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    rows = asyncio.run(inventory(["secret", "secret"]))
    assert len(rows) == len(seen) == 1
    assert "secret" not in str(rows)
    assert rows[0]["credential_ref"] == fingerprint("secret")
    assert rows[0]["model_120b_visible"]
