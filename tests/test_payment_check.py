"""Статус после оплаты Tribute: короткий отрицательный кэш, кнопка
«✅ Я оплатил(а) — проверить», фоновая проверка, логи отброшенных событий."""
from tests.bot_patch import patch_bot
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import storage

USER = "200"


@pytest.fixture()
def m(monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    storage.init_db()
    storage.upsert_user(USER, f"user{USER}")
    import main
    patch_bot(monkeypatch, "PREMIUM_CHANNEL_ID", "-100")
    patch_bot(monkeypatch, "PREMIUM_SUBSCRIBE_URL", "https://t.me/tribute/app?startapp=x")
    patch_bot(monkeypatch, "PREMIUM_CACHE_TTL", 300)
    patch_bot(monkeypatch, "PREMIUM_NEG_CACHE_TTL", 20)
    main._premium_cache.clear()
    main._check_payment_last.clear()
    main._payment_watches.clear()
    clock = {"t": 1000.0}
    patch_bot(monkeypatch, "_premium_clock", lambda: clock["t"])
    main._clock = clock
    return main


class _Bot:
    def __init__(self, member=False):
        self.member, self.checks, self.sent = member, 0, []

    async def get_chat_member(self, chat, uid):
        self.checks += 1
        return SimpleNamespace(status="member" if self.member else "left")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def _premium(m, bot):
    return asyncio.run(m._is_premium(bot, USER))


def test_negative_cache_expires_faster_than_positive(m):
    bot = _Bot(member=False)
    assert _premium(m, bot) is False
    m._clock["t"] += 10
    _premium(m, bot)
    assert bot.checks == 1           # «нет» ещё в кэше
    m._clock["t"] += 15              # 25 с > 20 с
    bot.member = True
    assert _premium(m, bot) is True and bot.checks == 2
    m._clock["t"] += 200             # «да» держится 300 с
    bot.member = False
    assert _premium(m, bot) is True and bot.checks == 2


class _Msg:
    def __init__(self):
        self.out = []

    async def answer(self, text, **kw):
        self.out.append((text, kw))


def _call(msg):
    answers = []

    async def ans(text=None, **kw):
        answers.append(text)
    return SimpleNamespace(data="check_payment", from_user=SimpleNamespace(id=int(USER)), message=msg, answer=ans), answers


async def _press(m, bot, msg):
    call, answers = _call(msg)
    await m.cb_check_payment(call, bot)
    return answers


def test_check_payment_resets_cache_and_confirms(m):
    bot = _Bot(member=False)
    _premium(m, bot)                 # «нет» закэшировано на 20 с
    bot.member = True
    msg = _Msg()
    asyncio.run(_press(m, bot, msg))
    assert bot.checks == 2           # кэш сброшен — спросили Telegram заново
    assert bot.sent == ["🎉 Premium активен!"] and msg.out == []


def test_check_payment_shows_date_when_known(m):
    from datetime import datetime, timedelta, timezone
    storage.set_stars_premium_until(USER, datetime.now(timezone.utc) + timedelta(days=3))
    bot = _Bot()
    asyncio.run(_press(m, bot, _Msg()))
    assert bot.sent[0].startswith("🎉 Premium активен до ")


def test_check_payment_not_yet_shows_retry_and_starts_watch(m):
    bot = _Bot(member=False)
    msg = _Msg()

    async def run():
        await _press(m, bot, msg)
        task = m._payment_watches.get(USER)
        assert task is not None and not task.done()
        task.cancel()
    asyncio.run(run())
    text, kw = msg.out[0]
    assert text.startswith("Платёж ещё обрабатывается") and "вступил(а)" in text
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "check_payment"


def test_check_payment_antispam(m):
    bot = _Bot(member=False)

    async def run():
        first = await _press(m, bot, _Msg())
        m._clock["t"] += 3
        second = await _press(m, bot, _Msg())
        m._clock["t"] += 8
        third = await _press(m, bot, _Msg())
        for t in m._payment_watches.values():
            t.cancel()
        return first, second, third
    first, second, third = asyncio.run(run())
    assert first == [None]
    assert "не чаще раза в 10 с" in second[0]
    assert third == [None] and bot.checks == 2


def test_payment_watch_confirms_once_and_cleans_up(m, monkeypatch):
    patch_bot(monkeypatch, "_PAYMENT_WATCH_INTERVAL", 0.01)
    patch_bot(monkeypatch, "_PAYMENT_WATCH_DURATION", 0.05)
    bot = _Bot(member=False)

    async def run():
        m._start_payment_watch(bot, USER)
        first = m._payment_watches[USER]
        m._start_payment_watch(bot, USER)
        assert m._payment_watches[USER] is first   # второй таск не создаётся
        await asyncio.sleep(0.015)
        bot.member = True
        await first
    asyncio.run(run())
    assert bot.sent == ["🎉 Premium активен!"]
    assert USER not in m._payment_watches


def test_payment_watch_gives_up_after_duration(m, monkeypatch):
    patch_bot(monkeypatch, "_PAYMENT_WATCH_INTERVAL", 0.01)
    patch_bot(monkeypatch, "_PAYMENT_WATCH_DURATION", 0.03)
    bot = _Bot(member=False)

    async def run():
        m._start_payment_watch(bot, USER)
        await m._payment_watches[USER]
    asyncio.run(run())
    assert bot.sent == [] and USER not in m._payment_watches and bot.checks == 4


def test_foreign_channel_event_logs_warning(m, monkeypatch, caplog):
    patch_bot(monkeypatch, "TRIBUTE_CHANNEL_ID", "111")
    with caplog.at_level("WARNING"):
        asyncio.run(m._handle_tribute_subscription_event(_Bot(), "new_subscription", {"channel_id": 999}))
    assert "для другого канала channel_id=999" in caplog.text


def test_empty_payload_logs_warning(m, caplog):
    with caplog.at_level("WARNING"):
        asyncio.run(m._handle_tribute_subscription_event(_Bot(), "new_subscription", {}))
    assert "пустой payload" in caplog.text


def test_keyboards_have_check_button(m):
    for kb in (m.paywall_kb(), m.premium_menu_kb()):
        rows = [b for row in kb.inline_keyboard for b in row]
        assert rows[0].url and rows[1].callback_data == "check_payment"
