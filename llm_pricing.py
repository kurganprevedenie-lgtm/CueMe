"""Проверенный снимок официальных тарифов USD/1M, 2026-11-07.

Это расчёт по прайсу, НЕ банковское списание: кредиты, налоги и free-tier
неизвестны. Неизвестная модель/режим/usage никогда не превращается в $0.
"""
from datetime import datetime, timezone

VERIFIED_ON = "2026-11-07"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
GROQ_SOURCE = "https://console.groq.com/docs/models"
RATES = {
    # Cache hit 50%: https://console.groq.com/docs/prompt-caching
    ("Groq", "openai/gpt-oss-120b"): (0.15, 0.075, 0.60, GROQ_SOURCE),
    ("Groq", "openai/gpt-oss-20b"): (0.075, 0.0375, 0.30, GROQ_SOURCE),
    ("OpenAI", "gpt-6-luna"): (0.10, 0.01, 0.50, OPENAI_SOURCE),
    ("OpenAI", "gpt-6.1-terra"): (1.00, 0.05, 5.00, OPENAI_SOURCE),
    ("DeepSeek", "deepseek-flash"): (0.30, 0.006, 1.20, DEEPSEEK_SOURCE),
    ("DeepSeek", "deepseek-v4-pro"): (1.32, 0.044, 3.96, DEEPSEEK_SOURCE),
}


def tariff(provider, model, at=None):
    rate = RATES.get((provider, model))
    if not rate:
        return None
    input_rate, cached_rate, output_rate, source = rate
    band = "standard"
    if provider == "DeepSeek":
        at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # Китайские праздники не хардкодим: в их будни консервативно peak.
        peak = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
        band = "peak_possible_holiday" if peak else "off_peak"
        if not peak:
            input_rate, cached_rate, output_rate = input_rate / 2, cached_rate / 2, output_rate / 2
    return dict(input=input_rate, cached=cached_rate, output=output_rate,
                source=source, verified_on=VERIFIED_ON, band=band)


def price_usage(provider, model, usage, at=None, service_tier=None):
    rate = tariff(provider, model, at)
    if not rate:
        return dict(usd=None, reason="unverified_tariff")
    if service_tier not in (None, "default", "standard", "auto"):
        return dict(usd=None, reason="unverified_service_tier", tariff=rate)
    i, c, o = (usage.get(k) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
    if i is None or o is None:
        return dict(usd=None, reason="missing_usage", tariff=rate)
    if c is None:
        return dict(usd=None, reason="unknown_cached_tokens", tariff=rate)
    if usage.get("cache_write_tokens"):
        return dict(usd=None, reason="cache_write_pricing_not_resolved", tariff=rate)
    if min(i, c, o) < 0 or c > i:
        return dict(usd=None, reason="invalid_usage", tariff=rate)
    if provider == "OpenAI" and i > 272000:
        return dict(usd=None, reason="long_context_tier_not_resolved", tariff=rate)
    return dict(usd=((i - c) * rate["input"] + c * rate["cached"] + o * rate["output"]) / 1e6,
                reason="tariff_calculation_not_invoice", tariff=rate)
