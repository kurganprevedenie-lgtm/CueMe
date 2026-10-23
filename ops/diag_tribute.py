"""Диагностика задержки статуса после оплаты Tribute. ТОЛЬКО ЧТЕНИЕ: база
открывается read-only, бот не перезапускается, ничего не пишется.

    cd /root/CueMe && venv/bin/python /root/diag_tribute.py

Единственный побочный эффект — проверочный POST без подписи на свой же
вебхук (локально и снаружи): бот ответит 401 и запишет в журнал одну строку
«tribute: неверная подпись вебхука» на каждую проверку. Это ожидаемо.
"""
import glob
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

ROOT = "/root/CueMe"
sys.path.insert(0, ROOT)
import config  # noqa: E402

DB = f"{ROOT}/bot.db"
SERVICE = "cueme-bot"


def hr(title):
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def journal(pattern: str, since="7 days ago") -> list[str]:
    try:
        out = subprocess.run(
            ["journalctl", "-u", SERVICE, "--since", since, "--no-pager", "-o", "short-iso", "-g", pattern],
            capture_output=True, text=True, timeout=180,
        ).stdout
    except Exception as e:
        print(f"  journalctl не сработал: {e}")
        return []
    return [l for l in out.splitlines() if l and not l.startswith("-- ")]


def tg(method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except Exception as e:
        return {"ok": False, "description": str(e)}


def probe(url: str) -> str:
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}" + (" — сервер вебхука отвечает (401 = нет подписи, это норма)" if e.code == 401 else "")
    except Exception as e:
        return f"недоступен: {type(e).__name__}: {e}"


# ── a) журнал ────────────────────────────────────────────────────────────────
hr("a) Журнал за 7 дней (строки с «tribute»)")
lines = journal("tribute")
cats = {
    "неверная подпись вебхука": 0, "неожиданный payload": 0, "сбой обработки события": 0,
    "не удалось отправить подтверждение оплаты": 0, "не удалось распарсить expires_at": 0,
    "выдать доступ в канал": 0, "слушаю": 0,
}
for l in lines:
    for c in cats:
        if c in l:
            cats[c] += 1
for c, n in cats.items():
    label = "сервер вебхука стартовал («tribute webhook: слушаю»)" if c == "слушаю" else c
    print(f"  {label}: {n}")
print(f"  всего строк с «tribute»: {len(lines)}")
for l in [l for l in lines if "сбой обработки" in l or "неожиданный payload" in l][-5:]:
    print("   ", l[:220])

path = config.TRIBUTE_WEBHOOK_PATH
posts = journal(re.escape(path))
post_lines = [l for l in posts if "POST" in l]
codes = Counter(m.group(1) for l in post_lines if (m := re.search(r'POST [^"]*" (\d{3})', l)))
print(f"\n  входящих POST на {path} в журнале: {len(post_lines)}  {dict(codes) or ''}")
if not post_lines:
    print("  (aiohttp мог не писать access-лог в журнал — тогда смотри пункт b: были ли оплаты через вебхук)")
for l in post_lines[-5:]:
    print("   ", l[:200])

# ── b) база ──────────────────────────────────────────────────────────────────
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
hr("b) Последние 20 оплат через вебхук (tribute_payments)")
try:
    pays = db.execute("SELECT telegram_id, event_name, amount, currency, created_at, expires_at "
                      "FROM tribute_payments ORDER BY created_at DESC LIMIT 20").fetchall()
    total_pays = db.execute("SELECT COUNT(*), MIN(created_at) FROM tribute_payments").fetchone()
except sqlite3.OperationalError as e:
    pays, total_pays = [], (0, None)
    print(f"  таблицы нет: {e}")
print(f"  всего записей: {total_pays[0]}, первая: {total_pays[1] or '—'}")
ev_ts = {}
for r in db.execute("SELECT user_telegram_id, ts, meta FROM events WHERE event_type='tribute_payment'"):
    ev_ts.setdefault(r[0], []).append(r[1])
for p in pays:
    has_ev = "да" if p["telegram_id"] in ev_ts else "НЕТ"
    print(f"  {p['created_at'][:19]}  {p['telegram_id']:>12}  {p['event_name']:<22} {p['amount']} {p['currency']}"
          f"  до {p['expires_at'][:16]}  событие tribute_payment: {has_ev}")

# ── доля вебхук / фолбэк по каналу ──────────────────────────────────────────
hr("Кто сейчас Premium через канал и откуда это знает бот")
if not config.PREMIUM_CHANNEL_ID:
    print("  PREMIUM_CHANNEL_ID не задан — проверка по каналу выключена")
