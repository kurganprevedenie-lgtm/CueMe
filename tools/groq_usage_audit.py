"""Сервер: python -m tools.groq_usage_audit --month 2026-10

Проверяет ключи через бесплатный GET /models. Это НЕ проверка остатка квоты
генераций. Месячные токены берёт только из локального журнала, не из users.
У обычного ключа нет найденного документированного API исторического Usage.
"""
import argparse
import asyncio
import calendar
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import config  # загрузка .env; секреты не выводим
from llm_usage import usage_dir
from llm_usage_report import read_events, build_report


def month_bounds(month):
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def inventory(keys):
    results = []
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        for number, key in enumerate(dict.fromkeys(keys), 1):
            row = dict(number=number, credential_ref=fingerprint(key))
            try:
                response = await client.get("https://api.groq.com/openai/v1/models",
                                            headers={"Authorization": f"Bearer {key}"})
                row["http_status"] = response.status_code
                if response.is_success:
                    models = {r.get("id") for r in response.json().get("data", [])}
                    row.update(status="ключ принят; квота генераций не проверена",
                               model_120b_visible="openai/gpt-oss-120b" in models)
                elif response.status_code == 401:
                    row["status"] = "ключ не принят (401)"
                else:
                    row["status"] = f"GET /models вернул HTTP {response.status_code}; причина не установлена"
            except Exception as exc:
                row["status"] = type(exc).__name__
            results.append(row)
    return results


def monthly(events, month):
    start, end = month_bounds(month)
    report = build_report(events, start.isoformat(), end.isoformat())
    groups = defaultdict(list)
    for event in events:
        if event.get("type") != "api" or event.get("provider") != "Groq" or event.get("origin") == "benchmark":
            continue
        try:
            ts = datetime.fromisoformat(event.get("started_at") or event["ts"])
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
        except (ValueError, KeyError, TypeError):
            continue
        if start <= ts < end:
            groups[(event.get("credential_ref") or "ключ в старом журнале не указан", event.get("model"))].append(event)
    rows = []
    for (ref, model), attempts in groups.items():
        # Один request_id не учитываем повторно при копировании журнала.
        attempts = list({r["request_id"]: r for r in attempts}.values())
        known = [r for r in attempts if r.get("input_tokens") is not None and r.get("output_tokens") is not None]
        priced = [r for r in attempts if r.get("tariff_cost_usd") is not None]
        rows.append(dict(credential_ref=ref, model=model, attempts=len(attempts),
            measured_attempts=len(known), unknown_usage_attempts=len(attempts) - len(known),
            input_tokens_known=sum(r["input_tokens"] for r in known),
            output_tokens_known=sum(r["output_tokens"] for r in known),
            priced_attempts=len(priced),
            known_tariff_subtotal_usd=sum(r["tariff_cost_usd"] for r in priced) if priced else None,
            all_attempts_tariff_usd=sum(r["tariff_cost_usd"] for r in priced) if len(priced) == len(attempts) else None))
    return dict(month_utc=month, measured_start=report["measured_start"], by_key_model=rows,
                historical_total_status="не восстановлен; журнал может покрывать только часть месяца",
                reason="проверка ключа и текущие rate-limit заголовки не содержат исторический месячный расход")


async def run(args):
    month_bounds(args.month)
    keys = config.GROQ_API_KEYS
    print("== Текущие ключи Groq: без платных генераций ==")
    print(f"В конфигурации {len(keys)}, уникальных {len(set(keys))}")
    for row in await inventory(keys):
        print(f"#{row['number']} [{row['credential_ref']}]: {row['status']}; 120b в каталоге: {row.get('model_120b_visible', 'неизвестно')}")
    events, broken = read_events(args.logs_dir)
    result = monthly(events, args.month)
    print("\n== Сохранённые измерения, а не статистика Groq Console ==")
    print(f"Каталог: {args.logs_dir or usage_dir()}; повреждённых строк: {broken}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["by_key_model"]:
        print("За этот месяц API-токены в журнале не найдены. Это НЕ нулевой расход.")
    days = calendar.monthrange(*map(int, args.month.split("-")))[1]
    print(f"\nТолько сценарий по твоему ориентиру $0.08/день: ${days * .08:.2f} за {days} дней на организацию.")
    print("Количество организаций по ключам не определено. Ключи одной организации нельзя считать отдельными аккаунтами.")
    print("Полный октябрь: Groq Console → Usage → October 2026, по каждой организации/проекту без дублей.")
    print("Для 120b отдельно нужны input, cached input и output; Whisper считать отдельно. Сумма в Free Tier — расчёт по тарифу, не списание.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default="2026-10")
    parser.add_argument("--logs-dir", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except ValueError:
        parser.exit(2, "Месяц должен быть в формате ГГГГ-ММ\n")


if __name__ == "__main__":
    main()
