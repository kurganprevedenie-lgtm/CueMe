"""Тесты слоя БД (storage.py). Каждый тест — на изолированной временной БД:
storage.DB_PATH подменяется на файл в tmp_path, схема поднимается init_db().
"""
import pytest

import storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    yield


# ── users ────────────────────────────────────────────────────────────────────

def test_upsert_and_get_user(db):
    assert storage.get_user("u1") is None
    storage.upsert_user("u1", "me123")
    row = storage.get_user("u1")
    assert row["telegram_id"] == "u1"
    assert row["my_id"] == "me123"


def test_upsert_user_updates_my_id(db):
    storage.upsert_user("u1", "me123")
    storage.upsert_user("u1", "me999")   # конфликт по PK → апдейт
    assert storage.get_user("u1")["my_id"] == "me999"
    # запись одна, не дубль
    with storage._conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1


def test_trial_counter(db):
    storage.upsert_user("u1", "me")
    assert storage.get_trial_used("u1") == 0
    storage.increment_trial_used("u1")
    storage.increment_trial_used("u1")
    assert storage.get_trial_used("u1") == 2
    # неизвестный юзер → 0, без падения
    assert storage.get_trial_used("nope") == 0


# ── contacts ─────────────────────────────────────────────────────────────────

def test_get_or_create_contact_idempotent(db):
    cid1 = storage.get_or_create_contact("u1", "user555", "Аня")
    cid2 = storage.get_or_create_contact("u1", "user555", "Аня")
    assert cid1 == cid2  # тот же контакт, не дубль
    cid3 = storage.get_or_create_contact("u1", "user777", "Маша")
    assert cid3 != cid1
    names = {c["display_name"] for c in storage.list_contacts("u1")}
    assert names == {"Аня", "Маша"}


def test_get_contact_by_id(db):
    cid = storage.get_or_create_contact("u1", "user1", "Аня")
    assert storage.get_contact_by_id(cid)["display_name"] == "Аня"
    assert storage.get_contact_by_id(999999) is None


# ── style / interaction cards ────────────────────────────────────────────────

def test_style_card_roundtrip_and_delete(db):
    assert storage.get_style_card("u1") is None
    storage.save_style_card("u1", "мой голос")
    assert storage.get_style_card("u1") == "мой голос"
    storage.save_style_card("u1", "обновлённый голос")  # upsert
    assert storage.get_style_card("u1") == "обновлённый голос"
    storage.delete_style_card("u1")
    assert storage.get_style_card("u1") is None


def test_interaction_card_roundtrip(db):
    cid = storage.get_or_create_contact("u1", "user1", "Аня")
    assert storage.get_interaction_card(cid) is None
    storage.save_interaction_card(cid, "как ей писать")
    assert storage.get_interaction_card(cid) == "как ей писать"


# ── message samples ──────────────────────────────────────────────────────────

def test_message_samples_roundtrip(db):
    cid = storage.get_or_create_contact("u1", "user1", "Аня")
    storage.save_message_samples(cid, ["мой1", "мой2"], ["её1"], "сводка-фич")
    s = storage.get_message_samples(cid)
    assert s["my_sample"] == ["мой1", "мой2"]
    assert s["contact_sample"] == ["её1"]
    assert s["features_summary"] == "сводка-фич"
    assert storage.get_message_samples(999999) is None


# ── llm cache ────────────────────────────────────────────────────────────────

def test_llm_cache_roundtrip_and_ttl(db):
    assert storage.get_llm_cache("k", 100) is None
    storage.set_llm_cache("k", "результат")
    assert storage.get_llm_cache("k", 100) == "результат"
    # ttl истёк → None (запись есть, но старше max_age)
    assert storage.get_llm_cache("k", 0) is None


def test_llm_cache_overwrite(db):
    storage.set_llm_cache("k", "v1")
    storage.set_llm_cache("k", "v2")  # INSERT OR REPLACE
    assert storage.get_llm_cache("k", 100) == "v2"
