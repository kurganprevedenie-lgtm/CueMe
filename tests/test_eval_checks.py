import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from checks import (
    opener_word,
    has_foreign_script,
    has_exotic_script,
    has_latin,
    has_ai_stock,
    has_begging,
    word_count,
    opens_with_cliche,
)
from run_eval import effective_score


def test_opener_word():
    assert opener_word("Давай сходим") == "давай"
    assert opener_word("  ну ок") == "ну"
    assert opener_word("") == ""


def test_has_foreign_script():
    assert has_foreign_script("ok норм") is True          # латиница
    assert has_foreign_script("✅正常но") is True           # иероглиф
    assert has_foreign_script("с dry юмором") is True      # англ. слово
    assert has_foreign_script("привет, как дела? 😄") is False
    assert has_foreign_script("норм, увидимся в 7") is False


def test_exotic_vs_latin_split():
    # экзотика — жёсткий фейл (глитч), латиница — мягкий флаг (бренды легитимны)
    assert has_exotic_script("✅正常но") is True
    assert has_exotic_script("го в McDonalds") is False
    assert has_latin("го в McDonalds") is True
    assert has_latin("норм, увидимся 😄") is False


def test_effective_score_hybrid():
    assert effective_score(9, has_hard_fail=False) == 9   # чисто → как есть
    assert effective_score(8, has_hard_fail=True) == 3    # нарушение роняет до ≤3
    assert effective_score(2, has_hard_fail=True) == 2    # уже ниже — не поднимаем


def test_has_ai_stock():
    assert has_ai_stock("Звучит здорово, давай!") is True
    assert has_ai_stock("Я понимаю, что тебе тяжело") is True
    assert has_ai_stock("норм, а ты куда хочешь?") is False


def test_has_begging():
    assert has_begging("давай не будем расставаться") is True
    assert has_begging("ну давай пообщаемся ещё") is True
    assert has_begging("ок, оставляю дверь открытой") is False


def test_word_count():
    assert word_count("норм увидимся завтра") == 3
    assert word_count("") == 0


def test_opens_with_cliche():
    assert opens_with_cliche("давай кофе") is True
    assert opens_with_cliche("слушай, а помнишь") is True
    assert opens_with_cliche("грузия крутая, кстати") is False
