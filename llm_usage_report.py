"""Отчёт только из измерений. Без восстановления запросов по users/messages."""
import json
from collections import defaultdict
from datetime import datetime, timezone

from llm_usage import usage_dir
from llm_pricing import RATES


def migration_estimates(rows, days):
    """Сценарий на измеренном объёме, НЕ фактическая стоимость новой модели.

    Токенизаторы, длина ответа и число retries у другой модели отличаются.
    Cache hit не переносим между провайдерами. Для DeepSeek даём диапазон.
    """
    text_rows = [r for r in rows if r.get("function") not in ("transcription", "vision")]
    measured = [r for r in text_rows if r.get("input_tokens") is not None and r.get("output_tokens") is not None]
    i = sum(r["input_tokens"] for r in measured)
    o = sum(r["output_tokens"] for r in measured)
    results = []
    for (provider, model), (ir, _, outr, _) in RATES.items():
        if provider not in ("OpenAI", "DeepSeek"):
            continue
        high = (i * ir + o * outr) / 1e6 if measured else None
        low = high / 2 if high is not None and provider == "DeepSeek" else high
        results.append(dict(provider=provider, model=model, measured_attempts=len(measured),
                            unknown_attempts=len(text_rows) - len(measured),
                            measured_volume_only_usd_low=low, measured_volume_only_usd_high=high,
                            partial_30_days_usd_low=low * 30 / days if low is not None and days else None,
                            partial_30_days_usd_high=high * 30 / days if high is not None and days else None,
                            assumptions="same_token_volume_and_attempts; no_cache; different_tokenizers_and_quality_not_measured; unknown_attempts_excluded"))
    return results


def read_events(directory=None):
    events, broken = [], 0
    for path in sorted((directory or usage_dir()).glob("usage.jsonl*")):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("schema") == 1:
                        events.append(event)
                except (ValueError, TypeError):
                    broken += 1
    return events, broken


def _date(raw):
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _totals(rows):
    known = [r["tariff_cost_usd"] for r in rows if r.get("tariff_cost_usd") is not None]
    unknown = len(rows) - len(known)
    return dict(api_calls=len(rows), retries=sum(bool(r.get("retry")) for r in rows),
                http_errors=sum(r.get("outcome") == "http_or_network_error" for r in rows),
                unknown_usage=sum(r.get("input_tokens") is None or r.get("output_tokens") is None for r in rows),
                unknown_cost=unknown, known_tariff_subtotal_usd=sum(known) if rows else None,
                total_tariff_usd=sum(known) if rows and not unknown else None,
                input_tokens_known=sum(r.get("input_tokens") or 0 for r in rows),
                cached_input_tokens_known=sum(r.get("cached_input_tokens") or 0 for r in rows),
                output_tokens_known=sum(r.get("output_tokens") or 0 for r in rows),
                reasoning_tokens_known=sum(r.get("reasoning_tokens") or 0 for r in rows))


def build_report(events, since, until):
    since, until = _date(since), _date(until)
    if until <= since:
        raise ValueError("Конец периода должен быть позже начала")
    selected = []
    for event in events:
        try:
            if since <= _date(event.get("started_at") or event["ts"]) < until:
                selected.append(event)
        except (ValueError, KeyError, TypeError):
            continue
    # Один request_id — одна попытка, даже если JSONL случайно скопировали дважды.
    api = {r["request_id"]: r for r in selected if r.get("type") == "api"}
    actions = {r["action_id"]: r for r in selected if r.get("type") == "action"}
    validation = {r["request_id"]: r["outcome"] for r in events if r.get("type") == "validation"}
    production = [r for r in api.values() if r.get("origin") != "benchmark"]
    benchmark = [r for r in api.values() if r.get("origin") == "benchmark"]
    groups = defaultdict(list)
    attempts_by_action = defaultdict(list)
    for row in production:
        groups[(row.get("function"), row.get("provider"), row.get("model"))].append(row)
        attempts_by_action[row.get("action_id")].append(row)
    functions = []
    for key, rows in sorted(groups.items(), key=lambda pair: str(pair[0])):
        functions.append(dict(function=key[0], provider=key[1], model=key[2], **_totals(rows),
                              invalid_outputs=sum(validation.get(r["request_id"]) in ("invalid_output", "empty_output") for r in rows)))
    successful = [a for a in actions.values() if a.get("origin") == "user" and a.get("outcome") == "ok"]
    per_action = []
    for action in successful:
        # Было: [r for r in production if r.get("action_id") == action["action_id"]].
        # Полный проход по API для каждого действия давал O(actions * API).
        # Готовый индекс сохраняет те же попытки/порядок за линейный проход.
        attempts = attempts_by_action.get(action["action_id"], [])
        # Ноль API-запросов у подтверждённого успешного действия — локальный
        # кэш/расчёт. Не приравниваем к нулю неизвестный usage реального вызова.
        total = _totals(attempts)
        if not attempts:
            total.update(total_tariff_usd=0.0, known_tariff_subtotal_usd=0.0)
        per_action.append(dict(functions=action.get("functions", []), **total))
    total = _totals(production)
    starts = [_date(e["ts"]) for e in events if e.get("type") == "coverage_start" and e.get("ts")]
    measured_start = max(since, min(starts)) if starts and min(starts) < until else None
    days = (until - measured_start).total_seconds() / 86400 if measured_start and until > measured_start else None
    forecast = total["total_tariff_usd"] * 30 / days if days and total["total_tariff_usd"] is not None else None
    costs = [a["total_tariff_usd"] for a in per_action]
    action_groups = defaultdict(list)
    for action in per_action:
        action_groups[tuple(sorted(action["functions"])) or ("local_cache_or_metrics",)].append(action)
    success_by_function = []
    for functions_key, group in sorted(action_groups.items()):
        values = [a["total_tariff_usd"] for a in group]
        success_by_function.append(dict(functions=list(functions_key), successful_actions=len(group),
            mean_api_calls=sum(a["api_calls"] for a in group) / len(group),
            mean_cost_usd=sum(values) / len(group) if all(v is not None for v in values) else None))
    return dict(
        since=since.isoformat(), until=until.isoformat(), measured_start=measured_start.isoformat() if measured_start else None,
        observation_days=days, period_coverage="inferred_from_instrumentation_start; downtime_and_loss_not_verified",
        actual_invoice_usd=None, production=total, by_function_model=functions,
        successful_user_actions=len(successful), successful_actions_without_api=sum(a["api_calls"] == 0 for a in per_action),
        successful_actions_by_functions=success_by_function,
        average_successful_action_tariff_usd=(sum(costs) / len(costs)) if costs and all(c is not None for c in costs) else None,
        fully_loaded_per_success_tariff_usd=(total["total_tariff_usd"] / len(successful)) if successful and total["total_tariff_usd"] is not None else None,
        # fully_loaded включает неуспешные попытки, фоновые карточки и подготовку
        # в других апдейтах. Это амортизированная величина, не цена одного клика.
        cost_scope="all_instrumented_attempts_including_failures_background_and_preparation",
        forecast_30_days_tariff_usd=forecast,
        forecast_status="estimate_at_observed_rate_not_invoice" if forecast is not None else "insufficient_measured_usage_or_tariffs",
        background=_totals([r for r in production if r.get("origin") == "background"]),
        unattributed=_totals([r for r in production if r.get("origin") == "unattributed"]),
        benchmark_excluded=_totals(benchmark),
        migration_estimates=migration_estimates(production, days),
    )
