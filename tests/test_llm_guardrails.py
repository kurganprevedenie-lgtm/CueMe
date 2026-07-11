from llm import _strip_wrapping_quotes, _EXOTIC_SCRIPT_RE, _quality_issues


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


def test_quality_issues_catches_prod_failures():
    issues = _quality_issues("кстати, звучит здорово, norm")
    assert any("шаблонный зачин" in i for i in issues)
    assert any("ассистентский штамп" in i for i in issues)
    assert any("латиница" in i for i in issues)


def test_quality_issues_accepts_clean_message():
    assert _quality_issues("грузия звучит как хороший план, что там первое в списке?") == []


from llm import _winning_block


def test_winning_block_empty_when_no_examples():
    assert _winning_block(None) == ""
    assert _winning_block([]) == ""


def test_winning_block_formats_examples():
    b = _winning_block(["рванём в горы?", "покажу лучшие места"])
    assert "ТАК У ТЕБЯ РЕАЛЬНО ЗАХОДИТ" in b
    assert "- «рванём в горы?»" in b and "- «покажу лучшие места»" in b
