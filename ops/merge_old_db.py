"""Перенос в настоящую базу того, что старая копия бота (домашний сервер)
записала 2026-10-09, пока две копии работали одновременно.

По умолчанию — ПРОБНЫЙ прогон: настоящая база копируется в
/root/merge_preview.db, перенос делается в копию, печатается, что изменилось.
Настоящая база при этом не трогается.

    cd /root/CueMe && venv/bin/python /root/merge.py            # пробно, на копии
    cd /root/CueMe && venv/bin/python /root/merge.py --apply    # по-настоящему

--apply работает только при остановленном боте (проверяет сам) и перед
переносом делает свежую копию настоящей базы рядом.

Что переносится (только добавление, существующее не перезаписывается):
  юзеры, которых нет; контакты (с новыми id) и их привязки к чатам;
  сообщения переписок с границы; новые подключения Автоматизации чатов;
  оплаты Stars; события с границы.
Что обновляется у существующих юзеров (берётся большее из двух значений,
ничего не уменьшается): бонусные попытки, отметка «награда за канал
получена», счётчики попыток, срок Premium по Stars. Пол и источник — только
если в настоящей базе пусто. Статус подключений вкл/выкл — по живому ответу
Telegram (getBusinessConnection), а не по одной из баз.
Не переносятся кэши (карточки стиля, анализы, свидания) — пересоберутся сами.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/root/CueMe")

OLD = "/root/old.db"
REAL = "/root/CueMe/bot.db"
PREVIEW = "/root/merge_preview.db"
CUTOFF = "2026-10-09T07:30:00"
APPLY = "--apply" in sys.argv

MAX_COLS = [
    "reply_trial_bonus", "analysis_trial_bonus", "date_trial_bonus",
    "promo_channel_reward_claimed", "trial_used", "analysis_trial_used",
    "date_trial_used", "stars_premium_until",
]
FILL_IF_EMPTY = ["gender", "acquisition_source"]


def sqlite_copy(src: str, dst: str) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def cols(db, schema, table):
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def common(db, table, skip=("id",)):
    old_c = set(cols(db, "old", table))
    return [c for c in cols(db, "main", table) if c in old_c and c not in skip]


def tg_connection_enabled(token: str, conn_id: str) -> bool | None:
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getBusinessConnection",
            data=json.dumps({"business_connection_id": conn_id}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return bool(data["result"]["is_enabled"]) if data.get("ok") else None
    except Exception:
        return None


def main() -> None:
    if APPLY:
        if subprocess.run(["pgrep", "-f", "CueMe/venv/bin/python main.py"], capture_output=True).stdout.strip():
            sys.exit("Бот работает. Сначала: systemctl stop cueme-bot")
        backup = f"/root/bot_before_merge_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.db"
        sqlite_copy(REAL, backup)
        print(f"Копия настоящей базы перед переносом: {backup}")
        target = REAL
    else:
        sqlite_copy(REAL, PREVIEW)
        target = PREVIEW
        print(f"ПРОБНЫЙ прогон на копии: {PREVIEW} (настоящая база не меняется)")

    # URI-режим нужен, чтобы ATTACH ниже открыл старую базу только на чтение.
    db = sqlite3.connect(f"file:{target}", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute(f"ATTACH DATABASE 'file:{OLD}?mode=ro' AS old")
    # Второй --apply задублировал бы события (у них нет ключа) — отметка в events.
    if db.execute("SELECT 1 FROM main.events WHERE event_type = 'merge_old_db_applied'").fetchone():
        sys.exit("Перенос в эту базу уже делался (есть отметка merge_old_db_applied). Повторно не запускаю.")
    report = []

    def done(label, n):
        report.append(f"  {label}: {n}")

    with db:
        # 1. Юзеры, которых нет
        c = common(db, "users", skip=())
        cl = ", ".join(c)
        n = db.execute(f"INSERT OR IGNORE INTO main.users ({cl}) SELECT {cl} FROM old.users "
                       "WHERE telegram_id NOT IN (SELECT telegram_id FROM main.users)").rowcount
        done("добавлено юзеров", n)

        # 2. Контакты с новыми id + карта старый id → новый
        c = common(db, "contacts")
        cl = ", ".join(c)
        n = db.execute(f"""INSERT OR IGNORE INTO main.contacts ({cl}) SELECT {cl} FROM old.contacts o
                           WHERE NOT EXISTS (SELECT 1 FROM main.contacts m WHERE m.user_telegram_id = o.user_telegram_id
                           AND m.original_from_id = o.original_from_id)""").rowcount
        done("добавлено контактов", n)
        id_map = {r[0]: r[1] for r in db.execute(
            """SELECT o.id, m.id FROM old.contacts o JOIN main.contacts m
               ON m.user_telegram_id = o.user_telegram_id AND m.original_from_id = o.original_from_id""")}

        # 3. Привязки чатов к контактам (только те, которых в настоящей нет)
        added = 0
        for r in db.execute("""SELECT owner_user_id, chat_ref, contact_id FROM old.business_chat_refs o
                               WHERE NOT EXISTS (SELECT 1 FROM main.business_chat_refs m
                               WHERE m.owner_user_id = o.owner_user_id AND m.chat_ref = o.chat_ref)""").fetchall():
            new_cid = id_map.get(r["contact_id"])
            if new_cid is None:
                continue
            added += db.execute("INSERT OR IGNORE INTO main.business_chat_refs (owner_user_id, chat_ref, contact_id) "
                                "VALUES (?, ?, ?)", (r["owner_user_id"], r["chat_ref"], new_cid)).rowcount
        done("добавлено привязок чат → контакт", added)

        # 4. Сообщения переписок с границы
        c = common(db, "business_messages")
        cl = ", ".join(c)
        n = db.execute(f"""INSERT INTO main.business_messages ({cl}) SELECT {cl} FROM old.business_messages o
                           WHERE o.date >= ? AND o.tg_message_id IS NOT NULL AND NOT EXISTS (
                           SELECT 1 FROM main.business_messages m WHERE m.connection_id = o.connection_id
                           AND m.chat_ref = o.chat_ref AND m.tg_message_id = o.tg_message_id)""", (CUTOFF,)).rowcount
        done("добавлено сообщений переписок", n)

        # 5. Подключения Автоматизации чатов: новые + статус по Telegram
        c = common(db, "business_connections", skip=())
        cl = ", ".join(c)
        n = db.execute(f"INSERT OR IGNORE INTO main.business_connections ({cl}) SELECT {cl} FROM old.business_connections "
                       "WHERE connection_id NOT IN (SELECT connection_id FROM main.business_connections)").rowcount
        done("добавлено подключений", n)
        try:
            from config import BOT_TOKEN
        except Exception:
            BOT_TOKEN = None
        to_check = [r[0] for r in db.execute(
            """SELECT o.connection_id FROM old.business_connections o JOIN main.business_connections m
               USING(connection_id) WHERE o.is_enabled != m.is_enabled""")]
        fixed = unknown = 0
        for cid in to_check:
            live = tg_connection_enabled(BOT_TOKEN, cid) if BOT_TOKEN else None
            if live is None:
                unknown += 1
                continue
            fixed += db.execute("UPDATE main.business_connections SET is_enabled = ? WHERE connection_id = ? "
                                "AND is_enabled != ?", (int(live), cid, int(live))).rowcount
        done(f"подключений с разным статусом: {len(to_check)}, исправлено по Telegram", fixed)
        if unknown:
            done("  не удалось узнать у Telegram (оставлено как в настоящей)", unknown)

        # 6. Оплаты Stars
        if "star_payments" in {r[0] for r in db.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}:
            c = common(db, "star_payments")
            cl = ", ".join(c)
            n = db.execute(f"INSERT OR IGNORE INTO main.star_payments ({cl}) SELECT {cl} FROM old.star_payments "
                           "WHERE charge_id NOT IN (SELECT charge_id FROM main.star_payments)").rowcount
            done("добавлено оплат Stars", n)

        # 7. События с границы
        c = common(db, "events")
        cl = ", ".join(c)
        n = db.execute(f"INSERT INTO main.events ({cl}) SELECT {cl} FROM old.events WHERE ts >= ?", (CUTOFF,)).rowcount
        done("добавлено событий", n)

        # 8. Обновление существующих юзеров: большее из двух / заполнить пустое
        user_cols = set(cols(db, "old", "users")) & set(cols(db, "main", "users"))
        for col in MAX_COLS:
            if col not in user_cols:
                continue
            n = db.execute(f"""UPDATE main.users SET {col} = (SELECT o.{col} FROM old.users o WHERE o.telegram_id = main.users.telegram_id)
                               WHERE EXISTS (SELECT 1 FROM old.users o WHERE o.telegram_id = main.users.telegram_id
                               AND o.{col} IS NOT NULL AND (main.users.{col} IS NULL OR o.{col} > main.users.{col}))""").rowcount
            done(f"обновлено {col} (большее значение)", n)
        for col in FILL_IF_EMPTY:
            if col not in user_cols:
                continue
            n = db.execute(f"""UPDATE main.users SET {col} = (SELECT o.{col} FROM old.users o WHERE o.telegram_id = main.users.telegram_id)
                               WHERE main.users.{col} IS NULL AND EXISTS (SELECT 1 FROM old.users o
                               WHERE o.telegram_id = main.users.telegram_id AND o.{col} IS NOT NULL)""").rowcount
            done(f"заполнено пустое {col}", n)

        db.execute("INSERT INTO main.events (ts, user_telegram_id, event_type, meta) VALUES (?, NULL, ?, ?)",
                   (datetime.now(timezone.utc).isoformat(), "merge_old_db_applied", OLD))

    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    users = db.execute("SELECT COUNT(*) FROM main.users").fetchone()[0]
    print("\n".join(report))
    print(f"\nПроверка целостности: {ok}. Юзеров после переноса: {users}.")
    print("Готово." if APPLY else "Это был пробный прогон. Настоящая база не изменена.")


if __name__ == "__main__":
    main()
