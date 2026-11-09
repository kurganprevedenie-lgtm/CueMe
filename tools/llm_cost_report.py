"""Чтение JSONL на сервере: python -m tools.llm_cost_report --days 7.

Вывод агрегированный, без пользовательских ID и текстов. Не делает API-вызовов.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv

from llm_usage_report import read_events, build_report


def report_days(days=7, directory=None):
    load_dotenv()
    now = datetime.now(timezone.utc)
    events, broken = read_events(directory)
    result = build_report(events, (now - timedelta(days=days)).isoformat(), now.isoformat())
    result["broken_log_lines"] = broken
    return result


def report_day(day, directory=None, timezone_name="UTC"):
    load_dotenv()
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo(timezone_name))
    end = start + timedelta(days=1)
    events, broken = read_events(directory)
    result = build_report(events, start.isoformat(), end.isoformat())
    result.update(broken_log_lines=broken, calendar_day=day, calendar_timezone=timezone_name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--days", type=float, default=7)
    period.add_argument("--day", help="Конкретная дата ГГГГ-ММ-ДД; по умолчанию UTC, как в Groq")
    parser.add_argument("--timezone", default="UTC", help="Например Europe/Moscow")
    parser.add_argument("--logs-dir", type=Path)
    args = parser.parse_args()
    if not 0 < args.days <= 366:
        parser.error("days должен быть от 0 до 366")
    try:
        result = report_day(args.day, args.logs_dir, args.timezone) if args.day else report_days(args.days, args.logs_dir)
    except (ValueError, ZoneInfoNotFoundError):
        parser.error("Проверь дату ГГГГ-ММ-ДД и часовой пояс")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
