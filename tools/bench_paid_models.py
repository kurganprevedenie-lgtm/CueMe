"""Изолированный админский тест: по умолчанию БЕЗ сети и списаний.

python -m tools.bench_paid_models --check-models  # GET, проверка доступа
python -m tools.bench_paid_models --run --budget-usd 2 --repeats 3
Только вымышленные ситуации, никакого чтения bot.db. Не меняет каскад бота.
"""
import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import config
import llm
import llm_usage
from llm_pricing import RATES, price_usage

CANDIDATES = [
    ("OpenAI", "gpt-6-luna"), ("OpenAI", "gpt-6.1-terra"),
    ("DeepSeek", "deepseek-flash"), ("DeepSeek", "deepseek-v4-pro"),
]
BASES = {"OpenAI": "https://api.openai.com/v1", "DeepSeek": "https://api.deepseek.com"}


class Captured(Exception):
    pass


async def cases():
    """Перехватываем штатные построители промптов без обращения к модели."""
    found = []
    current = None

    async def capture(prompt, max_tokens=1024, *, tag=None, fast=False, **kwargs):
        found.append(dict(case=current, prompt=prompt, max_tokens=max_tokens, tag=tag,
                          chars=len(prompt), prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest()))
        raise Captured()

    style = "Короткие сообщения, строчные буквы, добрый юмор, без пафоса."
    interaction = "Любит прогулки, кофе и живую музыку. Ценит конкретные предложения, без давления."
    jobs = [
        ("приглашение", lambda: llm.suggest_reply_variants("хочу выбраться куда-нибудь в субботу", style, interaction)),
        ("граница", lambda: llm.suggest_reply_variants("сегодня не хочу встречаться, устала", style, interaction)),
        ("конфликт", lambda: llm.suggest_reply_variants("ты опять пропал и ничего не объяснил", style, interaction)),
        ("живой_диалог", lambda: llm.live_coach_step("а какое место ты предлагаешь?", style, "Обсуждают прогулку в субботу.", ["Я: давай увидимся в субботу", "Собеседник: можно"])),
        ("свидание", lambda: llm.build_ideal_date(["люблю джаз", "кофе без сахара", "люблю гулять у воды"], ["давай встретимся в выходные"], interaction, "Переписываются месяц, общение взаимное.")),
        ("карточка", lambda: llm.build_interaction_card(["как день?", "давай завтра"], ["люблю прогулки", "спасибо за поддержку", "давай завтра", "кофе?", "я устала"], "Короткие сообщения, инициатива взаимная.")),
    ]
    from compatibility_metrics import compute_all
    metrics = compute_all([dict(date=f"2026-10-{i // 2 + 1:02d}T12:{i % 2:02d}:00+00:00",
                               direction="out" if i % 2 else "in", text="Как день?" if i % 2 else "Хорошо, гуляла") for i in range(24)])
    volume = metrics.pop("_volume_trend")
    jobs.append(("анализ", lambda: llm.build_compatibility_interpretation(metrics, volume, ["Я: как день?", "Собеседник: хорошо, гуляла"])))
    with patch.object(llm, "_ask", capture):
        for name, job in jobs:
            current = name
            try:
                await job()
            except Captured:
                pass
    return found


def reservation(provider, model, case):
    # Пессимистичный резерв: UTF-8 байты вместо предположения chars/4,
    # запас на обёртку и cache-write. Это лимит теста, НЕ измеренные токены.
    input_bound = len(case["prompt"].encode()) + 1024
    if input_bound > 272000:
        raise ValueError("Промпт превышает поддержанный тарифный диапазон теста")
    i, _, o, _ = RATES[(provider, model)]
    return (input_bound * i * 3 + case["max_tokens"] * o) / 1e6


async def check_models():
    result = []
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        for provider in BASES:
            key = getattr(config, f"{provider.upper()}_API_KEY")
            if not key:
                result.append(dict(provider=provider, status="missing_key"))
                continue
            try:
                response = await client.get(BASES[provider] + "/models", headers={"Authorization": f"Bearer {key}"})
                ids = {r.get("id") for r in response.json().get("data", [])} if response.is_success else set()
                result.append(dict(provider=provider, http_status=response.status_code,
                                   models={m: m in ids for p, m in CANDIDATES if p == provider}))
            except Exception as exc:
                result.append(dict(provider=provider, status=type(exc).__name__))
    return result


