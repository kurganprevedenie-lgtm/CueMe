"""CLI: извлечь переписку с контактом из bot.db в файл — одной командой.

Для eval на реальных данных и отладки. Тянет сообщения (business + imported)
через уже существующий storage.get_all_dated_messages, поэтому логика извлечения
и дедуп — та же, что в проде.

    # JSON в формате Telegram-экспорта (ест eval --export и tg_parser):
    python -m tools.export --contact 3 --out chat.json
    PYTHONPATH=. python eval/run_eval.py --export chat.json --my-id <my_id>

    # человекочитаемый текст:
    python -m tools.export --contact 3 --out chat.txt
"""
import argparse
import json
import sys
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
    messages = [
        {
            "type": "message",
            "from_id": my_id if r["direction"] == "out" else contact_fid,
            "from": "Я" if r["direction"] == "out" else name,
            "text": r["text"],
            "date": r["date"],
        }
        for r in rows
    ]
    return {"my_id": my_id, "contact_name": name, "messages": messages}


def to_text(export: dict) -> str:
    """Человекочитаемая выгрузка: «[дата] Кто: текст»."""
    return "\n".join(
        f"[{m['date']}] {m['from']}: {m['text']}" for m in export["messages"]
    )


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
    ap.add_argument("--out", help="файл (.json — формат экспорта; .txt — читаемый)")
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

    content = to_text(export) if args.out.endswith(".txt") else json.dumps(
        export, ensure_ascii=False, indent=2)
    Path(args.out).write_text(content, encoding="utf-8")

    print(f"Извлечено сообщений: {n} (контакт «{export['contact_name']}», my_id={export['my_id']})")
    print(f"Файл: {args.out}")
    if not args.out.endswith(".txt"):
        print(f"Прогнать eval:  PYTHONPATH=. python eval/run_eval.py "
              f"--export {args.out} --my-id {export['my_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
