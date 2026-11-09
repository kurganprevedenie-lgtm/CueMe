import json

from tools.llm_cost_report import report_day


def test_exact_day_excludes_adjacent_dates(tmp_path):
    rows = [dict(schema=1, type="api", request_id=str(n), ts=ts, provider="Groq",
                 model="openai/gpt-oss-120b", origin="user", tariff_cost_usd=.01)
            for n, ts in enumerate(["2026-11-07T23:59:59+00:00", "2026-11-08T00:00:00+00:00",
                                    "2026-11-08T23:59:59+00:00", "2026-11-09T00:00:00+00:00"])]
    (tmp_path / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    result = report_day("2026-11-08", tmp_path)
    assert result["production"]["api_calls"] == 2
    assert result["calendar_timezone"] == "UTC"
    result = report_day("2026-11-08", tmp_path, "Europe/Moscow")
    assert result["production"]["api_calls"] == 2
    assert result["since"] == "2026-11-07T21:00:00+00:00"
