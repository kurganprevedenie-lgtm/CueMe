import asyncio
import random

import httpx

from config import LLM_API_KEY
from features import ChatFeatures

_MODEL    = "llama-3.3-70b-versatile"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


async def _ask(prompt: str, max_tokens: int = 1024) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не задан в .env")

    async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
        for attempt in range(2):
            resp = await client.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(65)
                continue
            if not resp.is_success:
                raise RuntimeError(f"Groq API {resp.status_code}: {resp.text[:500]}")
            return resp.json()["choices"][0]["message"]["content"].strip()


def sample_texts(messages: list, n: int = 30) -> list[str]:
    texts = [m.text for m in messages if m.text and m.text.strip()]
    return random.sample(texts, min(n, len(texts)))


def make_features_summary(f: ChatFeatures) -> str:
    """Полная статистика — для interaction_card."""
    m, c = f.my, f.contact
    return (
        f"Пользователь: {m.total_messages} сообщ., "
        f"средн. длина {m.avg_message_length:.0f} симв., "
        f"вопросы {m.question_ratio:.0%}, "
        f"эмодзи/сообщ {m.emoji_per_message:.2f}, "
        f"инициатива {m.initiative_ratio:.0%}, "
        f"формальность: {m.formality}.\n"
        f"Собеседник: {c.total_messages} сообщ., "
        f"средн. длина {c.avg_message_length:.0f} симв., "
        f"вопросы {c.question_ratio:.0%}, "
        f"эмодзи/сообщ {c.emoji_per_message:.2f}, "
        f"инициатива {c.initiative_ratio:.0%}, "
        f"формальность: {c.formality}."
    )


def make_user_features_summary(f: ChatFeatures) -> str:
    """Только сторона пользователя — для style_card."""
    m = f.my
    return (
        f"Всего сообщений: {m.total_messages}, "
        f"средн. длина {m.avg_message_length:.0f} симв., "
        f"вопросы {m.question_ratio:.0%}, "
        f"эмодзи/сообщ {m.emoji_per_message:.2f}, "
        f"инициатива {m.initiative_ratio:.0%}, "
        f"формальность: {m.formality}."
    )


async def build_style_card(my_sample: list[str], user_features_summary: str) -> str:
    """Анализ голоса пользователя. Возвращает plain text."""
    prompt = (
        "Проанализируй сообщения пользователя.\n"
        "Верни ТОЛЬКО текст анализа — без JSON, без кавычек, без markdown.\n"
        "Просто текст с заголовками секций и пунктами через •.\n\n"
        f"СТАТИСТИКА:\n{user_features_summary}\n\n"
        "СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n"
        + "\n".join(f"- {t}" for t in my_sample)
        + "\n\n"
        "ПРАВИЛА (строго):\n"
        "• Только конкретные факты из сообщений — никаких общих слов\n"
        "• Каждый пункт: наблюдение + пример-цитата\n"
        "• Статистика — первичный источник. Если emoji/сообщ < 0.3 → «эмодзи почти не использует»\n"
        "• Регистр — обязательный пункт: пишет с большой или маленькой — проверь\n"
        "• Запрещено: «общительный», «тёплый», «использует юмор» — без конкретики\n\n"
        "ФОРМАТ (секции разделены пустой строкой):\n\n"
        "ГОЛОС И ТОН\n"
        "• [факт + пример-цитата]\n\n"
        "СТРУКТУРА СООБЩЕНИЙ\n"
        "• [типичная длина в словах + пример]\n"
        "• [пунктуация, абзацы]\n\n"
        "СЛОВАРНЫЙ ЗАПАС\n"
        "• [характерные слова — цитаты]\n\n"
        "ЮМОР И ЭМОЦИИ\n"
        "• [как выражает, с примером]\n\n"
        "РЕГИСТР И ИНИЦИАТИВА\n"
        "• [с большой или маленькой — факт]\n"
        "• [кто начинает темы]"
    )
    return await _ask(prompt, max_tokens=1500)


