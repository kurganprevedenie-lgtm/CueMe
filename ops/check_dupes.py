"""Только чтение. Склеиваются ли одни и те же сообщения из JSON и Business
у контакта (иначе в анализе они считаются дважды).

    venv/bin/python /root/check_dupes.py 16698
"""
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/CueMe")
from storage import _normalize_msg_date, _IMPORT_TZ  # noqa: E402

cid = int(sys.argv[1])
db = sqlite3.connect("file:/root/CueMe/bot.db?mode=ro", uri=True)
imp = db.execute("SELECT date, direction, text FROM imported_messages WHERE contact_id=?", (cid,)).fetchall()
biz = db.execute("""SELECT bm.date, bm.direction, bm.text FROM business_messages bm JOIN business_chat_refs r
                    ON r.chat_ref=bm.chat_ref AND r.owner_user_id=bm.owner_user_id WHERE r.contact_id=?""", (cid,)).fetchall()
print(f"JSON: {len(imp)}, {min(r[0] for r in imp)} — {max(r[0] for r in imp)}")
print(f"Business: {len(biz)}, {min(r[0] for r in biz)} — {max(r[0] for r in biz)}")
print("пример JSON:", imp[-1][:2], repr(imp[-1][2][:40]))
print("пример Business:", biz[-1][:2], repr(biz[-1][2][:40]))

imp_n = [(datetime.fromisoformat(_normalize_msg_date(d, _IMPORT_TZ)), dr, (t or "").strip()) for d, dr, t in imp]
biz_n = [(datetime.fromisoformat(_normalize_msg_date(d, timezone.utc)), dr, (t or "").strip()) for d, dr, t in biz]
lo, hi = min(b[0] for b in biz_n), max(i[0] for i in imp_n)
overlap = [b for b in biz_n if lo <= b[0] <= hi]
print(f"\nBusiness-сообщений в периоде, который есть и в JSON: {len(overlap)}")
by_text = {}
for d, dr, t in imp_n:
    by_text.setdefault((dr, t), []).append(d)
shifts = Counter()
for d, dr, t in overlap:
    cands = by_text.get((dr, t), [])
    if not cands:
        shifts["нет такого текста в JSON"] += 1
        continue
    best = min(cands, key=lambda x: abs(x - d))
    diff = round((best - d).total_seconds())
    shifts["точно совпало" if diff == 0 else f"сдвиг {diff} с" if abs(diff) < 3600 * 6 else "далеко"] += 1
for k, v in shifts.most_common(10):
    print(f"  {k}: {v}")
