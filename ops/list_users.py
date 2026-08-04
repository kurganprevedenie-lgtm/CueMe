"""Разовый отчёт: список всех пользователей CueMe + базовая статистика.
Запуск: py -3.13 ops/list_users.py (из корня репозитория, рядом с bot.db)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import _conn, init_db

_DEMO_ORIGINAL_IDS = ("demo_boss", "demo_friend")


def main() -> None:
    init_db()  # применяет недостающие миграции колонок, если их ещё нет
    with _conn() as conn:
        users = conn.execute(
            "SELECT telegram_id, my_id, created_at, gender, trial_used, "
            "demo_trial_used, deep_analysis_free_until FROM users "
            "ORDER BY created_at"
        ).fetchall()

        contacts_by_user: dict[str, list] = {}
        for row in conn.execute(
            "SELECT user_telegram_id, original_from_id FROM contacts"
        ):
            contacts_by_user.setdefault(row["user_telegram_id"], []).append(row)

    now = datetime.now(timezone.utc)
    total = len(users)
    with_gender = 0
    with_real_contact = 0
    with_any_contact = 0
    with_active_referral_premium = 0

    print(f"{'telegram_id':<12} {'gender':<8} {'created_at':<20} {'trial':<6} {'contacts':<10} {'premium(ref)':<12}")
    print("-" * 80)

    for u in users:
        tid = u["telegram_id"]
        gender = u["gender"] or "-"
        if u["gender"]:
            with_gender += 1

        user_contacts = contacts_by_user.get(tid, [])
        real_count = sum(1 for c in user_contacts if c["original_from_id"] not in _DEMO_ORIGINAL_IDS)
        demo_count = len(user_contacts) - real_count
        if user_contacts:
            with_any_contact += 1
        if real_count:
            with_real_contact += 1
        contacts_str = f"{real_count} real+{demo_count} demo" if user_contacts else "0"

        ref_until = u["deep_analysis_free_until"]
        premium_str = "-"
        if ref_until:
            try:
                until_dt = datetime.fromisoformat(ref_until)
                if until_dt > now:
                    premium_str = f"до {until_dt.strftime('%d.%m %H:%M')}"
                    with_active_referral_premium += 1
            except ValueError:
                pass

        print(f"{tid:<12} {gender:<8} {u['created_at']:<20} {u['trial_used']:<6} {contacts_str:<10} {premium_str:<12}")

    print("-" * 80)
    print(f"Всего пользователей: {total}")
    print(f"С выбранным полом: {with_gender}")
    print(f"С хотя бы одним контактом (включая демо): {with_any_contact}")
    print(f"С хотя бы одним РЕАЛЬНЫМ контактом: {with_real_contact}")
    print(f"С активной реферальной Premium-наградой: {with_active_referral_premium}")


if __name__ == "__main__":
    main()
