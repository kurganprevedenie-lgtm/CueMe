"""Сравнение «Ответа с CueMe» до/после смены reasoning_effort у Groq.

Пять одинаковых входящих сообщений, нейтральные карточки стиля и
собеседника, только Groq (через set_forced_provider), два прогона:
1) прежние настройки — без reasoning_effort, буфер 900, таймаут 90 с;
2) текущие VARIANTS_* из config.py (по умолчанию low / 300 / 20 с).
Печатает варианты и время каждого вызова — для сравнения качества глазами.

Запуск из корня проекта: python3 -m tools.ab_variants
Тратит 10 запросов к Groq.
"""
import asyncio
import logging
import time

import llm
from config import VARIANTS_REASONING_EFFORT

INPUTS = [
    "ты сегодня какой-то подозрительно долго не писал, случайно не заскучал по мне? 😏",
    "ну не знаю, я в эти выходные вроде занята",
    "ахах, ты всегда такой самоуверенный?",
    "мне сегодня что-то грустно, день вообще не задался",
    "слушай, а давай как-нибудь кофе выпьем?",
]
_STYLE = (
    "Данных о стиле письма пока нет — пиши так, как типично пишут в "
    "дейтинг-переписке в 18-30: на «ты», без канцелярита, разговорной длиной."
)


async def _run(label: str, effort: str) -> None:
    llm.VARIANTS_REASONING_EFFORT = effort
    print(f"\n######## {label} ########", flush=True)
    for text in INPUTS:
        t0 = time.monotonic()
        try:
            variants = await llm.suggest_reply_variants(
                text, _STYLE, llm.NEUTRAL_INTERACTION_PLACEHOLDER, user_gender="male",
            )
        except Exception as e:
            print(f"\n» {text}\n  ОШИБКА: {type(e).__name__}: {str(e)[:150]}", flush=True)
            continue
        print(f"\n» {text}   [{time.monotonic() - t0:.1f} с, вариантов: {len(variants)}]", flush=True)
        for name, body in variants:
            print(f"  {name}: {body}", flush=True)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    llm.set_forced_provider("groq")
    await _run("ДО: без reasoning_effort, буфер 900, таймаут 90", "")
    await _run(f"ПОСЛЕ: reasoning_effort={VARIANTS_REASONING_EFFORT or '—'}", VARIANTS_REASONING_EFFORT)


if __name__ == "__main__":
    asyncio.run(main())
