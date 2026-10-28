"""«Идеальное свидание»: проверка ответа модели и разбор на блоки (ложные
алерты «нет маркера 💐» — заголовок ставит main.py, модель его писать не должна)."""
import asyncio

import pytest

import llm
from llm import ProviderError, validate_llm_output

IDEA = "Она упоминала, что любит суши, поэтому позови её на мастер-класс по роллам в субботу."
GIFTS = "Судя по тому, что она говорила про керамику, — зайдёт набор для лепки."


def _run_build(monkeypatch, raw):
    async def fake_ask(prompt, max_tokens, tag=None, fast=False):
        assert tag == "ideal_date"
        return raw
    monkeypatch.setattr(llm, "_ask", fake_ask)
    return asyncio.run(llm.build_ideal_date(["я люблю суши"], [], "", ""))


# (а) без 💐, но с ===ПОДАРКИ=== и обоими блоками — валидный
def test_no_flower_with_marker_and_both_blocks_is_valid(monkeypatch):
    raw = f"{IDEA}\n===ПОДАРКИ===\n{GIFTS}"
    assert validate_llm_output(raw, "ideal_date") is None
    date_idea, gift_ideas = _run_build(monkeypatch, raw)
    assert date_idea == IDEA
    assert gift_ideas == f"🎁 Как порадовать\n\n{GIFTS}"


# (б) с 🎁, но без ===ПОДАРКИ=== — невалидный
def test_gift_emoji_without_marker_is_invalid():
    raw = f"{IDEA}\n\n🎁 Как порадовать\n\n{GIFTS}"
    assert validate_llm_output(raw, "ideal_date") == "нет маркера ===ПОДАРКИ==="


# (в) модель сама написала «💐 Идеальное свидание» — заголовок вырезается
@pytest.mark.parametrize("header", [
    "💐 Идеальное свидание", "💐 Идеальное свидание — Маша", "Идеальное свидание:", "БЛОК 1:",
])
def test_model_written_headers_are_stripped(monkeypatch, header):
    raw = f"{header}\n\n{IDEA}\n===ПОДАРКИ===\n🎁 Как порадовать\n\n{GIFTS}"
    assert validate_llm_output(raw, "ideal_date") is None
    date_idea, gift_ideas = _run_build(monkeypatch, raw)
    assert date_idea == IDEA
    assert gift_ideas == f"🎁 Как порадовать\n\n{GIFTS}"  # без дубля заголовка


def test_content_line_starting_with_podarki_is_kept():
    _, gifts = llm._split_ideal_date(f"{IDEA}\n===ПОДАРКИ===\nПодарки: сертификат на мастер-класс по керамике.")
    assert gifts == "Подарки: сертификат на мастер-класс по керамике."


# (г) английские рассуждения — по-прежнему отбраковываются
def test_english_reasoning_is_still_rejected():
    raw = ("The user says she likes sushi. Let's suggest a sushi place. Draft: суши.\n"
           f"===ПОДАРКИ===\n{GIFTS}")
    assert validate_llm_output(raw, "ideal_date") is not None


# пункт 4: пустой блок — невалидный (идёт следующей модели/провайдеру)
@pytest.mark.parametrize("raw, reason", [
    (f"💐 Идеальное свидание\n===ПОДАРКИ===\n{GIFTS}", "пустой блок идеи свидания"),
    (f"{IDEA}\n===ПОДАРКИ===\n🎁 Как порадовать\n", "пустой блок подарков"),
    (f"{IDEA}\n===ПОДАРКИ===", "пустой блок подарков"),
])
def test_empty_block_is_invalid(raw, reason):
    assert validate_llm_output(raw, "ideal_date") == reason


def test_empty_block_raises_in_build(monkeypatch):
    with pytest.raises(ProviderError):
        _run_build(monkeypatch, f"{IDEA}\n===ПОДАРКИ===\n")


class _Fake(llm.LLMProvider):
    def __init__(self, name, text):
        self.name, self.text = name, text

    async def ask(self, prompt, max_tokens):
        return llm._check_output(self.name, "", self.text)


def test_empty_block_goes_to_next_provider(monkeypatch):
    bad = f"{IDEA}\n===ПОДАРКИ===\n🎁 Как порадовать"
    good = f"{IDEA}\n===ПОДАРКИ===\n{GIFTS}"
    monkeypatch.setattr(llm, "_ordered_providers", lambda: [_Fake("Gemini", bad), _Fake("OpenRouter", good)])
    seen = []
    llm.set_invalid_output_hook(lambda p, t, r: seen.append((p, t, r)))
    try:
        assert asyncio.run(llm._ask("p", 100, tag="ideal_date")) == good
    finally:
        llm.set_invalid_output_hook(None)
    assert seen == [("Gemini", "ideal_date", "пустой блок подарков")]
