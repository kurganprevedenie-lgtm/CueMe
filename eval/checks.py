"""Детерминированные проверки качества сгенерированного ответа (без LLM).
Чистые функции — их можно юнит-тестировать и переиспользовать в гвардрейлах.
"""
import re

_LATIN = re.compile(r"[A-Za-z]")
# CJK, японская кана, тайский — типичные глитчи llama при подмешивании скриптов
_FOREIGN_SCRIPT = re.compile(r"[一-鿿぀-ヿ฀-๿가-힯]")
_WORD = re.compile(r"\w+", re.UNICODE)

# «Ассистентские» штампы, которых не должно быть в живом ответе.
AI_STOCK_PHRASES = [
    "звучит здорово", "я понимаю, что", "отличный вопрос", "надеюсь, у тебя всё",
    "надеюсь, у тебя все", "рад был помочь", "как я могу помочь", "чем могу помочь",
]

# Недостойный дожим/выпрашивание — недопустимо на отказ/холод.
BEGGING_PHRASES = [
    "давай не будем расставаться", "давай пообщаемся", "не отписывайся",
    "не пропадай", "не уходи", "давай не отписываться", "прошу", "умоляю",
    "дай мне шанс", "не бросай",
]

# Шаблонные зачины, которыми не стоит открывать раз за разом.
CLICHE_OPENERS = {"давай", "слушай", "кстати", "честно"}


def opener_word(text: str) -> str:
    """Первое слово ответа в нижнем регистре (для контроля однообразия зачинов)."""
    m = _WORD.search((text or "").lower())
    return m.group(0) if m else ""


def has_exotic_script(text: str) -> bool:
    """Иероглифы/кана/тай/хангыль — почти всегда глитч модели, жёсткий фейл."""
    return bool(_FOREIGN_SCRIPT.search(text or ""))


def has_latin(text: str) -> bool:
    """Латиница — мягкий флаг: бывает легитимной (бренды, ссылки), но и англ.
    слова-протечки («dry»). Считаем отдельно, не роняем оценку автоматически."""
    return bool(_LATIN.search(text or ""))


def has_foreign_script(text: str) -> bool:
    """Латиница ИЛИ экзотический скрипт (обратная совместимость)."""
    return has_latin(text) or has_exotic_script(text)


def has_ai_stock(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in AI_STOCK_PHRASES)


def has_begging(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in BEGGING_PHRASES)


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def opens_with_cliche(text: str) -> bool:
    return opener_word(text) in CLICHE_OPENERS