async def run_benchmark(scenarios, budget, repeats=3):
    if not math.isfinite(budget) or not 0 < budget <= 5 or not 1 <= repeats <= 5:
        raise ValueError("Нужен бюджет 0 < USD ≤ 5 и от 1 до 5 повторов")
    planned_reserve = sum(reservation(p, m, c) for p, m in CANDIDATES for c in scenarios) * repeats
    if planned_reserve > budget:
        raise ValueError(f"Для равного числа прогонов нужен резерв ${planned_reserve:.3f}; уменьши --repeats или увеличь бюджет")
    missing = [p for p in BASES if not getattr(config, f"{p.upper()}_API_KEY")]
    if missing:
        raise ValueError("Не заданы ключи: " + ", ".join(p.upper() + "_API_KEY" for p in missing))
    spent_reserve, rows = 0.0, []
    labels = list("ABCD")
    random.SystemRandom().shuffle(labels)
    mapping = dict(zip(labels, CANDIDATES))
    jobs = [(label, p, m, c, run) for label, (p, m) in mapping.items()
            for c in scenarios for run in range(repeats)]
    random.SystemRandom().shuffle(jobs)
    async with httpx.AsyncClient(trust_env=False, timeout=45) as client:
        for label, provider, model, case, run in jobs:
            reserve = reservation(provider, model, case)
            if spent_reserve + reserve > budget:
                break
            # Не возвращаем резерв при неизвестном usage/ошибке/таймауте.
            spent_reserve += reserve
            key = getattr(config, f"{provider.upper()}_API_KEY")
            payload = dict(model=model, messages=[dict(role="user", content=case["prompt"])])
            if provider == "OpenAI":
                payload.update(max_completion_tokens=case["max_tokens"], reasoning_effort="none", store=False, service_tier="default")
            else:
                payload.update(max_tokens=case["max_tokens"], thinking={"type": "disabled"})
            row = dict(candidate=label, case=case["case"], run=run + 1, prompt_sha256=case["prompt_sha256"],
                       reserved_usd=reserve, human_scores=None)
            t0 = time.monotonic()
            started_at = datetime.now(timezone.utc)
            with llm_usage.action_scope("benchmark", "paid_comparison"):
                try:
                    response = await llm._tracked_post(provider, key, client, BASES[provider] + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"}, json=payload, _usage_tag=case["tag"], _usage_model=model)
                    row["http_status"] = response.status_code
                    if not response.is_success:
                        row["outcome"] = "http_error"
                    else:
                        data = response.json()
                        choice = data["choices"][0]
                        text = choice["message"].get("content") or ""
                        reason = llm.validate_llm_output(text, case["tag"])
                        if choice.get("finish_reason") == "length":
                            reason = "output_limit"
                        if not reason and case["tag"] in ("variants", "live") and len(llm._parse_variants(text, 3)) != 3:
                            reason = "variant_count"
                        llm_usage.validation("invalid_output" if reason else "valid_output")
                        usage = llm_usage.extract_usage(provider, data)
                        actual_model = llm_usage.safe_name(data.get("model") or model)
                        cost = price_usage(provider, actual_model, usage, started_at, data.get("service_tier"))
                        # Здесь только специально придуманные диалоги, не переписки пользователей.
                        row.update(outcome="invalid_output" if reason else "format_ok_needs_human_review",
                                   invalid_reason=reason, answer=text, usage=usage, actual_model=actual_model,
                                   tariff_cost_usd=cost["usd"], cost_reason=cost["reason"])
                except Exception as exc:
                    row.update(outcome="request_failed", error_type=type(exc).__name__)
            row["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            rows.append(row)
    summaries = []
    for label in labels:
        group = [r for r in rows if r["candidate"] == label]
        times = [r["duration_ms"] for r in group]
        costs = [r.get("tariff_cost_usd") for r in group]
        summaries.append(dict(candidate=label, calls=len(group), median_ms=statistics.median(times) if times else None,
                              format_ok=sum(r["outcome"] == "format_ok_needs_human_review" for r in group),
                              total_usd=sum(costs) if costs and all(c is not None for c in costs) else None))
    return dict(rows=rows, summaries=summaries, reserved_usd=spent_reserve, budget_usd=budget,
                blind_mapping={k: list(v) for k, v in mapping.items()},
                rubric="Оценить вслепую 1–5: естественность русского, голос автора, уместность, уважение границ, конкретность. Формат не является оценкой качества.")


async def async_main(args):
    scenarios = await cases()
    if args.check_models:
        result = await check_models()
    elif args.run:
        if args.budget_usd is None:
            raise ValueError("Для платного запуска обязательно --budget-usd")
        result = await run_benchmark(scenarios, args.budget_usd, args.repeats)
    else:
        result = dict(mode="dry_run_no_network", cases=[{k: v for k, v in c.items() if k != "prompt"} for c in scenarios],
                      candidates=CANDIDATES, calls_planned=len(scenarios) * len(CANDIDATES) * args.repeats,
                      conservative_reserve_usd=sum(reservation(p, m, c) for p, m in CANDIDATES for c in scenarios) * args.repeats,
                      keys_present={p: bool(getattr(config, f"{p.upper()}_API_KEY")) for p in BASES})
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output = args.output or Path.home() / f"cueme_model_comparison_{suffix}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Результат: {output}")
    if isinstance(result, dict):
        print(json.dumps({k: v for k, v in result.items() if k not in ("rows", "blind_mapping")}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--check-models", action="store_true")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(async_main(args))
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
