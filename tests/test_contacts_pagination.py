"""Постраничный список контактов: contacts_kb / unified_contacts_kb / _delete_kb
по CONTACTS_PAGE_SIZE на страницу, навигация «◀️ 2/5 ▶️» (pg:<kind>:<page>),
один общий хендлер листания."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import storage
from tests.bot_patch import patch_bot

OWNER = "500"


@pytest.fixture()
def bot_mod(monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    storage.init_db()
    storage.upsert_user(OWNER, f"user{OWNER}")
    import main
    patch_bot(monkeypatch, "CONTACTS_PAGE_SIZE", 10)
    return main


def _make(n: int, owner: str = OWNER) -> list:
    for i in range(n):
        storage.get_or_create_contact(owner, f"user{9000 + i}", f"Контакт {i:02d}")
    return storage.list_contacts(owner)


def _rows(kb):
    return [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]


def _contact_rows(kb, prefix):
    return [r[0] for r in _rows(kb) if len(r) == 1 and r[0][1].startswith(prefix) and not r[0][1].endswith(":new")]


def _nav(kb):
    navs = [r for r in _rows(kb) if any(d.startswith("pg:") for _, d in r)]
    assert len(navs) <= 1
    return [t for t, _ in navs[0]] if navs else None


@pytest.mark.parametrize("n, pages", [(0, 1), (1, 1), (10, 1), (11, 2), (25, 3)])
def test_page_count_and_sizes(bot_mod, n, pages):
    contacts = _make(n)
    seen = []
    for page in range(pages):
        kb = bot_mod.contacts_kb(contacts, "reply", page)
        items = _contact_rows(kb, "reply:")
        assert len(items) == (min(10, n - page * 10) if n else 0)
        seen += [d for _, d in items]
        if pages == 1:
            assert _nav(kb) is None  # как раньше — без навигации
        else:
            nav = _nav(kb)
            assert f"{page + 1}/{pages}" in nav
            assert ("◀️" in nav) == (page > 0)            # на первой нет «назад»
            assert ("▶️" in nav) == (page < pages - 1)    # на последней нет «вперёд»
    assert len(seen) == len(set(seen)) == n  # каждый контакт ровно один раз


def test_page_out_of_range_shows_last(bot_mod):
    contacts = _make(25)
    kb = bot_mod.contacts_kb(contacts, "deepan", 99)
    assert _nav(kb) == ["◀️", "3/3"] and len(_contact_rows(kb, "deepan:")) == 5
    assert _nav(bot_mod.contacts_kb(contacts, "deepan", -3))[0] == "1/3"


@pytest.mark.parametrize("builder, special", [
    ("unified_contacts_kb", ("➕ Другой человек", "unified_contact:new")),
    ("_delete_kb", ("‼️ Удалить ВСЕ данные", "delall")),
])
@pytest.mark.parametrize("n", [0, 1, 25])
def test_special_buttons_on_every_page_below_nav(bot_mod, builder, special, n):
    fn = getattr(bot_mod, builder)
    contacts = _make(n)
    for page in range(max(1, -(-n // 10))):
        rows = _rows(fn(contacts, page))
        assert rows[-1] == [special]
        if n > 10:
            assert any(d.startswith("pg:") for _, d in rows[-2])  # навигация прямо над спец-кнопкой


def test_contact_callbacks_unchanged(bot_mod):
    contacts = _make(3)
    ids = {c["id"] for c in contacts}
    for prefix in ("deepan", "idealdate", "mystyle", "reply", "setup"):
        assert {int(d.split(":")[1]) for _, d in _contact_rows(bot_mod.contacts_kb(contacts, prefix), f"{prefix}:")} == ids
    assert {d for _, d in _contact_rows(bot_mod.unified_contacts_kb(contacts), "unified_contact:")} == {
        f"unified_contact:{i}" for i in ids}
    assert {d for _, d in _contact_rows(bot_mod._delete_kb(contacts), "del:")} == {f"del:{i}" for i in ids}


def test_callback_data_fits_64_bytes(bot_mod):
    contacts = _make(25)
    builders = [lambda c, p, k=k: bot_mod.contacts_kb(c, k, p) for k in bot_mod._CONTACTS_KB_KINDS]
    builders += [bot_mod.unified_contacts_kb, bot_mod._delete_kb]
    for build in builders:
        for page in range(3):
            for row in _rows(build(contacts, page)):
                for _, data in row:
                    assert len(data.encode("utf-8")) <= 64, data


def test_recent_contacts_first_and_order_stable(bot_mod):
    contacts = _make(12)
    by_name = {c["display_name"]: c["id"] for c in contacts}
    now = datetime.now(timezone.utc)
    # «Контакт 07» — переписка только что (живое подключение), «Контакт 03» —
    # вчера (JSON-экспорт, дата без пояса), остальные без сообщений
    with storage._conn() as conn:
        conn.execute("INSERT INTO business_chat_refs VALUES (?, 'ref7', ?)", (OWNER, by_name["Контакт 07"]))
    storage.save_business_message("c1", OWNER, "ref7", "in", "привет", now.isoformat(), 1, {})
    storage.save_imported_messages(by_name["Контакт 03"], [
        {"direction": "in", "text": "пока", "date": (now - timedelta(days=1)).replace(tzinfo=None).isoformat()}])
    order = []
    for page in range(2):
        order += [int(d.split(":")[1]) for _, d in _contact_rows(bot_mod.contacts_kb(contacts, "reply", page), "reply:")]
    assert order[:2] == [by_name["Контакт 07"], by_name["Контакт 03"]]
    rest = [c["id"] for c in contacts if c["id"] not in order[:2]]
    assert order[2:] == sorted(rest)
    # тот же порядок при повторном построении (листание туда-обратно)
    again = [int(d.split(":")[1]) for _, d in _contact_rows(bot_mod.contacts_kb(contacts, "reply", 0), "reply:")]
    assert again == order[:10]


class _Msg:
    def __init__(self, chat_id):
        self.chat = SimpleNamespace(id=chat_id)
        self.markups = []

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


def _press(bot_mod, data, user_id=int(OWNER), chat_id=None):
    msg = _Msg(int(OWNER) if chat_id is None else chat_id)
    answers = []

    async def answer(text=None, **kw):
        answers.append(text)
    call = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), message=msg, answer=answer)
    asyncio.run(bot_mod.cb_contacts_page(call))
    return msg, answers


@pytest.mark.parametrize("kind", ["deepan", "idealdate", "mystyle", "reply", "setup", "unified", "del"])
def test_handler_edits_markup_of_same_message(bot_mod, kind):
    _make(25)
    msg, answers = _press(bot_mod, f"pg:{kind}:1")
    assert len(msg.markups) == 1 and answers == [None]
    assert "2/3" in _nav(msg.markups[0])


def test_handler_out_of_range_opens_last_page(bot_mod):
    contacts = _make(25)
    # пока юзер листал, 5 контактов удалили: было 3 страницы, стало 2
    for c in contacts[:5]:
        storage.delete_contact_data(OWNER, c["id"])
    msg, _ = _press(bot_mod, "pg:reply:2")
    assert _nav(msg.markups[0]) == ["◀️", "2/2"]
    assert len(_contact_rows(msg.markups[0], "reply:")) == 10


def test_handler_uses_only_presser_contacts(bot_mod):
    _make(25)                      # контакты владельца
    _make(3, owner="777")          # чужие
    storage.upsert_user("777", "user777")
    msg, answers = _press(bot_mod, "pg:reply:0", user_id=777, chat_id=777)
    assert len(_contact_rows(msg.markups[0], "reply:")) == 3 and _nav(msg.markups[0]) is None
    # нажатие в чужом чате — ничего не правим
    msg, answers = _press(bot_mod, "pg:reply:1", user_id=777, chat_id=int(OWNER))
    assert msg.markups == [] and answers == [None]


def test_handler_noop_bad_data_and_no_contacts(bot_mod):
    msg, answers = _press(bot_mod, "pg:noop")
    assert msg.markups == [] and answers == [None]
    msg, _ = _press(bot_mod, "pg:unknown:1")
    assert msg.markups == []
    msg, _ = _press(bot_mod, "pg:reply:abc")
    assert msg.markups == []
    msg, answers = _press(bot_mod, "pg:reply:0")  # контактов нет
    assert msg.markups == [] and answers == ["Контактов больше нет."]
