import asyncio
import json
import random

import httpx

from config import LLM_API_KEY
from features import ChatFeatures
from tg_parser import ParsedChat

_MODEL = "llama-3.3-70b-versatile"
_SAMPLE_SIZE = 30
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


async def _ask(prompt: str) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не задан в .env")

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        for attempt in range(2):
            resp = await client.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                },
            )
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(65)
                continue
            if not resp.is_success:
                raise RuntimeError(
                    f"Groq API {resp.status_code}: {resp.text[:500]}"
                )
            return resp.json()["choices"][0]["message"]["content"].strip()


def _sample_texts(messages: list, n: int = _SAMPLE_SIZE) -> list[str]:
    texts = [m.text for m in messages if m.text.strip()]
    return random.sample(texts, min(n, len(texts)))


def _features_summary(f: ChatFeatures) -> str:
    m, c = f.my, f.contact
    return (
        f"Пользователь: {m.total_messages} сообщ., "
        f"средн. длина {m.avg_message_length:.0f} симв., "
        f"вопросы {m.question_ratio:.0%}, "
        f"эмодзи/сообщ {m.emoji_per_message:.1f}, "
        f"инициатива {m.initiative_ratio:.0%}, "
        f"формальность: {m.formality}.\n"
        f"Собеседник: {c.total_messages} сообщ., "
        f"средн. длина {c.avg_message_length:.0f} симв., "
        f"вопросы {c.question_ratio:.0%}, "
        f"эмодзи/сообщ {c.emoji_per_message:.1f}, "
        f"инициатива {c.initiative_ratio:.0%}, "
        f"формальность: {c.formality}."
    )


async def build_cards(chat: ParsedChat, features: ChatFeatures) -> dict:
    """Один LLM-вызов на импорт. Возвращает {'style_card': str, 'interaction_card': str}."""
    my_sample = _sample_texts(chat.my_messages)
    contact_sample = _sample_texts(chat.contact_messages)

    prompt = (
        "Проанализируй переписку и верни JSON с двумя ключами.\n\n"
        f"СТАТИСТИКА:\n{_features_summary(features)}\n\n"
        "СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ (выборка):\n"
        + "\n".join(f"- {t}" for t in my_sample)
        + "\n\nСООБЩЕНИЯ СОБЕСЕДНИКА (выборка):\n"
        + "\n".join(f"- {t}" for t in contact_sample)
        + "\n\n"
        "Верни строго JSON без markdown-обёртки:\n"
        '{\n'
        '  "style_card": "описание голоса и стиля пользователя — тон, приёмы, '
        'что делает его сообщения узнаваемыми. Факты, не интерпретация.",\n'
        '  "interaction_card": "гипотезы о том, как эффективнее писать именно этому '
        'собеседнику: что заходит, какой тон работает, что игнорируется. '
        'Наблюдения по переписке, не психологический портрет."\n'
        '}'
    )

    raw = await _ask(prompt)

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        cards = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Модель вернула невалидный JSON: {e}\nОтвет: {raw[:300]}") from e

    if "style_card" not in cards or "interaction_card" not in cards:
        raise ValueError(f"Ответ модели не содержит нужных ключей: {list(cards.keys())}")

    return cards


async def rewrite_message(draft: str, style_card: str, interaction_card: str) -> str:
    """Переписывает черновик в голосе пользователя под конкретного собеседника."""
    prompt = (
        f"МОЙ СТИЛЬ:\n{style_card}\n\n"
        f"КАК ПИСАТЬ ЭТОМУ СОБЕСЕДНИКУ:\n{interaction_card}\n\n"
        f"МОЙ ЧЕРНОВИК:\n{draft}\n\n"
        "Перепиши черновик: сохрани мой голос, адаптируй под собеседника. "
        "Только итоговое сообщение, без пояснений и кавычек."
    )

    return await _ask(prompt)