async def build_interaction_card(
    my_sample: list[str],
    contact_sample: list[str],
    features_summary: str,
) -> str:
    """Наблюдения о собеседнике. Возвращает plain text."""
    prompt = (
        "Проанализируй как СОБЕСЕДНИК общается в этой переписке.\n"
        "Верни ТОЛЬКО текст анализа — без JSON, без кавычек, без markdown.\n"
        "Просто текст с заголовками секций и пунктами через •.\n\n"
        f"СТАТИСТИКА:\n{features_summary}\n\n"
        "СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n"
        + "\n".join(f"- {t}" for t in my_sample)
        + "\n\nСООБЩЕНИЯ СОБЕСЕДНИКА:\n"
        + "\n".join(f"- {t}" for t in contact_sample)
        + "\n\n"
        "ПРАВИЛА (строго):\n"
        "• Конкретные наблюдения + пример-цитата из сообщений\n"
        "• ЗАПРЕЩЕНЫ общие слова без примеров: «общается дружелюбно», «любит юмор»\n"
        "• Статистика — первичный источник для эмодзи и длины\n"
        "• Это наблюдения о том КАК человек общается — НЕ психологический портрет\n\n"
        "ФОРМАТ (секции разделены пустой строкой):\n\n"
        "ДЛИНА СООБЩЕНИЙ\n"
        "• Типичный размер в словах (диапазон) + пример из переписки\n\n"
        "ТЕМП И РИТМ\n"
        "• Характерное время ответа, паузы\n\n"
        "РЕГИСТР И ЯЗЫК\n"
        "• Ты/Вы, с большой или маленькой — проверь в сообщениях\n"
        "• Характерный сленг (цитаты)\n"
        "• Эмодзи: взять цифру из статистики, не придумывать\n\n"
        "ИНИЦИАТИВА\n"
        "• Кто чаще пишет первым; задаёт ли встречные вопросы\n\n"
        "ЧТО ДАЁТ ЖИВОЙ ОТКЛИК\n"
        "• Конкретный пример из переписки когда ответил развёрнуто\n\n"
        "ЧТО НЕ РАБОТАЕТ\n"
        "• Конкретный пример когда ответ был сухим или его не было\n\n"
        "КАК ПИСАТЬ ЭТОМУ ЧЕЛОВЕКУ\n"
        "• [3-4 конкретных практических совета]"
    )
    return await _ask(prompt, max_tokens=2000)


async def rewrite_message(draft: str, style_card: str, interaction_card: str) -> str:
    """Переписывает черновик в голосе пользователя под конкретного собеседника."""
    prompt = (
        "Ты переписываешь сообщение от лица конкретного человека — под конкретного собеседника.\n\n"
        f"ГОЛОС АВТОРА (style_card):\n{style_card}\n\n"
        f"СОБЕСЕДНИК — его привычки и что у него заходит (interaction_card):\n{interaction_card}\n\n"
        f"ЧЕРНОВИК АВТОРА:\n{draft}\n\n"
        "=== ЧТО СОХРАНЯЕМ (голос автора из style_card) ===\n"
        "• Личный словарный запас — его слова и обороты, не чужие\n"
        "• Смысл и суть — не меняй что он хочет сказать\n"
        "• Регистр — если пишет с маленькой буквы, оставь маленькую\n"
        "• Эмодзи — если в черновике нет, не добавляй\n\n"
        "=== ЧТО МЕНЯЕМ (адаптация под собеседника из interaction_card) ===\n"
        "• Длина — если собеседник любит короткие: режь до сути; если длинные: разворачивай\n"
        "• Тон и теплота — под то, что у него даёт живой отклик\n"
        "• Первая фраза / заход — измени чтобы цеплял этого человека\n"
        "• Формальность — под его уровень: ты/Вы, официально/неформально\n"
        "• Ритм и структура — под его привычки читать и отвечать\n\n"
        "=== КРИТЕРИИ ===\n"
        "ПРОВАЛ — только исправлена орфография/пунктуация, подача та же.\n"
        "УСПЕХ — заметно изменена длина, тон, заход или ритм под этого конкретного человека.\n\n"
        "ПРИМЕРЫ ожидаемой глубины:\n"
        "Черновик: «хотел спросить, ты не забыл про встречу в пятницу?»\n"
        "→ Для лаконика: «пятница, встреча — помнишь?»\n"
        "→ Для болтуна: «слушай, мы же договаривались на пятницу? или я что-то напутал»\n\n"
        "Верни только итоговое переписанное сообщение — без кавычек, без пояснений."
    )
    return await _ask(prompt)
