"""Пострадавшие от бага «Анализ собеседника падает после загрузки JSON»
(2026-10-14). Запускать ПОСЛЕ деплоя исправления.

    cd /root/CueMe && venv/bin/python ops/fix_deep_analysis.py
        # список: у кого есть контакты и из JSON, и из Business (только чтение)
    cd /root/CueMe && venv/bin/python ops/fix_deep_analysis.py --user 123456789
        # пересчитать анализ его смешанных контактов и ПОКАЗАТЬ, что уйдёт
    cd /root/CueMe && venv/bin/python ops/fix_deep_analysis.py --user 123456789 --send
        # то же + отправить ему с извинением

Пишет только в deep_analysis (кэш анализа). Попытки не списываются.
"""
import asyncio
import os
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # DB_PATH бота относительный
from config import BOT_TOKEN  # noqa: E402

DB = ROOT / "bot.db"
SEND = "--send" in sys.argv
USER = sys.argv[sys.argv.index("--user") + 1] if "--user" in sys.argv else None
APOLOGY = "Прости, анализ раньше не собирался из-за ошибки на нашей стороне — уже починили. Вот он 👇"


def tg(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def username(tid: str) -> str:
    u = tg("getChat", {"chat_id": int(tid)}).get("result", {}).get("username")
    return f"@{u}" if u else "—"


def mixed_contacts(db, owner: str | None = None):
    q = """
        SELECT c.id, c.user_telegram_id AS owner, c.display_name,
               (SELECT COUNT(*) FROM imported_messages i WHERE i.contact_id = c.id) AS n_json,
               (SELECT COUNT(*) FROM business_messages bm JOIN business_chat_refs r
                  ON r.chat_ref = bm.chat_ref AND r.owner_user_id = bm.owner_user_id
                WHERE r.contact_id = c.id) AS n_biz,
               (SELECT updated_at FROM deep_analysis d WHERE d.contact_id = c.id) AS analysis_at
        FROM contacts c
        WHERE EXISTS (SELECT 1 FROM imported_messages i WHERE i.contact_id = c.id)
          AND EXISTS (SELECT 1 FROM business_chat_refs r WHERE r.contact_id = c.id)
    """
    args = ()
    if owner:
        q += " AND c.user_telegram_id = ?"
        args = (owner,)
    return [r for r in db.execute(q, args) if r["n_biz"] > 0]


def failed_events(db) -> dict[str, int]:
    rows = db.execute("SELECT user_telegram_id, COUNT(*) n FROM events WHERE event_type = 'gen_deep_analysis_failed' "
                      "GROUP BY user_telegram_id").fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> None:
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if not USER:
        rows = mixed_contacts(db)
        fails = failed_events(db)
        owners: dict[str, list] = {}
        for r in rows:
            owners.setdefault(r["owner"], []).append(r)
        print(f"Контактов и из JSON, и из Business: {len(rows)}, у юзеров: {len(owners)}")
        print("(до исправления «Анализ собеседника» падал на каждом таком контакте)\n")
        for owner, cs in sorted(owners.items(), key=lambda kv: -sum(c["n_json"] for c in kv[1])):
            print(f"{owner} {username(owner)}  ошибок анализа после деплоя: {fails.get(owner, 0)}")
            for c in cs:
                print(f"    контакт {c['id']} «{c['display_name']}»: JSON {c['n_json']}, Business {c['n_biz']}, "
                      f"анализ в кэше: {c['analysis_at'] or 'нет'}")
        print("\nДальше: --user <telegram_id> — пересчитать и показать, что уйдёт.")
        return

    import main as bot  # noqa: E402 — функции бота, без запуска самого бота
    contacts = mixed_contacts(db, USER)
    db.close()
    print(f"{USER} {username(USER)}: смешанных контактов {len(contacts)}")
    for c in contacts:
        bot.delete_deep_analysis(c["id"])  # свежий пересчёт, а не старый кэш
        data = asyncio.run(bot._gen_deep_analysis(c["id"], USER))
        if not data:
            print(f"  контакт {c['id']}: мало данных — пропущен")
            continue
        metrics = json.loads(data["metrics_json"])
        name = c["display_name"] or "собеседника"
        text = bot._format_deep_analysis_text(name, metrics, data["dynamics_text"], data["synthesis_text"],
                                               data["advice_text"], data["message_text"])
        status = "без выводов (LLM не ответила)" if data.get("degraded") else "полный"
        print(f"\n  контакт {c['id']} «{name}»: анализ {status}, {len(text)} символов")
        print("  ── что уйдёт ──\n" + APOLOGY + "\n\n" + text[:1500] + ("\n  …" if len(text) > 1500 else ""))
        if SEND:
            ok = tg("sendMessage", {"chat_id": int(USER), "text": APOLOGY}).get("ok")
            for chunk in bot._split_long_text(text):
                ok = tg("sendMessage", {"chat_id": int(USER), "text": chunk, "parse_mode": "HTML"}).get("ok") and ok
            print(f"  отправлено: {'да' if ok else 'НЕТ — проверь вручную'}")
    if not SEND:
        print("\nЭто только просмотр. Отправить: добавь --send")


if __name__ == "__main__":
    main()
