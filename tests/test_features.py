from datetime import datetime, timedelta

from tg_parser import Message, ParsedChat, ChatMeta
from features import (
    SESSION_GAP,
    _split_sessions,
    _formality,
    _response_latencies,
    extract_features,
)

BASE = datetime(2026, 7, 1, 12, 0, 0)


def _msg(from_id, text, offset_min=0, media_type=None):
    return Message(from_id=from_id, text=text,
                   date=BASE + timedelta(minutes=offset_min), media_type=media_type)


# ── _split_sessions ───────────────────────────────────────────────────────────

def test_split_sessions_empty():
    assert _split_sessions([]) == []


def test_split_sessions_gap_exactly_4h_stays_one_session():
    # Условие новой сессии — СТРОГО больше SESSION_GAP, значит ровно 4ч → та же сессия
    msgs = [_msg("a", "1", 0), _msg("a", "2", offset_min=SESSION_GAP.total_seconds() / 60)]
    sessions = _split_sessions(msgs)
    assert len(sessions) == 1
    assert len(sessions[0]) == 2


def test_split_sessions_gap_just_over_4h_splits():
    over = SESSION_GAP.total_seconds() / 60 + 1  # на минуту больше 4ч
    msgs = [_msg("a", "1", 0), _msg("a", "2", offset_min=over)]
    sessions = _split_sessions(msgs)
    assert len(sessions) == 2


# ── _formality ────────────────────────────────────────────────────────────────

def test_formality_formal():
    msgs = [_msg("a", "как вы поживаете"), _msg("a", "ваш заказ готов")]
    assert _formality(msgs) == "formal"


def test_formality_informal():
    msgs = [_msg("a", "ты где"), _msg("a", "твой кофе стынет")]
    assert _formality(msgs) == "informal"


def test_formality_mixed():
    msgs = [_msg("a", "ты и вы")]  # 50/50
    assert _formality(msgs) == "mixed"


def test_formality_unknown_without_pronouns():
    msgs = [_msg("a", "погода хорошая")]
    assert _formality(msgs) == "unknown"


# ── _response_latencies ───────────────────────────────────────────────────────

def test_response_latencies_counts_only_cross_replies_within_gap():
    all_msgs = [
        _msg("me", "привет", 0),
        _msg("her", "хай", 2),        # ответ her на me через 2 мин → латентность 120с
        _msg("her", "как ты", 3),     # her подряд, не ответ на me → не считается
        _msg("me", "норм", 10),       # ответ me на her через 7 мин → 420с
    ]
    lat_her = _response_latencies(all_msgs, side_id="her", other_id="me")
    assert lat_her == [120.0]
    lat_me = _response_latencies(all_msgs, side_id="me", other_id="her")
    assert lat_me == [420.0]


def test_response_latencies_ignores_gap_over_session():
    over = SESSION_GAP.total_seconds() / 60 + 5
    all_msgs = [_msg("me", "?", 0), _msg("her", "!", offset_min=over)]
    # разрыв больше сессии — не латентность ответа, а новая сессия
    assert _response_latencies(all_msgs, side_id="her", other_id="me") == []


# ── extract_features: инициатива ──────────────────────────────────────────────

def _chat(my_msgs, contact_msgs):
    meta = ChatMeta(contact_name="C", contact_id="her", my_id="me",
                    date_from=BASE, date_to=BASE, total_messages=len(my_msgs) + len(contact_msgs))
    return ParsedChat(my_messages=my_msgs, contact_messages=contact_msgs, meta=meta)


def test_initiative_ratio_all_sessions_started_by_me():
    over = SESSION_GAP.total_seconds() / 60 + 1
    # Две сессии, обе начаты "me"
    my = [_msg("me", "s1", 0), _msg("me", "s2", offset_min=over)]
    contact = [_msg("her", "r1", 1), _msg("her", "r2", offset_min=over + 1)]
    f = extract_features(_chat(my, contact))
    assert f.my.initiative_ratio == 1.0
    assert f.contact.initiative_ratio == 0.0


def test_extract_features_basic_counts():
    my = [_msg("me", "привет", 0)]
    contact = [_msg("her", "хай?", 1)]
    f = extract_features(_chat(my, contact))
    assert f.my.total_messages == 1
    assert f.contact.total_messages == 1
    # у собеседника сообщение с "?" → доля вопросов 1.0
    assert f.contact.question_ratio == 1.0


def test_question_ratio_denominator_is_text_messages_only():
    # 1 текстовый вопрос + 1 фото без текста: доля должна считаться по текстовым (1/1=1.0),
    # а не по всем сообщениям (было бы 1/2=0.5).
    contact = [
        _msg("her", "как дела?", 0),
        _msg("her", "", 1, media_type="photo"),
    ]
    f = extract_features(_chat([_msg("me", "привет", 0)], contact))
    assert f.contact.question_ratio == 1.0
    assert f.contact.total_messages == 2
    assert f.contact.photo_ratio == 0.5
