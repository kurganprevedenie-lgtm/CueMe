#!/usr/bin/env bash
# Расследование подмены bot.db (2026-10-09). ТОЛЬКО ЧТЕНИЕ: бота не трогает,
# ничего не удаляет и не перезаписывает. Пишет только в новую папку
# /root/db_rescue_<время>/: копии баз (sqlite backup API, безопасно при
# работающем боте), копии удалённых-но-открытых файлов и report.txt.
# Запуск: bash db_forensics.sh

TS=$(date -u +%Y%m%d_%H%M%S)
OUT="/root/db_rescue_$TS"
mkdir -p "$OUT"
REPORT="$OUT/report.txt"
exec > >(tee "$REPORT") 2>&1
PY=$(command -v python3)

section() { echo; echo "=================== $* ==================="; }

backup_db() {  # $1 — источник, $2 — куда (через sqlite backup API, не cp)
  "$PY" - "$1" "$2" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
print("  сохранено:", sys.argv[2])
PYEOF
}

section "0+1. Процессы бота"
systemctl status cueme-bot --no-pager 2>&1 | head -15
echo "--- unit-файл:"
systemctl cat cueme-bot 2>&1
echo "--- все python-процессы:"
ps -eo pid,ppid,lstart,user,cmd | grep -iE "python|main.py" | grep -v grep
PIDS=$(pgrep -f "main.py")
echo "--- PID с main.py: ${PIDS:-нет}"
[ "$(echo "$PIDS" | wc -w)" -gt 1 ] && echo "!!! ЗАПУЩЕНО НЕСКОЛЬКО КОПИЙ БОТА"

for PID in $PIDS; do
  section "PID $PID"
  echo "cwd: $(readlink /proc/$PID/cwd)"
  echo "cmd: $(tr '\0' ' ' < /proc/$PID/cmdline)"
  echo "--- открытые файлы баз:"
  ls -l /proc/$PID/fd 2>/dev/null | grep -E "\.db|sqlite|-wal|-shm"
  echo "--- удалённые, но ещё открытые:"
  ls -l /proc/$PID/fd 2>/dev/null | grep deleted
  for FD in $(ls -l /proc/$PID/fd 2>/dev/null | grep -E "\.db.*deleted|sqlite.*deleted" | awk '{print $9}'); do
    cp "/proc/$PID/fd/$FD" "$OUT/deleted_pid${PID}_fd${FD}.db" && echo "!!! скопирован удалённый файл: $OUT/deleted_pid${PID}_fd${FD}.db"
  done
  for DB in $(ls -l /proc/$PID/fd 2>/dev/null | grep -E "\.db$" | grep -v deleted | awk '{print $11}' | sort -u); do
    echo "--- бэкап открытой базы $DB:"
    backup_db "$DB" "$OUT/bot_current_${TS}_pid${PID}.db"
  done
  CWD_DB="$(readlink /proc/$PID/cwd)/bot.db"
  if [ -f "$CWD_DB" ] && ! ls "$OUT"/bot_current_*_pid${PID}.db >/dev/null 2>&1; then
    echo "--- бэкап $CWD_DB (по cwd процесса):"
    backup_db "$CWD_DB" "$OUT/bot_current_${TS}_pid${PID}.db"
  fi
done

section "2. Все базы и бэкапы на диске (новее 2026-08-01)"
find / -xdev \( -name "*.db" -o -name "*.sqlite*" -o -name "*.bak" -o -name "*.sql" -o -name "*.db-wal" -o -iname "*bot*backup*" \) \
  -newermt "2026-08-01" -not -path "/proc/*" -not -path "$OUT/*" 2>/dev/null > "$OUT/found.txt"
