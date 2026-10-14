"""/gift_premium: подарок N дней Premium (срок в stars_premium_until)."""
import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError

import storage

ADMIN, USER = "100", "200"


@pytest.fixture()
def m(monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    storage.init_db()
    storage.upsert_user(USER, f"user{USER}")
    import main
    monkeypatch.setattr(main, "ADMIN_TELEGRAM_IDS", {ADMIN})
    monkeypatch.setattr(main, "PREMIUM_CHANNEL_ID", "")
    main._premium_cache.clear()
    main._pending_gifts.clear()
    return main


def _now():
    return datetime.now(timezone.utc)


def _close(a, b):
    return abs((a - b).total_seconds()) < 5


def test_gift_without_subscription_starts_now(m):
    until = m._apply_gift_premium(ADMIN, USER, 7)
    assert _close(until, _now() + timedelta(days=7))
    assert m._has_stars_premium(USER)
    assert m._premium_expiry_info(USER)[0] == "gift"


def test_gift_on_top_of_active_stars_adds_days(m):
    paid = _now() + timedelta(days=10)
    storage.set_stars_premium_until(USER, paid)
    assert _close(m._apply_gift_premium(ADMIN, USER, 7), paid + timedelta(days=7))


def test_repeated_gift_stacks_and_is_logged(m):
    m._apply_gift_premium(ADMIN, USER, 7)
    until = m._apply_gift_premium(ADMIN, USER, 3)
    assert _close(until, _now() + timedelta(days=10))
    with storage._conn() as c:
        rows = c.execute("SELECT meta FROM events WHERE event_type='gift_premium'").fetchall()
    assert [json.loads(r[0])["days"] for r in rows] == [7, 3]
    assert json.loads(rows[0][0])["by"] == ADMIN


def test_gift_for_tribute_user_keeps_later_tribute_date(m):
    tribute = _now() + timedelta(days=30)
    storage.set_tribute_premium_until(USER, tribute)
    m._apply_gift_premium(ADMIN, USER, 7)
    assert storage.get_tribute_premium_until(USER) == tribute  # вебхук-срок не тронут
    source, until, _ = m._premium_expiry_info(USER)
    assert source == "tribute" and _close(until, tribute)


def test_gift_expiry(m):
    storage.set_stars_premium_until(USER, _now() - timedelta(minutes=1))
    storage.record_gift_premium(USER, _now() - timedelta(minutes=1))
    assert asyncio.run(m._is_premium(None, USER)) is False


class _Bot:
    def __init__(self, member=False, blocked=False):
        self.sent, self.member, self.blocked = [], member, blocked

    async def send_message(self, chat_id, text, **kw):
        if self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat, uid):
        return SimpleNamespace(status="member" if self.member else "left")


def _expire_soon(m, days_left_h=30):
    until = _now() + timedelta(hours=days_left_h)
    storage.set_stars_premium_until(USER, until)
    storage.record_gift_premium(USER, until)


def test_gift_expiry_reminder_is_soft(m):
    _expire_soon(m)
    bot = _Bot()
    asyncio.run(m._check_premium_expiry_reminders(bot))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert text.startswith("🎁 Подарочный Premium закончится") and "потерять доступ" not in text
    asyncio.run(m._check_premium_expiry_reminders(bot))
    assert len(bot.sent) == 1  # про тот же срок — один раз


def test_gift_reminder_skipped_for_channel_member(m, monkeypatch):
    monkeypatch.setattr(m, "PREMIUM_CHANNEL_ID", "-100")
    _expire_soon(m)
    bot = _Bot(member=True)
    asyncio.run(m._check_premium_expiry_reminders(bot))
    assert bot.sent == []


def test_stars_purchase_after_gift_keeps_gift_days(m):
    gift_until = m._apply_gift_premium(ADMIN, USER, 20)
    answers = []

    async def answer(text, **kw):
        answers.append(text)
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=int(USER)), answer=answer,
        successful_payment=SimpleNamespace(
            currency="XTR", invoice_payload="stars:week:x:y", subscription_expiration_date=None,
            total_amount=100, telegram_payment_charge_id="ch1"),
    )
    asyncio.run(m.process_stars_successful_payment(msg, None))
    assert _close(storage.get_stars_premium_until(USER), gift_until + timedelta(days=7))
    assert m._premium_expiry_info(USER)[0] == "stars"


class _Msg:
    def __init__(self, text, uid=ADMIN):
        self.text, self.from_user, self.out = text, SimpleNamespace(id=int(uid)), []

    async def answer(self, text, **kw):
        self.out.append((text, kw))

    async def edit_text(self, text, **kw):
        self.out.append((text, kw))


def _call(data, msg):
    async def ans(*a, **k):
        pass
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=int(ADMIN)), message=msg, answer=ans)


def test_command_confirm_flow_and_blocked_user(m):
    msg = _Msg(f"/gift_premium {USER} 7")
    asyncio.run(m.cmd_gift_premium(msg, _Bot()))
    preview, kw = msg.out[0]
    assert "Stars/подарок: —" in preview and "Мы подарили тебе 7 дней Premium" in preview
    assert m.get_stars_premium_until(USER) is None  # до «Да» ничего не выдано

    data = kw["reply_markup"].inline_keyboard[0][0].callback_data
    bot = _Bot(blocked=True)
    asyncio.run(m.cb_gift_premium(_call(data, msg), bot))
    assert m._has_stars_premium(USER)
    assert "НЕ доставлено" in msg.out[-1][0]


def test_command_custom_text_and_non_admin(m):
    asyncio.run(m.cmd_gift_premium(_Msg(f"/gift_premium {USER} 3", uid="999"), _Bot()))
    assert m._pending_gifts == {}
    msg = _Msg(f"/gift_premium {USER} 3 Держи, спасибо!")
    asyncio.run(m.cmd_gift_premium(msg, _Bot()))
    data = msg.out[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    bot = _Bot()
    asyncio.run(m.cb_gift_premium(_call(data, msg), bot))
    assert bot.sent == [(int(USER), "Держи, спасибо!")]


def test_bad_days_shows_usage(m):
    msg = _Msg(f"/gift_premium {USER} много")
    asyncio.run(m.cmd_gift_premium(msg, _Bot()))
    assert msg.out[0][0].startswith("Использование")
