import json

import pytest

from tg_parser import _normalize_text, parse_chat


# ── _normalize_text ───────────────────────────────────────────────────────────

def test_normalize_plain_string():
    assert _normalize_text("привет") == "привет"


def test_normalize_list_of_entities():
    # Telegram отдаёт text списком строк и объектов-сущностей
    raw = ["смотри ", {"type": "link", "text": "тут"}, "!"]
    assert _normalize_text(raw) == "смотри тут!"


def test_normalize_entity_without_text_key():
    assert _normalize_text([{"type": "custom_emoji"}]) == ""


def test_normalize_non_string_non_list():
    assert _normalize_text(None) == ""
    assert _normalize_text(123) == ""


def test_normalize_empty():
    assert _normalize_text("") == ""
    assert _normalize_text([]) == ""


# ── parse_chat: определение собеседника ───────────────────────────────────────

def _write(tmp_path, messages):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_parse_chat_detects_contact(tmp_path):
    path = _write(tmp_path, [
        {"type": "message", "from_id": "user1", "from": "Я",   "text": "привет", "date": "2026-07-01T10:00:00"},
        {"type": "message", "from_id": "user2", "from": "Аня", "text": "хай",     "date": "2026-07-01T10:01:00"},
    ])
    chat = parse_chat(path, my_id="user1")
    assert chat.meta.contact_id == "user2"
    assert chat.meta.contact_name == "Аня"
    assert len(chat.my_messages) == 1
    assert len(chat.contact_messages) == 1


def test_parse_chat_skips_service_and_group_members(tmp_path):
    path = _write(tmp_path, [
        {"type": "service", "from_id": "user1", "text": "создал группу", "date": "2026-07-01T09:59:00"},
        {"type": "message", "from_id": "user1", "from": "Я",   "text": "здоров", "date": "2026-07-01T10:00:00"},
        {"type": "message", "from_id": "user2", "from": "Аня", "text": "привет", "date": "2026-07-01T10:01:00"},
        {"type": "message", "from_id": "user3", "from": "Третий", "text": "лишний", "date": "2026-07-01T10:02:00"},
    ])
    chat = parse_chat(path, my_id="user1")
    assert chat.meta.contact_id == "user2"
    # user3 (не я и не первый собеседник) — отброшен
    assert chat.meta.total_messages == 2


def test_parse_chat_raises_when_no_contact(tmp_path):
    path = _write(tmp_path, [
        {"type": "message", "from_id": "user1", "from": "Я", "text": "сам с собой", "date": "2026-07-01T10:00:00"},
    ])
    with pytest.raises(ValueError):
        parse_chat(path, my_id="user1")
