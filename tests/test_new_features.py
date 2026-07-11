"""Тесты новых фич-функций (opener/scenario/practice) без живого LLM:
перехватываем llm._ask, проверяем сборку промпта и ветвление."""
import asyncio

import llm


def _run(coro):
    return asyncio.run(coro)


def _patch_ask(monkeypatch, reply="ответ\n===ПОЯСНЕНИЕ===\nтест\n===ОЦЕНКА===\n✅ ок"):
    captured = {}

    async def fake_ask(prompt, **kw):
        captured["prompt"] = prompt
        return reply

    monkeypatch.setattr(llm, "_ask", fake_ask)
    return captured


# ── #1 opener_from_profile ────────────────────────────────────────────────────

def test_opener_uses_profile_and_first_message_frame(monkeypatch):
    cap = _patch_ask(monkeypatch)
    msg, expl, rating = _run(llm.opener_from_profile(
        "любит горы и корги, фото с Эльбруса", "ГОЛОС", "flirt"))
    p = cap["prompt"]
    assert "ПЕРВОЕ сообщение" in p
    assert "любит горы и корги" in p          # анкета в промпте
    assert "кириллица" in p                    # скрипт-гвард
    assert msg == "ответ" and rating.startswith("✅")


# ── #6 scenario_move ──────────────────────────────────────────────────────────

def test_scenario_move_branches_by_scenario(monkeypatch):
    cap = _patch_ask(monkeypatch)
    _run(llm.scenario_move("ghosting", "Ты: привет\nОна: ...", "ГОЛОС", "ИНТ"))
    assert "пропал" in cap["prompt"] and "ре-инициатор" in cap["prompt"]

    _run(llm.scenario_move("move_offline", "Ты: привет\nОна: ага", "ГОЛОС", "ИНТ"))
    assert "предложить встречу" in cap["prompt"]

    # неизвестный сценарий → дефолт deadlock, без падения
    _run(llm.scenario_move("что-то", "к", "ГОЛОС", "ИНТ"))
    assert "тупик" in cap["prompt"]


def test_scenario_move_includes_context_and_dignity(monkeypatch):
    cap = _patch_ask(monkeypatch)
    _run(llm.scenario_move("deadlock", "Ты: как выходные\nОна: норм", "ГОЛОС", "ИНТ"))
    assert "как выходные" in cap["prompt"]
    assert "Достоинство важнее удержания" in cap["prompt"]


# ── #2 practice ───────────────────────────────────────────────────────────────

def test_practice_reply_stays_in_character_and_strips_quotes(monkeypatch):
    cap = _patch_ask(monkeypatch, reply='«ну привет, чего хотел?»')
    out = _run(llm.practice_reply("любит иронию", "Ты: привет", "как настроение?"))
    p = cap["prompt"]
    assert "ОТЫГРЫВАЕШЬ собеседника" in p
    assert "любит иронию" in p and "как настроение?" in p
    assert "не выходи из роли" in p
    assert out == "ну привет, чего хотел?"     # кавычки сняты


def test_practice_debrief_structure(monkeypatch):
    cap = _patch_ask(monkeypatch, reply="💪 ок\n⚠️ подтяни\n🎯 попробуй")
    out = _run(llm.practice_debrief("любит юмор", "Ты: привет\nОна: хай"))
    assert "тренировочный диалог" in cap["prompt"]
    assert "💪" in out and "🎯" in out
