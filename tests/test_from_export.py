import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from tg_parser import Message, ParsedChat, ChatMeta
from from_export import lightweight_cards, scenarios_from_parsed

BASE = datetime(2026, 7, 1, 12, 0, 0)


def _msg(from_id, text, offset=0):
    return Message(from_id=from_id, text=text, date=BASE + timedelta(minutes=offset), media_type=None)


def _chat():
    my = [_msg("me", "норм, как ты", 0), _msg("me", "давай в кино", 4)]
    contact = [
        _msg("her", "привет!", 1),
        _msg("her", "", 2),          # пустое — должно отсеяться
        _msg("her", "ок", 6),        # последняя → сухая
    ]
    meta = ChatMeta("C", "her", "me", BASE, BASE, 5)
    return ParsedChat(my_messages=my, contact_messages=contact, meta=meta)


def test_lightweight_cards_use_real_messages():
    style, inter = lightweight_cards(_chat())
    assert "давай в кино" in style           # мои сообщения в карточке голоса
    assert "привет!" in inter                # сообщения собеседника в карточке привычек
    assert "ГОЛОС АВТОРА" in style and "СОБЕСЕДНИКА" in inter


def test_scenarios_from_parsed_shape():
    scs = scenarios_from_parsed(_chat(), max_scenarios=10)
    # только непустые реплики собеседника → 2 сценария
    assert len(scs) == 2
    s = scs[-1]
    assert s["kind"] == "reply"
    assert s["incoming"] == "ок"                     # последняя реплика
    assert "style_card" in s and "interaction_card" in s
    assert s["expects"]["no_foreign"] is True
    # сигнал по сухой реплике присутствует
    assert s["data_signals"] and "сух" in s["data_signals"]


def test_scenarios_respect_limit():
    assert len(scenarios_from_parsed(_chat(), max_scenarios=1)) == 1
