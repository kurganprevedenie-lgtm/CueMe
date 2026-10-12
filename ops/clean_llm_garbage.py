"""Разовая чистка сохранённых ответов модели, не прошедших проверку
(llm.validate_llm_output: мало кириллицы / рассуждения модели), — инцидент
2026-10-11. Запускать ПОСЛЕ деплоя исправления (берёт проверку из llm.py).

    cd /root/CueMe && venv/bin/python ops/clean_llm_garbage.py               # только показать
    cd /root/CueMe && venv/bin/python ops/clean_llm_garbage.py --apply       # удалить (с копией базы)
    cd /root/CueMe && venv/bin/python ops/clean_llm_garbage.py --apply --regenerate
        # + заново собрать «Идеальное свидание» пострадавшим (за 7 дней) и
        #   прислать с извинением; попытки не списываются

Кэши пересобираются сами при следующем открытии — удаление безопасно.
"""
import asyncio
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/CueMe")
from config import BOT_TOKEN  # noqa: E402
from llm import validate_llm_output  # noqa: E402

DB = "/root/CueMe/bot.db"
APPLY = "--apply" in sys.argv
REGEN = "--regenerate" in sys.argv

# таблица → (функция, ключ, колонки с текстом)
TABLES = {
    "ideal_date": ("Идеальное свидание", "contact_id", ["date_idea", "gift_ideas"]),
    "deep_analysis": ("Анализ собеседника", "contact_id", ["dynamics_text", "synthesis_text", "advice_text", "message_text", "metrics_json"]),
    "style_cards": ("Карточка стиля", "user_telegram_id", ["card_text"]),
    "interaction_cards": ("Карточка собеседника", "contact_id", ["card_text"]),
    "my_style_per_contact": ("Мой стиль с контактом", "contact_id", ["card_text"]),
    "llm_cache": ("Кэш вариантов ответа / живого диалога", "cache_key", ["result"]),
}


def strings_of(value: str) -> str:
    """JSON-поля (metrics_json, llm_cache) — только строковые значения, без ключей."""
    v = (value or "").strip()
    if not v.startswith(("{", "[")):
        return v
    try:
        data = json.loads(v)
    except ValueError:
        return v
    out = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(data)
    return "\n".join(out)


def tg_username(tid: str) -> str:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={tid}", timeout=10) as r:
            u = json.loads(r.read()).get("result", {}).get("username")
        return f"@{u}" if u else "—"
    except Exception:
        return "?"


def send(tid: str, text: str) -> bool:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": int(tid), "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def main() -> None:
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    found: dict[str, list] = {}
    for table, (label, key, columns) in TABLES.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        cols = [c for c in columns if c in have]
        if not cols:
            continue
        extra = ", updated_at" if "updated_at" in have else ""
        for r in db.execute(f"SELECT {key}, {', '.join(cols)}{extra} FROM {table}"):
            text = "\n".join(strings_of(r[c]) for c in cols)
            reason = validate_llm_output(text)
            if reason and reason != "пустой ответ":
                found.setdefault(table, []).append((r[key], reason, r["updated_at"] if extra else "", text[:100]))

    print("Найдено невалидных сохранённых ответов:")
    for table, (label, _, _) in TABLES.items():
        print(f"  {label} ({table}): {len(found.get(table, []))}")

    victims = []
    for cid, reason, updated, preview in found.get("ideal_date", []):
        owner = db.execute("SELECT user_telegram_id, display_name FROM contacts WHERE id = ?", (cid,)).fetchone()
        tid = owner["user_telegram_id"] if owner else "?"
        print(f"\n  свидание: контакт {cid} ({owner['display_name'] if owner else '?'}), владелец {tid} {tg_username(tid)}, "
              f"сохранено {str(updated)[:19]}, причина: {reason}\n    «{preview.replace(chr(10), ' ')}»")
        victims.append((cid, tid, owner["display_name"] if owner else "", updated))
    for table in ("deep_analysis", "style_cards", "interaction_cards", "my_style_per_contact", "llm_cache"):
        for k, reason, updated, preview in found.get(table, [])[:5]:
            print(f"  {table}: {str(k)[:24]} — {reason} — «{preview.replace(chr(10), ' ')[:70]}»")

    if not APPLY:
        print("\nЭто только просмотр. Удалить: --apply (с --regenerate — ещё и пересобрать свидание пострадавшим).")
        return

    backup = f"/root/bot_before_clean_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.db"
    dst = sqlite3.connect(backup)
    db.backup(dst)
    dst.close()
    print(f"\nКопия базы: {backup}")
    with db:
        for table, rows in found.items():
            key = TABLES[table][1]
            for k, *_ in rows:
                db.execute(f"DELETE FROM {table} WHERE {key} = ?", (k,))
            print(f"  удалено из {table}: {len(rows)}")

    if not REGEN:
        return
    import main as bot_main  # noqa: E402 — функции генерации бота, без запуска самого бота
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for cid, tid, name, updated in victims:
        if tid == "?" or (updated and str(updated) < week_ago):
            continue
        try:
            data = asyncio.run(bot_main._gen_ideal_date(cid, tid))
        except Exception as e:
            print(f"  {tid}: пересобрать не вышло ({type(e).__name__}) — напиши вручную")
            continue
        if not data:
            print(f"  {tid}: мало данных для идеи — пропущен")
            continue
        text = ("Прости, в прошлый раз идея свидания пришла с ошибкой — вот нормальная 👇\n\n"
                + bot_main._format_ideal_date(name or "собеседника", data))
        print(f"  {tid}: {'отправлено' if send(tid, text) else 'НЕ отправлено'}")


if __name__ == "__main__":
    main()
