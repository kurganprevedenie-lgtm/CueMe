"""CLI: извлечь переписку с контактом из bot.db в файл — одной командой.

Для eval на реальных данных и отладки. Тянет сообщения (business + imported)
через уже существующий storage.get_all_dated_messages, поэтому логика извлечения
и дедуп — та же, что в проде.

    # JSON в формате Telegram-экспорта (ест eval --export и tg_parser):
    python -m tools.export --contact 3 --out chat.json
    PYTHONPATH=. python eval/run_eval.py --export chat.json --my-id <my_id>

    # человекочитаемый текст:
    python -m tools.export --contact 3 --out chat.txt

    # визуальный HTML (пузыри слева/справа, как в мессенджере):
    python -m tools.export --contact 3 --out chat.html
"""
import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

import storage


def extract_conversation(contact_id: int) -> dict:
    """Собирает переписку контакта в формате Telegram-экспорта (result.json),
    который понимают tg_parser.parse_chat и eval --export.
    Ключи my_id/contact_name — служебные (parse_chat читает только messages)."""
    contact = storage.get_contact_by_id(contact_id)
    if not contact:
        raise ValueError(f"контакт id={contact_id} не найден в bot.db")
    owner = contact["user_telegram_id"]
    user = storage.get_user(owner)
    my_id = user["my_id"] if user and user["my_id"] else "me"
    contact_fid = contact["original_from_id"] or f"contact{contact_id}"
    name = contact["display_name"] or contact_fid

    rows = storage.get_all_dated_messages(owner, contact_id)
    rows.sort(key=lambda r: r["date"])
    # (date, text) исходящего business-сообщения → результат сопоставления с
    # подсказкой CueMe (main._match_outgoing_to_suggestion) — тот же
    # необработанный date/text, что и в rows выше, так что ключ совпадает
    # напрямую, без дополнительной нормализации.
    matches = storage.get_business_message_matches_for_contact(owner, contact_id)
    messages = []
    for r in rows:
        m = {
            "type": "message",
            "from_id": my_id if r["direction"] == "out" else contact_fid,
            "from": "Я" if r["direction"] == "out" else name,
            "text": r["text"],
            "date": r["date"],
        }
        if r["direction"] == "out":
            match = matches.get((r["date"], r["text"]))
            if match:
                m["cueme_match"] = match
        messages.append(m)
    return {"my_id": my_id, "contact_name": name, "messages": messages}


def _cueme_badge(match: dict | None) -> str:
    """«🤖 CueMe (92%)» — использовано как есть (ratio>=0.85, процент совпадения);
    «🤖 CueMe (с правками)» — использовано с правками (0.5<=ratio<0.85)."""
    if not match:
        return ""
    if match["match_kind"] == "exact":
        return f"🤖 CueMe ({match['ratio']:.0%})"
    return "🤖 CueMe (с правками)"


def to_text(export: dict) -> str:
    """Человекочитаемая выгрузка: «[дата] Кто: текст» — плюс короткая приписка
    «🤖 CueMe (N%)» / «🤖 CueMe (с правками)» в конце строки, если исходящее
    сообщение засчитано как использование подсказки (см. cueme_match)."""
    lines = []
    for m in export["messages"]:
        line = f"[{m['date']}] {m['from']}: {m['text']}"
        badge = _cueme_badge(m.get("cueme_match"))
        if badge:
            line += f"  {badge}"
        lines.append(line)
    return "\n".join(lines)


_RU_MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _fmt_day(dt: datetime) -> str:
    label = f"{dt.day} {_RU_MONTHS_GEN[dt.month]}"
    if dt.year != datetime.now().year:
        label += f" {dt.year}"
    return label


