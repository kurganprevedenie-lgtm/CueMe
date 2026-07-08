from llm import _split_rated, _split_explained, _parse_blocks, _DELIM, _RATING


# ── _split_explained / _split_rated ───────────────────────────────────────────

def test_split_explained_both_parts():
    raw = f"текст ответа\n{_DELIM}\nпочему так"
    msg, expl = _split_explained(raw)
    assert msg == "текст ответа"
    assert expl == "почему так"


def test_split_explained_no_marker():
    msg, expl = _split_explained("просто текст")
    assert msg == "просто текст"
    assert expl == ""


def test_split_rated_all_three_parts():
    raw = f"ответ\n{_DELIM}\nпояснение\n{_RATING}\n✅ зайдёт"
    msg, expl, rating = _split_rated(raw)
    assert msg == "ответ"
    assert expl == "пояснение"
    assert rating == "✅ зайдёт"


def test_split_rated_missing_rating():
    raw = f"ответ\n{_DELIM}\nтолько пояснение"
    msg, expl, rating = _split_rated(raw)
    assert msg == "ответ"
    assert expl == "только пояснение"
    assert rating == ""


def test_split_rated_plain_text_only():
    msg, expl, rating = _split_rated("голый ответ без маркеров")
    assert msg == "голый ответ без маркеров"
    assert expl == ""
    assert rating == ""


# ── _parse_blocks ─────────────────────────────────────────────────────────────

def test_parse_blocks_single():
    raw = ("<observation>она пишет коротко</observation>"
           "<mechanism>снижение вовлечённости</mechanism>"
           "<action>позови на кофе</action>")
    blocks = _parse_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0] == {
        "observation": "она пишет коротко",
        "mechanism": "снижение вовлечённости",
        "action": "позови на кофе",
    }


def test_parse_blocks_ignores_preamble_and_is_case_insensitive():
    raw = ("бла-бла вводная\n"
           "<OBSERVATION>верхний регистр</OBSERVATION>\n"
           "<Mechanism>смешанный</Mechanism>\n"
           "<action>ок</action>")
    blocks = _parse_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0]["observation"] == "верхний регистр"


def test_parse_blocks_caps_at_three():
    one = "<observation>o</observation><mechanism>m</mechanism><action>a</action>"
    blocks = _parse_blocks(one * 5)
    assert len(blocks) == 3


def test_parse_blocks_empty_on_garbage():
    assert _parse_blocks("никаких тегов тут нет") == []


def test_parse_blocks_strips_whitespace():
    raw = ("<observation>  с пробелами  </observation>"
           "<mechanism>\nперенос\n</mechanism>"
           "<action> ок </action>")
    b = _parse_blocks(raw)[0]
    assert b["observation"] == "с пробелами"
    assert b["mechanism"] == "перенос"
    assert b["action"] == "ок"
