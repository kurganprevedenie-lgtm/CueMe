"""Чтение JSONL на сервере: python -m tools.llm_cost_report --days 7.

Вывод агрегированный, без пользовательских ID и текстов. Не делает API-вызовов.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm_usage_report import read_events, build_report


def report_days(days=7, directory=None):
    now = datetime.now(timezone.utc)
    events, broken = read_events(directory)
    result = build_report(events, (now - timedelta(days=days)).isoformat(), now.isoformat())
    result["broken_log_lines"] = broken
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--logs-dir", type=Path)
    args = parser.parse_args()
    if not 0 < args.days <= 366:
        parser.error("days должен быть от 0 до 366")
    print(json.dumps(report_days(args.days, args.logs_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
