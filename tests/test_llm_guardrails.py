from llm import _strip_wrapping_quotes, _EXOTIC_SCRIPT_RE


def test_strip_wrapping_quotes_removes_outer():
    assert _strip_wrapping_quotes('«норм, увидимся»') == "норм, увидимся"
    assert _strip_wrapping_quotes('"привет"') == "привет"
    assert _strip_wrapping_quotes("  «ок»  ") == "ок"


def test_strip_wrapping_quotes_keeps_inner():
    assert _strip_wrapping_quotes('скажи ей "да"') == 'скажи ей "да"'
    assert _strip_wrapping_quotes("норм") == "норм"


def test_strip_wrapping_quotes_nested():
    assert _strip_wrapping_quotes('«"го гулять"»') == "го гулять"


def test_exotic_script_detects_glitches():
    assert _EXOTIC_SCRIPT_RE.search("✅正常но")          # иероглиф
    assert _EXOTIC_SCRIPT_RE.search("собеседникаตอบ")    # тай
    # латиница и обычный русский — НЕ экзотика (не трогаем)
    assert not _EXOTIC_SCRIPT_RE.search("го в McDonalds")
    assert not _EXOTIC_SCRIPT_RE.search("норм, увидимся в 7 😄")
