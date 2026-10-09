"""Что старая копия бота (домашний сервер, 2026-10-09) записала в свою базу
и чего нет в настоящей. ТОЛЬКО ЧТЕНИЕ: обе базы открываются mode=ro,
ничего не пишется и не меняется.

Запуск на новом сервере: venv/bin/python compare_old_db.py
Пути: старая — /root/old.db, настоящая — /root/CueMe/bot.db.
Граница «сегодня»: CUTOFF (UTC), с запасом до начала конфликта (08:01 UTC).
"""
import sqlite3
import sys

OLD = sys.argv[1] if len(sys.argv) > 1 else "/root/old.db"
NEW = sys.argv[2] if len(sys.argv) > 2 else "/root/CueMe/bot.db"
CUTOFF = "2026-10-09T07:30:00"

db = sqlite3.connect(f"file:{NEW}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
db.execute(f"ATTACH DATABASE 'file:{OLD}?mode=ro' AS old")


def tables(schema: str) -> set[str]:
    return {r[0] for r in db.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def cols(schema: str, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")}


def q(sql: str, *args):
    return db.execute(sql, args).fetchall()


def head(title: str) -> None:
    print(f"\n=== {title} ===")


old_t, new_t = tables("old"), tables("main")
print(f"Старая база: {OLD}\nНастоящая:  {NEW}\nГраница:    {CUTOFF} UTC")
for name, schema in (("старой", "old"), ("настоящей", "main")):
    n, mx = q(f"SELECT COUNT(*), MAX(created_at) FROM {schema}.users")[0]
    print(f"Юзеров в {name}: {n}, последний created_at: {mx}")

head("1. Юзеры, которых нет в настоящей базе")
rows = q("SELECT telegram_id, created_at FROM old.users WHERE telegram_id NOT IN (SELECT telegram_id FROM main.users) ORDER BY created_at")
print(f"всего: {len(rows)}")
for r in rows[:15]:
    print(f"  {r['telegram_id']}  создан {r['created_at']}")
if len(rows) > 15:
    print(f"  … и ещё {len(rows) - 15}")

head("2. Юзеры, у которых в старой базе значения больше/другие")
COMPARE = [
    "trial_used", "analysis_trial_used", "date_trial_used",
    "reply_trial_bonus", "analysis_trial_bonus", "date_trial_bonus",
    "promo_channel_reward_claimed", "stars_premium_until",
    "gender", "acquisition_source", "blocked_bot",
]
common = cols("old", "users") & cols("main", "users")
for c in COMPARE:
    if c not in common:
        print(f"  {c}: нет в одной из баз — пропущено")
        continue
    n = q(f"""SELECT COUNT(*) FROM old.users o JOIN main.users m USING(telegram_id)
              WHERE o.{c} IS NOT NULL AND (m.{c} IS NULL OR o.{c} > m.{c})""")[0][0]
    if n:
        ids = [r[0] for r in q(f"""SELECT o.telegram_id FROM old.users o JOIN main.users m USING(telegram_id)
              WHERE o.{c} IS NOT NULL AND (m.{c} IS NULL OR o.{c} > m.{c}) LIMIT 5""")]
        print(f"  {c}: {n} юзеров (например {', '.join(ids)})")
    else:
        print(f"  {c}: расхождений нет")

head("3. Сообщения переписок (business_messages)")
total_today = q("SELECT COUNT(*) FROM old.business_messages WHERE date >= ?", CUTOFF)[0][0]
missing = q("""SELECT COUNT(*) FROM old.business_messages o WHERE o.date >= ? AND NOT EXISTS (
                 SELECT 1 FROM main.business_messages m WHERE m.connection_id = o.connection_id
                 AND m.chat_ref = o.chat_ref AND m.tg_message_id = o.tg_message_id)""", CUTOFF)[0][0]
owners = q("""SELECT COUNT(DISTINCT o.owner_user_id) FROM old.business_messages o WHERE o.date >= ? AND NOT EXISTS (
                 SELECT 1 FROM main.business_messages m WHERE m.connection_id = o.connection_id
                 AND m.chat_ref = o.chat_ref AND m.tg_message_id = o.tg_message_id)""", CUTOFF)[0][0]
print(f"в старой с границы: {total_today}, из них нет в настоящей: {missing} (у {owners} владельцев)")

head("4. Контакты, которых нет в настоящей базе")
n = q("""SELECT COUNT(*) FROM old.contacts o WHERE NOT EXISTS (SELECT 1 FROM main.contacts m
         WHERE m.user_telegram_id = o.user_telegram_id AND m.original_from_id = o.original_from_id)""")[0][0]
print(f"всего: {n}")

head("5. Подключения Автоматизации чатов")
n_new = q("SELECT COUNT(*) FROM old.business_connections WHERE connection_id NOT IN (SELECT connection_id FROM main.business_connections)")[0][0]
n_diff = q("""SELECT COUNT(*) FROM old.business_connections o JOIN main.business_connections m USING(connection_id)
              WHERE o.is_enabled != m.is_enabled""")[0][0]
print(f"новых: {n_new}, с другим статусом вкл/выкл: {n_diff}")

head("6. Оплаты Stars")
if "star_payments" in old_t and "star_payments" in new_t:
    rows = q("""SELECT telegram_id, tier, stars_amount, created_at FROM old.star_payments
                WHERE charge_id NOT IN (SELECT charge_id FROM main.star_payments) ORDER BY created_at""")
    print(f"нет в настоящей: {len(rows)}")
    for r in rows:
        print(f"  !!! {r['telegram_id']}  {r['tier']}  {r['stars_amount']}⭐  {r['created_at']}")
else:
    print("таблицы star_payments нет в одной из баз")

head("7. Рефералы")
if "referrals" in old_t:
    n = q("SELECT COUNT(*) FROM old.referrals WHERE referred_telegram_id NOT IN (SELECT referred_telegram_id FROM main.referrals)")[0][0]
    print(f"нет в настоящей: {n}")

head("8. События в старой базе с границы (по типам)")
if "events" in old_t:
    for r in q("SELECT event_type, COUNT(*) n FROM old.events WHERE ts >= ? GROUP BY event_type ORDER BY n DESC", CUTOFF):
        print(f"  {r['event_type']}: {r['n']}")

print("\nГотово. Ничего не изменено.")