def to_html(export: dict) -> str:
    """Визуальный HTML-экспорт — самостоятельный файл (инлайновый <style>, без
    внешних зависимостей), открывается локально в браузере без интернета.
    Сообщения автора (from_id == my_id) — пузырём справа, собеседника — слева,
    с разделителем даты между днями, в духе типичного чат-интерфейса.

    Медиа-плейсхолдер (фото/голосовое без текста) — ЧИСТО подстраховка на
    случай пустого text: storage.get_all_dated_messages уже фильтрует такие
    сообщения на уровне SQL (WHERE text IS NOT NULL AND text != ''), поэтому
    extract_conversation физически не отдаёт медиа-only сообщения — to_text()
    по той же причине их никак не обрабатывает, этот код в реальности не
    срабатывает, пока структура данных (messages: from_id/from/text/date) не
    начнёт нести отдельный признак типа сообщения."""
    name = export.get("contact_name") or "собеседник"
    my_id = export.get("my_id")

    rows_html: list[str] = []
    last_day: str | None = None
    for m in export["messages"]:
        try:
            dt = datetime.fromisoformat(m["date"])
        except (ValueError, TypeError):
            dt = None

        day_key = (m.get("date") or "")[:10]
        if dt is not None and day_key != last_day:
            rows_html.append(f'<div class="day-sep"><span>{html.escape(_fmt_day(dt))}</span></div>')
            last_day = day_key

        text = (m.get("text") or "").strip()
        if text:
            body = html.escape(text).replace("\n", "<br>")
        else:
            body = "📷 Без текста"  # медиа-плейсхолдер, см. докстринг выше

        time_label = dt.strftime("%H:%M") if dt is not None else ""
        side = "out" if m.get("from_id") == my_id else "in"
        badge = _cueme_badge(m.get("cueme_match"))
        badge_html = f'<div class="cueme-badge">{html.escape(badge)}</div>' if badge else ""
        rows_html.append(
            f'<div class="row {side}"><div class="bubble">'
            f'<div class="bubble-text">{body}</div>'
            f"{badge_html}"
            f'<div class="bubble-time">{time_label}</div>'
            f'</div></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Переписка с {html.escape(name)}</title>
<style>
  :root {{
    --bg: #e9edf1;
    --header-bg: #ffffff;
    --out-bg: #d9fdd3;
    --in-bg: #ffffff;
    --text: #111b21;
    --meta: #667781;
    --sep-bg: #d9dee3;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--text);
  }}
  .header {{
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--header-bg);
    padding: 14px 16px;
    font-size: 17px;
    font-weight: 600;
    border-bottom: 1px solid #d1d7db;
  }}
  .chat {{
    max-width: 760px;
    margin: 0 auto;
    padding: 12px 10px 24px;
  }}
  .day-sep {{
    display: flex;
    justify-content: center;
    margin: 14px 0;
  }}
  .day-sep span {{
    background: var(--sep-bg);
    color: var(--meta);
    font-size: 12.5px;
    padding: 4px 12px;
    border-radius: 8px;
  }}
  .row {{
    display: flex;
    margin: 3px 0;
  }}
  .row.out {{ justify-content: flex-end; }}
  .row.in  {{ justify-content: flex-start; }}
  .bubble {{
    max-width: 75%;
    padding: 6px 9px 5px;
    border-radius: 9px;
    box-shadow: 0 1px 0.5px rgba(0, 0, 0, .08);
  }}
  .row.out .bubble {{ background: var(--out-bg); border-top-right-radius: 2px; }}
  .row.in  .bubble {{ background: var(--in-bg);  border-top-left-radius: 2px; }}
  .bubble-text {{
    font-size: 14.5px;
    line-height: 1.35;
    white-space: pre-wrap;
    word-wrap: break-word;
  }}
  .bubble-time {{
    font-size: 11px;
    color: var(--meta);
    text-align: right;
    margin-top: 2px;
  }}
  .cueme-badge {{
    display: inline-block;
    font-size: 10.5px;
    color: #2563eb;
    background: #e8efff;
    padding: 1px 7px;
    border-radius: 6px;
    margin-top: 4px;
  }}
  @media (max-width: 480px) {{
    .bubble {{ max-width: 85%; }}
  }}
</style>
</head>
<body>
<div class="header">Переписка с {html.escape(name)}</div>
<div class="chat">
{"".join(rows_html)}
</div>
</body>
</html>
"""


def list_contacts() -> list[dict]:
    """Все контакты с числом сохранённых сообщений — чтобы узнать contact_id."""
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT id, user_telegram_id, display_name FROM contacts ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        n = len(storage.get_all_dated_messages(r["user_telegram_id"], r["id"]))
        out.append({"id": r["id"], "name": r["display_name"], "messages": n})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Извлечь переписку контакта из bot.db")
    ap.add_argument("--contact", type=int, help="contact_id из таблицы contacts")
    ap.add_argument("--out", help="файл (.json — формат экспорта; .txt — читаемый; .html — визуальный)")
    ap.add_argument("--list", action="store_true", help="показать контакты с id и числом сообщений")
    ap.add_argument("--db", default=None, help="путь к БД (по умолчанию bot.db)")
    args = ap.parse_args(argv)

    if args.db:
        storage.DB_PATH = Path(args.db)

    if args.list:
        rows = list_contacts()
        if not rows:
            print("В bot.db нет контактов.")
            return 0
        for r in rows:
            print(f"  id={r['id']}  «{r['name']}»  сообщений: {r['messages']}")
        return 0

    if args.contact is None or not args.out:
        print("Нужны --contact <id> и --out <файл> (или --list для просмотра контактов).")
        return 2

    try:
        export = extract_conversation(args.contact)
    except ValueError as e:
        print(f"Ошибка: {e}")
        return 1
    n = len(export["messages"])
    if n == 0:
        print(f"У контакта id={args.contact} нет сохранённых сообщений.")
        return 1

    if args.out.endswith(".txt"):
        content = to_text(export)
    elif args.out.endswith(".html"):
        content = to_html(export)
    else:
        content = json.dumps(export, ensure_ascii=False, indent=2)
    Path(args.out).write_text(content, encoding="utf-8")

    print(f"Извлечено сообщений: {n} (контакт «{export['contact_name']}», my_id={export['my_id']})")
    print(f"Файл: {args.out}")
    if args.out.endswith(".json"):
        print(f"Прогнать eval:  PYTHONPATH=. python eval/run_eval.py "
              f"--export {args.out} --my-id {export['my_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
