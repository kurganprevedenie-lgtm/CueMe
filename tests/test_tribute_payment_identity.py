import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot.db")
    storage.init_db()
    return storage


def payment(db, user="1", days=7):
    return db.record_tribute_payment(user, 100, 200, "new_subscription", 10000, "rub",
        datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(days=days))


def test_same_plan_different_buyers_and_renewals(db):
    assert payment(db, "1")
    assert payment(db, "2")
    assert not payment(db, "1")
    assert payment(db, "1", days=14)


def test_legacy_identity_migration_preserves_original_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(storage, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE tribute_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id TEXT NOT NULL,
            subscription_id INTEGER NOT NULL, period_id INTEGER NOT NULL,
            event_name TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL,
            expires_at TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(subscription_id,period_id))""")
        conn.execute("INSERT INTO tribute_payments VALUES(1,'1',100,200,'new_subscription',10000,'rub','2030-01-08T00:00:00+00:00','2026-01-01')")
    storage.init_db()
    storage.init_db()  # повтор запуска не меняет миграцию/историю
    assert payment(storage, "2")
    assert not payment(storage, "1")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from tribute_payments").fetchone()[0] == 2
        assert conn.execute("select count(*) from tribute_payments_legacy_20261110").fetchone()[0] == 1


def test_repeat_repairs_missing_window_and_does_not_double_notify(db, monkeypatch):
    import main
    from handlers import stars_tribute as handler, business
    monkeypatch.setattr(handler, "TRIBUTE_CHANNEL_ID", "")
    monkeypatch.setattr(handler, "ADMIN_TELEGRAM_IDS", {"999"})
    invites, notifications = [], []
    async def grant(*args):
        invites.append(args[1])
        return "invite_sent"
    async def send(self, chat_id, text, **kw):
        notifications.append(chat_id)
    async def reveal(*args):
        pass
    monkeypatch.setattr(handler, "_grant_premium_channel_access", grant)
    monkeypatch.setattr(business, "_offer_pending_reveals", reveal)
    bot = type("Bot", (), {"send_message": send})()
    payload = dict(telegram_user_id=42, subscription_id=100, period_id=200, expires_at="2030-01-08T00:00:00Z")
    asyncio.run(handler._handle_tribute_subscription_event(bot, "new_subscription", payload))
    assert db.get_tribute_premium_until("42") == datetime(2030, 1, 8, tzinfo=timezone.utc)
    # Прерванная старая обработка: платёж есть, срок потерян.
    with db._conn() as conn:
        conn.execute("update users set tribute_premium_until=NULL where telegram_id='42'")
    asyncio.run(handler._handle_tribute_subscription_event(bot, "new_subscription", payload))
    assert db.get_tribute_premium_until("42") is not None
    assert notifications.count(999) == 1
    assert invites == ["42", "42"]


def test_old_event_never_shortens_current_subscription(db, monkeypatch):
    import main
    from handlers import stars_tribute as handler, business
    monkeypatch.setattr(handler, "TRIBUTE_CHANNEL_ID", "")
    monkeypatch.setattr(handler, "ADMIN_TELEGRAM_IDS", set())
    db.upsert_user("42", "user42")
    until = datetime(2031, 1, 1, tzinfo=timezone.utc)
    db.set_tribute_premium_until("42", until)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(handler, "_grant_premium_channel_access", noop)
    monkeypatch.setattr(business, "_offer_pending_reveals", noop)
    bot = SimpleNamespace(send_message=noop)
    payload = dict(telegram_user_id=42, subscription_id=100, period_id=200, expires_at="2030-01-08T00:00:00Z")
    asyncio.run(handler._handle_tribute_subscription_event(bot, "new_subscription", payload))
    assert db.get_tribute_premium_until("42") == until


def test_delivered_invite_is_not_claimed_as_membership(db, monkeypatch):
    import main
    from handlers import stars_tribute as handler
    monkeypatch.setattr(handler, "PREMIUM_CHANNEL_ID", "-100")
    sent = []
    class Bot:
        async def get_chat_member(self, *args):
            return SimpleNamespace(status="left")
        async def create_chat_invite_link(self, *args, **kwargs):
            return SimpleNamespace(invite_link="https://t.me/+test")
        async def send_message(self, *args, **kwargs):
            sent.append(args)
    assert asyncio.run(handler._grant_premium_channel_access(Bot(), "42", None)) == "invite_sent"
    assert len(sent) == 1
    with db._conn() as conn:
        assert conn.execute("select meta from events where event_type='premium_channel_access'").fetchone()[0] == "invite_sent"


def test_webhook_processing_error_requests_provider_retry(monkeypatch):
    import main
    from handlers import stars_tribute as handler
    monkeypatch.setattr(handler, "_verify_tribute_signature", lambda *args: True)
    async def fail(*args):
        raise RuntimeError("transient database failure")
    monkeypatch.setattr(handler, "_handle_tribute_subscription_event", fail)
    class Request:
        headers = {}
        app = {"bot": object()}
        async def read(self):
            return b'{"name":"new_subscription","payload":{}}'
    assert asyncio.run(handler.handle_tribute_webhook(Request())).status == 503
