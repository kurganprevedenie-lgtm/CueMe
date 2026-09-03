"""Тесты слоя suggestions/suggestion_matches: сохранение показанных подсказок,
сопоставление с реальными исходящими business-сообщениями (main.py), badge в
экспорте (tools/export.py), агрегированная статистика."""
from datetime import datetime, timedelta, timezone

import pytest

import storage
from tools.export import extract_conversation, to_html, to_text


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    yield


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def test_save_and_fetch_recent_unmatched_suggestions(db):
    storage.save_suggestions("u1", 5, "reply", ["Привет, как дела?", "Йо, что нового?"])
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = storage.get_recent_unmatched_suggestions("u1", 5, since)
    assert {r["suggestion_text"] for r in rows} == {"Привет, как дела?", "Йо, что нового?"}
    # другой контакт/юзер не видит чужие подсказки
    assert storage.get_recent_unmatched_suggestions("u1", 6, since) == []
    assert storage.get_recent_unmatched_suggestions("u2", 5, since) == []


def test_matched_suggestion_not_returned_again(db):
    storage.save_suggestions("u1", 5, "reply", ["Привет, как дела?"])
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    row = storage.get_recent_unmatched_suggestions("u1", 5, since)[0]
    storage.mark_suggestion_matched(row["id"], business_message_id=1, ratio=0.9, match_kind="exact")
    assert storage.get_recent_unmatched_suggestions("u1", 5, since) == []


def test_business_message_id_returned_and_dedup(db):
    kwargs = dict(
        connection_id="c1", owner_user_id="u1", chat_ref="ref1", direction="out",
        text="привет", date=_now_iso(), tg_message_id=100, raw_meta={},
    )
    first_id = storage.save_business_message(**kwargs)
    assert isinstance(first_id, int)
    dup_id = storage.save_business_message(**kwargs)  # тот же tg_message_id — дубль
    assert dup_id is None


def test_full_matching_pipeline_exact_and_edited():
    """End-to-end через public API storage.py (без main.py, чтобы не тянуть
    aiogram/BOT_TOKEN в тест) — воспроизводит ровно то, что делает
    main._match_outgoing_to_suggestion."""
    import re as _re

    def normalize(t):
        return _re.sub(r"\s+", " ", t.lower()).strip(" .,!?\"'«»—–…").strip()

    import difflib

    def ratio(a, b):
        return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()

    assert ratio("Привет, как дела?", "привет, как дела?") >= 0.85
    assert 0.5 <= ratio("Привет, как твои дела сегодня?", "привет как дела") < 0.85
    assert ratio("Привет, как дела?", "Пошли гулять вечером") < 0.5


def test_export_shows_cueme_badge(db, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", storage.DB_PATH)  # уже подменено фикстурой db
    storage.upsert_user("u1", "me1")
    cid = storage.get_or_create_contact("u1", "user9", "Аня")
    storage.upsert_chat_ref_mapping("u1", "ref1", cid)

    date = "2026-07-01T10:00:00+00:00"
    msg_id = storage.save_business_message(
        connection_id="c1", owner_user_id="u1", chat_ref="ref1", direction="out",
        text="привет как дела", date=date, tg_message_id=1, raw_meta={},
    )
    storage.save_suggestions("u1", cid, "reply", ["привет как дела"])
    suggestion = storage.get_recent_unmatched_suggestions(
        "u1", cid, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )[0]
    storage.mark_suggestion_matched(suggestion["id"], msg_id, ratio=0.97, match_kind="exact")

    export = extract_conversation(cid)
    out_msg = next(m for m in export["messages"] if m["from"] == "Я")
    assert out_msg["cueme_match"]["match_kind"] == "exact"

    text_out = to_text(export)
    assert "🤖 CueMe (97%)" in text_out

    html_out = to_html(export)
    assert "cueme-badge" in html_out
    assert "CueMe" in html_out


def test_suggestion_stats_by_user_aggregates(db):
    storage.save_suggestions("u1", 1, "reply", ["a", "b", "c"])
    rows = storage.get_recent_unmatched_suggestions(
        "u1", 1, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    storage.mark_suggestion_matched(rows[0]["id"], business_message_id=101, ratio=0.9, match_kind="exact")
    storage.mark_suggestion_matched(rows[1]["id"], business_message_id=102, ratio=0.6, match_kind="edited")
    # rows[2] остаётся неиспользованной

    stats = {r["telegram_id"]: r for r in storage.suggestion_stats_by_user()}
    s = stats["u1"]
    assert s["total"] == 3
    assert s["exact_n"] == 1
    assert s["edited_n"] == 1
