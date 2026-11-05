"""Stars вне канала, история оплат и пакетная сборка /users."""
import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import storage


@pytest.fixture
def report(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "report.db")
    storage.init_db()
    import main  # регистрация модулей в исходном порядке
    from handlers import admin_tools
    monkeypatch.setattr(admin_tools, "PREMIUM_CHANNEL_ID", "-100")
    return admin_tools


class Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.checked = []

    async def get_chat(self, tid):
        return SimpleNamespace(username=f"u{tid}")

    async def get_chat_member(self, chat, tid):
        self.checked.append((chat, tid))
        if self.fail:
            raise TimeoutError("Telegram недоступен")
        return SimpleNamespace(status="left")


def pay(tid, until, tier="day", amount=25):
    storage.upsert_user(tid, f"user{tid}")
    storage.record_star_payment(telegram_id=tid, tier=tier, stars_amount=amount,
                               charge_id=f"payment-{tid}", is_subscription=tier == "month",
                               expires_at=until)
    storage.set_stars_premium_until(tid, until)


def test_active_stars_counted_without_channel_even_when_telegram_fails(report):
    until = datetime.now(timezone.utc) + timedelta(days=2)
    pay("1", until)
    bot = Bot(fail=True)
    rows, totals = asyncio.run(report._collect_users_data(bot))
    row = rows[0]
    assert row["is_premium_now"] and row["premium_source"] == "stars"
    assert row["premium_until"] == until.isoformat()
    assert totals["premium_by_source"] == {"stars": 1}
    assert ("-100", 1) not in bot.checked
    assert not row["premium_check_failed"]


def test_expired_stars_remain_in_history_csv_and_html(report):
    pay("1", datetime.now(timezone.utc) - timedelta(days=1))
    rows, totals = asyncio.run(report._collect_users_data(Bot()))
    assert not rows[0]["is_premium_now"]
    assert rows[0]["stars_payments_count"] == 1
    assert rows[0]["stars_paid_total"] == 25
    table = list(csv.DictReader(io.StringIO(report._build_users_csv(rows).decode("utf-8-sig")), delimiter=";"))
    assert table[0]["stars_payments_count"] == "1"
    assert "Оплаты Stars за всё время" in report._build_users_html(rows, totals).decode()


def test_gift_is_not_a_paid_stars_purchase(report):
    storage.upsert_user("1", "user1")
    until = datetime.now(timezone.utc) + timedelta(days=1)
    storage.set_stars_premium_until("1", until)
    storage.record_gift_premium("1", until)
    rows, _ = asyncio.run(report._collect_users_data(Bot()))
    assert rows[0]["premium_source"] == "gift"
    assert rows[0]["stars_payments_count"] == rows[0]["stars_paid_total"] == 0


def test_tribute_and_paid_stars_history_are_independent(report):
    pay("1", datetime.now(timezone.utc) - timedelta(days=1))
    storage.set_tribute_premium_until("1", datetime.now(timezone.utc) + timedelta(days=3))
    rows, _ = asyncio.run(report._collect_users_data(Bot()))
    assert rows[0]["premium_source"] == "tribute"
    assert rows[0]["stars_payments_count"] == 1


def test_failed_channel_lookup_is_explicit(report):
    storage.upsert_user("1", "user1")
    rows, totals = asyncio.run(report._collect_users_data(Bot(fail=True)))
    assert rows[0]["premium_check_failed"]
    assert "get_chat_member:-100" in rows[0]["telegram_check_errors"]
    assert "Premium не проверен" in report._build_users_html(rows, totals).decode()


def test_snapshot_counts_match_previous_queries(report):
    storage.upsert_user("1", "user1")
    cid = storage.get_or_create_contact("1", "user2", "Контакт")
    with storage._conn() as conn:
        conn.execute("INSERT INTO business_chat_refs VALUES (?, ?, ?)", ("1", "chat", cid))
        conn.execute("INSERT INTO business_messages (connection_id, owner_user_id, chat_ref, direction, text, date) VALUES ('c', '1', 'chat', 'in', 'Привет', '2026-01-01')")
        conn.execute("INSERT INTO imported_messages (contact_id, direction, text, date) VALUES (?, 'out', 'Привет', '2026-01-01')", (cid,))
    data = storage.users_report_snapshot()
    assert data["messages"]["1"] == storage.count_biz_messages_for_contact("1", cid) + storage.count_imported_messages(cid)
    assert data["incoming"]["1"] == storage.get_last_incoming_message_time("1")


def test_telegram_calls_overlap_with_bounded_concurrency(report):
    class SlowBot:
        active = 0
        peak = 0

        async def get_chat(self, tid):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.1)
            self.active -= 1
            return tid

    async def run():
        bot = SlowBot()
        client = report._UsersReportBot(bot)
        assert await asyncio.gather(*(client.get_chat(i) for i in range(12))) == list(range(12))
        assert 1 < bot.peak <= 8
    asyncio.run(run())


def test_rate_limit_is_retried(report):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import GetChat

    class LimitedBot:
        calls = 0

        async def get_chat(self, tid):
            self.calls += 1
            if self.calls == 1:
                raise TelegramRetryAfter(GetChat(chat_id=tid), "Слишком часто", retry_after=0)
            return tid

    async def run():
        bot = LimitedBot()
        client = report._UsersReportBot(bot)
        assert await client.get_chat(1) == 1
        assert bot.calls == 2 and not client.errors
    asyncio.run(run())


def test_report_does_not_run_per_contact_counts(report, monkeypatch):
    storage.upsert_user("1", "user1")
    for i in range(25):
        storage.get_or_create_contact("1", f"user{i + 2}", "Контакт")

    def forbidden(*args):
        raise AssertionError("Запрос на каждый контакт вернулся в отчёт")
    monkeypatch.setattr(report, "count_biz_messages_for_contact", forbidden)
    monkeypatch.setattr(report, "count_imported_messages", forbidden)
    rows, _ = asyncio.run(report._collect_users_data(Bot()))
    assert rows[0]["contacts_count"] == 25
