"""Сообщение тем, кто подтвердил подписку на канал (gate_check_ok), но ответа
после этого так и не получил (баг 09.10: контекст терялся при перезапуске,
две копии бота одновременно).

    cd /root/CueMe && venv/bin/python /root/notify.py           # только список и текст
    cd /root/CueMe && venv/bin/python /root/notify.py --send    # отправить

Кому: есть gate_check_ok, после него нет ни одной генерации (gen_* события,
обновлённый кэш анализа/свидания),
бот не заблокирован, бесплатные попытки в настоящей базе открыты (иначе
обещать нечего). Кому уже отправляли — пропускает (событие gate_apology_sent).
Требует, чтобы перенос из старой базы был сделан (merge_old_db_applied).
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/root/CueMe")
from config import BOT_TOKEN  # noqa: E402

DB = "/root/CueMe/bot.db"
SEND = "--send" in sys.argv
GEN = ("gen_reply_variants", "gen_live", "gen_live_regen", "gen_deep_analysis", "gen_ideal_date")

db = sqlite3.connect(f"file:{DB}?mode={'rw' if SEND else 'ro'}", uri=True, timeout=30)
db.row_factory = sqlite3.Row

if not db.execute("SELECT 1 FROM events WHERE event_type='merge_old_db_applied'").fetchone():
    sys.exit("Перенос из старой базы ещё не сделан — сначала /root/merge.py --apply, иначе часть юзеров не видна.")


def text_for(r) -> str:
    reply = max(0, (r["reply_trial_bonus"] or 0) - (r["trial_used"] or 0))
    analysis = max(0, (r["analysis_trial_bonus"] or 0) - (r["analysis_trial_used"] or 0))
    date = max(0, (r["date_trial_bonus"] or 0) - (r["date_trial_used"] or 0))
    return (
        "Подписка на канал засчитана ✅\n\n"
        "Бесплатные попытки уже открыты:\n"
        f"💬 «Ответить за меня» — {reply}\n"
        f"🔬 «Анализ собеседника» — {analysis}\n"
        f"💡 «Идеальное свидание» — {date}\n\n"
        "Прости, в прошлый раз ответ до тебя не дошёл. Пришли сообщение собеседника — отвечу сразу."
    )


rows = db.execute(f"""
    SELECT u.*, ok.ok_ts FROM users u
    JOIN (SELECT user_telegram_id tid, MIN(ts) ok_ts FROM events
          WHERE event_type = 'gate_check_ok' GROUP BY user_telegram_id) ok ON ok.tid = u.telegram_id
    WHERE COALESCE(u.blocked_bot, 0) = 0
      AND (COALESCE(u.reply_trial_bonus, 0) + COALESCE(u.analysis_trial_bonus, 0) + COALESCE(u.date_trial_bonus, 0)) > 0
      AND NOT EXISTS (SELECT 1 FROM events e WHERE e.user_telegram_id = u.telegram_id AND e.ts >= ok.ok_ts
                      AND e.event_type IN ({",".join("?" * len(GEN))}))
      AND NOT EXISTS (SELECT 1 FROM events e WHERE e.user_telegram_id = u.telegram_id AND e.event_type = 'gate_apology_sent')
      AND NOT EXISTS (SELECT 1 FROM deep_analysis x JOIN contacts c ON c.id = x.contact_id
                      WHERE c.user_telegram_id = u.telegram_id AND x.updated_at >= ok.ok_ts)
      AND NOT EXISTS (SELECT 1 FROM ideal_date x JOIN contacts c ON c.id = x.contact_id
                      WHERE c.user_telegram_id = u.telegram_id AND x.updated_at >= ok.ok_ts)
    ORDER BY ok.ok_ts
""", GEN).fetchall()

print(f"Получателей: {len(rows)}")
for r in rows:
    print(f"  {r['telegram_id']}  подтвердил подписку {r['ok_ts'][:19]}")
if rows:
    print("\nТекст (на примере первого):\n" + "-" * 40 + f"\n{text_for(rows[0])}\n" + "-" * 40)

if not SEND:
    print("\nЭто только просмотр. Отправить: добавь --send")
    sys.exit(0)

sent = failed = 0
for r in rows:
    tid = r["telegram_id"]
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": int(tid), "text": text_for(r)}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = json.loads(resp.read()).get("ok")
    except urllib.error.HTTPError as e:
        ok = False
        if e.code == 403:
            db.execute("UPDATE users SET blocked_bot = 1 WHERE telegram_id = ?", (tid,))
    except Exception:
        ok = False
    if ok:
        sent += 1
        db.execute("INSERT INTO events (ts, user_telegram_id, event_type, meta) VALUES (?, ?, 'gate_apology_sent', '')", (datetime.now(timezone.utc).isoformat(), tid))
    else:
        failed += 1
    db.commit()
    time.sleep(0.1)

print(f"\nОтправлено: {sent}, не удалось: {failed}")
