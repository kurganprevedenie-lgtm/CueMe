import json

import pytest

import storage
from tg_parser import parse_chat
from tools.export import extract_conversation, to_text


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    yield


def _seed(owner="u1", my="me42", contact_fid="user999", name="Аня"):
    storage.upsert_user(owner, my)
    cid = storage.get_or_create_contact(owner, contact_fid, name)
    storage.save_imported_messages(cid, [
        {"direction": "out", "text": "привет", "date": "2026-07-01T10:00:00+00:00"},
        {"direction": "in",  "text": "хай!",   "date": "2026-07-01T10:01:00+00:00"},
        {"direction": "out", "text": "как ты",  "date": "2026-07-01T10:02:00+00:00"},
    ])
    return cid, owner, my, contact_fid, name


def test_extract_maps_direction_and_sorts(db):
    cid, owner, my, cfid, name = _seed()
    exp = extract_conversation(cid)
    assert exp["my_id"] == my and exp["contact_name"] == name
    assert [m["text"] for m in exp["messages"]] == ["привет", "хай!", "как ты"]  # по дате
    assert exp["messages"][0]["from_id"] == my    # out → мой id
    assert exp["messages"][1]["from_id"] == cfid   # in → id собеседника
    assert exp["messages"][0]["type"] == "message"


def test_extract_unknown_contact_raises(db):
    with pytest.raises(ValueError):
        extract_conversation(999999)


def test_roundtrip_through_tg_parser(db, tmp_path):
    cid, owner, my, cfid, name = _seed()
    path = tmp_path / "chat.json"
    path.write_text(json.dumps(extract_conversation(cid), ensure_ascii=False), encoding="utf-8")
    # то, что выгрузили, обратно парсится eval-парсером
    parsed = parse_chat(str(path), my)
    assert [m.text for m in parsed.my_messages] == ["привет", "как ты"]
    assert [m.text for m in parsed.contact_messages] == ["хай!"]


def test_to_text_format(db):
    cid, *_ = _seed()
    txt = to_text(extract_conversation(cid))
    assert "Я: привет" in txt and "Аня: хай!" in txt


def test_list_contacts_with_counts(db):
    _seed(owner="u1", contact_fid="user1", name="Аня")
    cid2 = storage.get_or_create_contact("u1", "user2", "Маша")
    storage.save_imported_messages(cid2, [
        {"direction": "in", "text": "йо", "date": "2026-07-02T09:00:00+00:00"},
    ])
    from tools.export import list_contacts
    rows = {r["name"]: r["messages"] for r in list_contacts()}
    assert rows == {"Аня": 3, "Маша": 1}