find /home /opt /srv /var/lib /var/backups /root /tmp \( -name "*.db" -o -name "*.sqlite*" \) -not -path "$OUT/*" 2>/dev/null >> "$OUT/found.txt"
sort -u "$OUT/found.txt" -o "$OUT/found.txt"
ls "$OUT"/*.db 2>/dev/null >> "$OUT/found.txt"

"$PY" - "$OUT/found.txt" <<'PYEOF'
import os, sqlite3, sys, datetime
paths = [p.strip() for p in open(sys.argv[1]) if p.strip()]
print(f"{'путь':70} {'размер':>9} {'mtime (UTC)':16} {'users':>6} {'min created':11} {'max created':11} {'>=09-01':>7} {'contacts':>8} {'biz_msgs':>8}")
for p in paths:
    if not os.path.isfile(p) or not (p.endswith(".db") or ".sqlite" in p or p.endswith(".bak")):
        continue
    st = os.stat(p)
    mtime = datetime.datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    row = [p[-70:], f"{st.st_size//1024}K", mtime]
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        def one(q):
            try: return c.execute(q).fetchone()
            except sqlite3.Error: return None
        u = one("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM users")
        n9 = one("SELECT COUNT(*) FROM users WHERE created_at >= '2026-09-01'")
        ct = one("SELECT COUNT(*) FROM contacts")
        bm = one("SELECT COUNT(*) FROM business_messages")
        if u is None:
            print(f"{row[0]:70} {row[1]:>9} {row[2]:16}  (нет таблицы users)"); continue
        print(f"{row[0]:70} {row[1]:>9} {row[2]:16} {u[0]:>6} {str(u[1])[:10]:11} {str(u[2])[:10]:11} {n9[0] if n9 else '-':>7} {ct[0] if ct else '-':>8} {bm[0] if bm else '-':>8}")
    except Exception as e:
        print(f"{row[0]:70} {row[1]:>9} {row[2]:16}  не открылась: {e}")
PYEOF

section "3. Git на сервере"
for D in $(for PID in $PIDS; do readlink /proc/$PID/cwd; done | sort -u) /root/CueMe; do
  [ -d "$D/.git" ] || continue
  echo "--- $D"
  git -C "$D" status --short | head -20
  git -C "$D" log --oneline -5
  git -C "$D" reflog -30 --date=iso
  echo "db в индексе:"; git -C "$D" ls-files | grep -iE "\.db|sqlite" || echo "  нет"
  echo "bot.db игнорируется:"; git -C "$D" check-ignore -v bot.db || echo "  НЕТ"
  git -C "$D" log --all --oneline -- bot.db | head
done

section "3b. cron / таймеры / скрипты деплоя"
crontab -l 2>&1
ls -la /etc/cron.d /etc/cron.daily 2>/dev/null
systemctl list-timers --all --no-pager 2>/dev/null | head -20
grep -rlE "bot\.db|git (pull|reset|clean|checkout)|rsync" /etc/cron* /etc/systemd/system /root/*.sh /root/CueMe/*.sh /opt 2>/dev/null | head -20 | while read F; do echo "--- $F"; cat "$F"; done

section "4. Журнал бота 2026-10-09 06:00–18:00 (запуски, остановки, ошибки)"
journalctl -u cueme-bot --since "2026-10-09 06:00" --until "2026-10-09 18:00" --no-pager \
  | grep -E "Started|Stopped|Stopping|Main process exited|Failed|Conflict|terminated by other getUpdates|sqlite|database|Traceback|Error|LLM-каскад|Start polling" \
  | grep -v "LLM \[" | head -200
echo "--- запуски по времени (строка «LLM-каскад» пишется при каждом старте):"
journalctl -u cueme-bot --since "2026-10-01" --no-pager | grep "LLM-каскад" | awk '{print $1, $2, $3}'

section "4b. История shell (bot.db, git reset/checkout/clean, cp/mv/rm, scp, systemctl)"
for H in /root/.bash_history /home/*/.bash_history; do
  [ -f "$H" ] || continue
  echo "--- $H"
  grep -nE "bot\.db|\.db|git (reset|checkout|clean|pull|stash)|^ *(cp|mv|rm|scp|rsync) |systemctl|cd |nohup|screen|tmux|main\.py" "$H" | tail -80
done

section "ГОТОВО"
echo "Отчёт: $REPORT"
echo "Копии баз: $(ls "$OUT"/*.db 2>/dev/null | tr '\n' ' ')"
