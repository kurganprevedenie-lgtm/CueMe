"""Расследование «разблокировал гейт, но не получил ответ». ТОЛЬКО ЧТЕНИЕ.

Смотрит обе базы: настоящую (/root/CueMe/bot.db) и базу старой копии бота
(/root/old.db, если есть) — 09.10 с 10:01 CEST до её остановки две копии
работали одновременно, и события одного юзера могли разойтись по двум базам
(а «Я подписался» и «Показать» — попасть в РАЗНЫЕ копии).

    cd /root/CueMe && venv/bin/python /root/investigate.py [telegram_id]
"""
import json
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, "/root/CueMe")

USER = sys.argv[1] if len(sys.argv) > 1 else "6629057268"
DBS = [("настоящая", "/root/CueMe/bot.db"), ("старая", "/root/old.db")]
GEN = ("gen_reply_variants", "gen_live", "gen_live_regen")


def open_ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


dbs = [(name, open_ro(p)) for name, p in DBS if os.path.isfile(p)]
print("Базы:", ", ".join(f"{n} ({p})" for (n, p) in DBS if os.path.isfile(p)))
merged = any(db.execute("SELECT 1 FROM events WHERE event_type='merge_old_db_applied'").fetchone() for _, db in dbs)
print("Перенос из старой базы уже сделан:", "да" if merged else "нет")


def cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def tg_username(tid):
    try:
        from config import BOT_TOKEN
        with urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={tid}", timeout=10) as r:
            d = json.loads(r.read())
        u = d.get("result", {}).get("username")
        return f"@{u}" if u else "—"
    except Exception:
        return "?"


# ── 1. Хронология юзера ───────────────────────────────────────────────────────
print(f"\n=============== 1. Юзер {USER} ({tg_username(USER)}) ===============")
for name, db in dbs:
    print(f"\n--- база: {name}")
    ucols = cols(db, "users")
    want = [c for c in ("created_at", "gender", "acquisition_source", "trial_used", "analysis_trial_used",
                        "date_trial_used", "reply_trial_bonus", "analysis_trial_bonus", "date_trial_bonus",
                        "promo_channel_reward_claimed", "blocked_bot", "last_action", "last_action_at") if c in ucols]
    row = db.execute(f"SELECT {', '.join(want)} FROM users WHERE telegram_id = ?", (USER,)).fetchone()
    if not row:
        print("  в этой базе юзера нет")
        continue
    for k in want:
        print(f"  {k}: {row[k]}")
    for r in db.execute("SELECT connection_id, is_enabled, can_reply, created_at FROM business_connections "
                        "WHERE owner_user_id = ? ORDER BY created_at", (USER,)):
        print(f"  подключение {r['connection_id'][:10]}…  включено={r['is_enabled']}  создано {r['created_at']}")
    nc = db.execute("SELECT COUNT(*) FROM contacts WHERE user_telegram_id = ?", (USER,)).fetchone()[0]
    nm = db.execute("SELECT COUNT(*), MAX(date) FROM business_messages WHERE owner_user_id = ?", (USER,)).fetchone()
    print(f"  контактов: {nc}, сообщений переписок: {nm[0]} (последнее {nm[1]})")
    for t in ("deep_analysis", "ideal_date"):
        if "updated_at" in cols(db, t):
            r = db.execute(f"SELECT COUNT(*), MAX(x.updated_at) FROM {t} x JOIN contacts c ON c.id = x.contact_id "
                           "WHERE c.user_telegram_id = ?", (USER,)).fetchone()
            print(f"  {t}: {r[0]} шт., последний {r[1]}")
    print("  события:")
    for r in db.execute("SELECT ts, event_type, meta FROM events WHERE user_telegram_id = ? ORDER BY ts", (USER,)):
        mark = "  <<< ГЕЙТ" if r["event_type"].startswith("gate_") else ""
        print(f"    {r['ts'][:19]}  {r['event_type']:22} {r['meta'] or ''}{mark}")

# ── 3. Баг-класс по всем юзерам ───────────────────────────────────────────────
print("\n=============== 3. Подтвердили подписку, но генерации после этого не видно ===============")
print("Признак генерации: событие gen_reply_variants/gen_live/gen_live_regen после подтверждения, либо")
print("кэш анализа/свидания обновлён после него, либо счётчик потраченных попыток > 0.")
print("(Отдельного события генерации анализа/свидания в боте нет — поэтому по кэшу и счётчикам.)\n")

users: dict[str, dict] = {}
for name, db in dbs:
    for r in db.execute("SELECT user_telegram_id tid, MIN(ts) ok_ts FROM events WHERE event_type='gate_check_ok' "
                        "AND user_telegram_id IS NOT NULL GROUP BY user_telegram_id"):
        u = users.setdefault(r["tid"], {"ok_ts": r["ok_ts"], "src": set()})
        u["ok_ts"] = min(u["ok_ts"], r["ok_ts"])
        u["src"].add(name)

groups: dict[str, list] = {}
for tid, u in sorted(users.items(), key=lambda kv: kv[1]["ok_ts"]):
    ok = u["ok_ts"]
    gen_after = reveal_after = 0
    kinds, last_action, last_at, used = set(), None, None, 0
    for name, db in dbs:
        gen_after += db.execute(f"SELECT COUNT(*) FROM events WHERE user_telegram_id=? AND ts>=? AND event_type IN "
                                f"({','.join('?' * len(GEN))})", (tid, ok, *GEN)).fetchone()[0]
        for r in db.execute("SELECT meta FROM events WHERE user_telegram_id=? AND ts>=? AND event_type='gate_reveal'", (tid, ok)):
            reveal_after += 1
            kinds.add(r["meta"])
        for r in db.execute("SELECT meta FROM events WHERE user_telegram_id=? AND event_type='gate_check_ok'", (tid,)):
            kinds.add(r["meta"])
        for t in ("deep_analysis", "ideal_date"):
            if "updated_at" in cols(db, t):
                gen_after += db.execute(f"SELECT COUNT(*) FROM {t} x JOIN contacts c ON c.id = x.contact_id "
                                        "WHERE c.user_telegram_id=? AND x.updated_at>=?", (tid, ok)).fetchone()[0]
        uc = cols(db, "users")
        row = db.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        if row:
            used = max([used] + [row[c] or 0 for c in ("analysis_trial_used", "date_trial_used") if c in uc])
            if "last_action_at" in uc and row["last_action_at"] and (not last_at or row["last_action_at"] > last_at):
                last_at, last_action = row["last_action_at"], row["last_action"]
    if gen_after or used:
        reason = "генерация есть"
    elif not reveal_after:
        reason = "не нажал «Показать»"
    else:
        reason = "нажал «Показать», генерации не видно"
    groups.setdefault(reason, []).append((tid, ok, last_action, last_at, ",".join(sorted(kinds)), ",".join(sorted(u["src"]))))

for reason, rows in groups.items():
    print(f"\n--- {reason}: {len(rows)}")
    if reason == "генерация есть":
        continue
    for tid, ok, la, lat, kinds, src in rows:
        print(f"  {tid} {tg_username(tid):18} подтвердил {ok[:19]}  что: {kinds or '?'}  база: {src}  "
              f"последнее действие: {la or '—'} в {(lat or '—')[:19]}")

print("\nИтого подтвердивших подписку:", len(users))
for reason, rows in groups.items():
    print(f"  {reason}: {len(rows)}")
print("\nГотово. Ничего не изменено.")