else:
    users = [r[0] for r in db.execute("SELECT telegram_id FROM users WHERE COALESCE(blocked_bot,0)=0")]
    webhook_users = {r[0] for r in db.execute("SELECT DISTINCT telegram_id FROM tribute_payments")} if total_pays[0] else set()
    stars_users = {r[0] for r in db.execute("SELECT DISTINCT telegram_id FROM star_payments WHERE tier != 'gift'")}
    members, via_webhook, via_channel_only = [], [], []
    print(f"  проверяю членство в канале у {len(users)} пользователей (по ~20 в секунду)…")
    for i, tid in enumerate(users):
        st = tg("getChatMember", chat_id=config.PREMIUM_CHANNEL_ID, user_id=int(tid)).get("result", {}).get("status")
        if st in ("member", "administrator", "creator"):
            members.append(tid)
            (via_webhook if tid in webhook_users else via_channel_only).append(tid)
        time.sleep(0.05)
    print(f"  в канале: {len(members)}")
    print(f"    из них есть оплата через вебхук: {len(via_webhook)}")
    print(f"    только членство в канале (вебхук не приходил): {len(via_channel_only)}")
    for tid in via_channel_only[:30]:
        u = tg("getChat", chat_id=int(tid)).get("result", {}).get("username")
        extra = " (платил Stars)" if tid in stars_users else ""
        print(f"      {tid} @{u or '—'}{extra}")

# ── задержка ─────────────────────────────────────────────────────────────────
hr("Задержка между оплатой и статусом")
print("  Время самой оплаты на стороне Tribute бот не хранит. Через вебхук статус")
print("  появляется в момент его прихода: _is_premium читает tribute_premium_until раньше кэша.")
print("  Без вебхука — когда Tribute добавит в канал, плюс до PREMIUM_CACHE_TTL =", config.PREMIUM_CACHE_TTL,
      "с отрицательного кэша.")
if pays:
    # Косвенно: последнее нажатие юзера в боте перед оплатой (пейволл/подписка) → приход вебхука.
    print("  Косвенно (последнее событие юзера в боте до прихода вебхука → вебхук):")
    for p in pays[:10]:
        prev = db.execute("SELECT ts, event_type FROM events WHERE user_telegram_id=? AND ts < ? "
                          "AND event_type != 'tribute_payment' ORDER BY ts DESC LIMIT 1",
                          (p["telegram_id"], p["created_at"])).fetchone()
        if prev:
            try:
                d = datetime.fromisoformat(p["created_at"]) - datetime.fromisoformat(prev[0])
                print(f"    {p['telegram_id']}: {prev[1]} → вебхук через {int(d.total_seconds())} с")
            except ValueError:
                pass

# ── c) настройки и доступность ──────────────────────────────────────────────
hr("c) Настройки")
print(f"  TRIBUTE_API_KEY: {'задан' if config.TRIBUTE_API_KEY else 'НЕ ЗАДАН — вебхук-сервер не запускается'}")
print(f"  TRIBUTE_CHANNEL_ID: {config.TRIBUTE_CHANNEL_ID or '(пусто — события не фильтруются)'}")
print(f"  TRIBUTE_WEBHOOK_PATH: {path}   порт: {config.TRIBUTE_WEBHOOK_PORT}")
print(f"  PREMIUM_CHANNEL_ID: {config.PREMIUM_CHANNEL_ID or 'НЕ ЗАДАН'}   PREMIUM_CACHE_TTL: {config.PREMIUM_CACHE_TTL}")

hr("c) Доступность вебхука")
print(f"  локально 127.0.0.1:{config.TRIBUTE_WEBHOOK_PORT}{path}: {probe(f'http://127.0.0.1:{config.TRIBUTE_WEBHOOK_PORT}{path}')}")
hosts = set()
for f in glob.glob("/etc/nginx/**/*", recursive=True) + glob.glob("/etc/caddy/*"):
    try:
        text = open(f, encoding="utf-8", errors="ignore").read()
    except (IsADirectoryError, PermissionError):
        continue
    if path in text or str(config.TRIBUTE_WEBHOOK_PORT) in text:
        print(f"  упоминается в {f}")
        hosts.update(h for h in re.findall(r"server_name\s+([^;]+);", text) for h in h.split() if h != "_")
        hosts.update(re.findall(r"^([a-z0-9.-]+\.[a-z]{2,})\s*\{", text, re.M))
if not hosts:
    print("  в /etc/nginx и /etc/caddy путь вебхука не найден — снаружи по https его, скорее всего, нет")
for h in sorted(hosts):
    print(f"  снаружи https://{h}{path}: {probe(f'https://{h}{path}')}")
print("\nГотово. Пришли весь вывод.")
