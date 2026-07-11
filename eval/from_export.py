"""Превращает РЕАЛЬНЫЙ Telegram-экспорт (result.json) в eval-сценарии.
Замыкает цикл качества на реальных данных: берём настоящие входящие реплики
собеседника + лёгкие карточки из настоящих сообщений (без LLM), гоняем через
тот же харнесс. Владелец продукта подставляет свой экспорт — и меряет качество
на живых кейсах, а не на синтетике.
"""
from features import detect_reply_situation, stage_hint
from tg_parser import parse_chat


def _texts(msgs) -> list[str]:
    return [m.text.strip() for m in msgs if m.text and m.text.strip()]


def lightweight_cards(parsed, n: int = 15) -> tuple[str, str]:
    """Карточки голоса/привычек прямо из реальных сообщений — без вызова LLM.
    Достаточно, чтобы генерация ловила манеру для eval."""
    mine = _texts(parsed.my_messages)[-n:]
    theirs = _texts(parsed.contact_messages)[-n:]
    style = ("ГОЛОС АВТОРА — реальные примеры его сообщений (перенимай манеру):\n"
             + "\n".join(f"- {t}" for t in mine))
    inter = ("ПРИВЫЧКИ СОБЕСЕДНИКА — реальные примеры его сообщений:\n"
             + "\n".join(f"- {t}" for t in theirs))
    return style, inter


def scenarios_from_parsed(parsed, max_scenarios: int = 15, style: str = "friendly") -> list[dict]:
    """Сценарии-ответы из распарсенного чата: последние реплики собеседника как
    входящие, реальные карточки, реальная стадия + ситуативный сигнал."""
    style_card, inter_card = lightweight_cards(parsed)
    my_n, c_n = len(parsed.my_messages), len(parsed.contact_messages)
    incoming = _texts(sorted(parsed.contact_messages, key=lambda m: m.date))[-max_scenarios:]

    scenarios = []
    for i, inc in enumerate(incoming):
        parts = []
        if my_n + c_n >= 4:
            parts.append(stage_hint(my_n, c_n))
        situ = detect_reply_situation(inc)
        if situ:
            parts.append(situ)
        scenarios.append({
            "id": f"real-{i:02d}", "kind": "reply", "style": style,
            "incoming": inc, "style_card": style_card, "interaction_card": inter_card,
            "data_signals": "\n".join(f"• {p}" for p in parts) if parts else None,
            "expects": {"no_foreign": True, "no_ai_stock": True, "max_words": 40},
        })
    return scenarios


def scenarios_from_export(path: str, my_id: str, max_scenarios: int = 15,
                          style: str = "friendly") -> list[dict]:
    return scenarios_from_parsed(parse_chat(path, my_id), max_scenarios, style)
