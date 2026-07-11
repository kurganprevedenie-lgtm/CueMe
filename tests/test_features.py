from datetime import datetime, timedelta

from tg_parser import Message, ParsedChat, ChatMeta
from features import (
    SESSION_GAP,
    _split_sessions,
    _formality,
    _response_latencies,
    extract_features,
    detect_reply_situation,
    stage_hint,
    totals_from_summary,
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


# ── detect_reply_situation ──────────────────────────────────────────────────────

def test_situation_none_for_normal_message():
    assert detect_reply_situation("расскажи, как прошёл твой день?") is None
    assert detect_reply_situation("") is None
    assert detect_reply_situation("   ") is None


def test_situation_negative_rejection():
    for t in ["не хочу с тобой общаться", "отстань", "давай не будем", "мне всё равно"]:
        assert detect_reply_situation(t) is not None
        assert "достоинством" in detect_reply_situation(t)


def test_situation_dry_one_word_ack():
    for t in ["ок", "Угу", "ясно", "нз"]:
        hint = detect_reply_situation(t)
        assert hint is not None and "сух" in hint


def test_situation_short_but_warm_not_flagged():
    # короткое, но не из сухого списка и не негатив — не помечаем как тяжёлое
    assert detect_reply_situation("привет!") is None
    assert detect_reply_situation("спасибо ❤️") is None


# ── stage_hint ──────────────────────────────────────────────────────────────────

def test_stage_hint_buckets():
    assert "свежее знакомство" in stage_hint(3, 2)
    assert "общение уже идёт" in stage_hint(30, 30)
    assert "давняя переписка" in stage_hint(200, 200)


def test_stage_hint_handles_none_like_zeros():
    assert "свежее знакомство" in stage_hint(0, 0)


# ── totals_from_summary ─────────────────────────────────────────────────────────

def test_totals_from_summary_parses_real_format():
    from llm import make_features_summary
    from features import ChatFeatures, SideFeatures

    def side(n):
        return SideFeatures(total_messages=n, avg_message_length=42.0,
                            avg_response_latency_sec=None, question_ratio=0.2,
                            emoji_per_message=0.3, initiative_ratio=0.5,
                            photo_ratio=0.0, formality="informal")

    summary = make_features_summary(ChatFeatures(my=side(137), contact=side(89)))
    assert totals_from_summary(summary) == (137, 89)


def test_totals_from_summary_none_on_garbage():
    assert totals_from_summary("") is None
    assert totals_from_summary("нет чисел про сообщения") is None


# ── winning_messages ────────────────────────────────────────────────────────────

from features import winning_messages


def _m(direction, text, iso):
    return {"direction": direction, "text": text, "date": iso}


def test_winning_picks_out_msgs_with_lively_reply():
    msgs = [
        _m("out", "как настроение? рванём в горы на выходных?", "2026-07-01T10:00:00"),
        _m("in",  "ооо да, давно хотела, куда именно?",        "2026-07-01T10:05:00"),  # живой ответ → win
        _m("out", "ну ок",                                     "2026-07-01T11:00:00"),
        _m("in",  "угу",                                       "2026-07-01T11:02:00"),  # сухо → не win
    ]
    wins = winning_messages(msgs)
    assert wins == ["как настроение? рванём в горы на выходных?"]


def test_winning_excludes_slow_and_negative_replies():
    msgs = [
        _m("out", "давай на кофе сходим",   "2026-07-01T10:00:00"),
        _m("in",  "не хочу, отстань",       "2026-07-01T10:03:00"),  # негатив → не win
        _m("out", "ну как знаешь, интересный был вечер вчера", "2026-07-02T10:00:00"),
        _m("in",  "да, согласна полностью", "2026-07-03T20:00:00"),  # ответ через сутки → слишком долго
    ]
    assert winning_messages(msgs) == []


def test_winning_dedup_and_recency_and_limit():
    msgs = []
    for i in range(5):
        base = f"2026-07-0{i+1}T10:0"
        msgs.append(_m("out", f"классный заход номер {i}", base + "0:00"))
        msgs.append(_m("in", "ого расскажи подробнее пожалуйста", base + "3:00"))
    wins = winning_messages(msgs, max_examples=2)
    assert len(wins) == 2
    assert wins[0] == "классный заход номер 4"   # самый свежий первым
