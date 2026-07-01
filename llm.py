"""
llm.py — обёртки над LLM-провайдерами с каскадным fallback.

Порядок попыток:
  1. Gemini      (gemini-2.5-flash)           — основной
  2. Groq        (llama-3.3-70b-versatile)    — fallback 1
  3. OpenRouter  (meta-llama/llama-3.1-8b-instruct:free) — fallback 2

Если все три провайдера недоступны — пробрасывается последнее исключение.
"""

import base64
import logging
import random
import re
from abc import ABC, abstractmethod

import httpx

from config import GEMINI_API_KEY, GEMINI_PROXY, GROQ_API_KEY, OPENROUTER_API_KEY, VISION_MODEL
from features import ChatFeatures

log = logging.getLogger(__name__)

# ── Исключения ────────────────────────────────────────────────────────────────

class RateLimitError(RuntimeError):
    """Все провайдеры вернули 429 / дневной лимит исчерпан."""


class ProviderError(RuntimeError):
    """Временная ошибка одного провайдера (5xx, таймаут). Триггерит fallback."""


# ── Абстрактный провайдер ─────────────────────────────────────────────────────

class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def ask(self, prompt: str, max_tokens: int) -> str:
        """Отправляет prompt, возвращает текст ответа.

        Raises:
            RateLimitError: лимит исчерпан, повтор бесполезен.
            ProviderError:  временная ошибка, следующий провайдер может помочь.
        """


# ── Транскрипция голосовых (Groq Whisper) ────────────────────────────────────

_WHISPER_URL   = "https://api.groq.com/openai/v1/audio/transcriptions"
_WHISPER_MODEL = "whisper-large-v3-turbo"


async def transcribe_audio(data: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровывает голосовое в текст через Groq Whisper. Пусто при ошибке/без ключа."""
    if not GROQ_API_KEY:
        log.warning("Whisper: GROQ_API_KEY не задан — транскрипция недоступна")
        return ""
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            resp = await client.post(
                _WHISPER_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filename, data, "application/octet-stream")},
                data={"model": _WHISPER_MODEL},
            )
        if resp.is_success:
            return resp.json().get("text", "").strip()
        log.warning("Whisper %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("Whisper: ошибка запроса — %s", e)
    return ""


# ── Распознавание скриншотов (Groq Vision) ───────────────────────────────────

_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
ILLEGIBLE_MARKER = "ТЕКСТ_НЕЧИТАЕМ"
_MAX_IMAGE_B64_BYTES = 4 * 1024 * 1024  # лимит Groq на base64 image_url


async def extract_chat_from_image(image_bytes: bytes) -> str:
    """Распознаёт диалог со скриншота через Groq Vision. Пусто при ошибке/без ключа."""
    if not GROQ_API_KEY:
        log.warning("Vision: GROQ_API_KEY не задан")
        return ""
    b64 = base64.b64encode(image_bytes).decode()
    if len(b64) > _MAX_IMAGE_B64_BYTES:
        log.warning("Vision: изображение больше лимита Groq (4MB base64)")
        return ""

    prompt = (
        "На скриншоте — переписка в мессенджере. Извлеки текст диалога строго "
        "в хронологическом порядке (сверху вниз), различая кто автор реплики.\n"
        "Формат вывода — построчно, без заголовков и пояснений:\n"
        "Собеседник: <текст>\n"
        "Я: <текст>\n"
        f"Если на изображении нет читаемого текста переписки — верни СТРОГО и "
        f"ТОЛЬКО одно слово: {ILLEGIBLE_MARKER}, без кавычек и пояснений.\n"
        "Не добавляй ничего от себя — только то, что реально написано на скриншоте."
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }]
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            resp = await client.post(
                _VISION_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": VISION_MODEL, "messages": messages, "max_tokens": 2000,
                    # qwen3.6 — thinking-модель, иначе добавляет <think>...</think> перед
                    # ответом и ломает точное сравнение с ILLEGIBLE_MARKER.
                    "reasoning_effort": "none",
                },
            )
        if resp.is_success:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            # Защитная зачистка на случай, если reasoning_effort не подавил thinking полностью.
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
        log.warning("Vision %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("Vision: ошибка запроса — %s", e)
    return ""


# ── Groq ──────────────────────────────────────────────────────────────────────

class GroqProvider(LLMProvider):
    name = "Groq"
    _URL   = "https://api.groq.com/openai/v1/chat/completions"
    _MODEL = "llama-3.3-70b-versatile"

    async def ask(self, prompt: str, max_tokens: int) -> str:
        if not GROQ_API_KEY:
            raise ProviderError("GROQ_API_KEY не задан")

        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            resp = await client.post(
                self._URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": self._MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )

        # На ЛЮБОЙ 429 не ждём — сразу уходим на следующего провайдера (Gemini).
        # Раньше на минутный лимит спали 65 с; с появлением fallback это не нужно.
        if resp.status_code == 429:
            raise RateLimitError("Groq: лимит исчерпан (429) — переключаюсь на следующего.")

        if resp.status_code in (500, 502, 503):
            raise ProviderError(f"Groq {resp.status_code}: {resp.text[:200]}")

        if not resp.is_success:
            raise ProviderError(f"Groq {resp.status_code}: {resp.text[:200]}")

        return resp.json()["choices"][0]["message"]["content"].strip()


# ── Google Gemini ─────────────────────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    name = "Gemini"
    _MODEL = "gemini-2.5-flash"

    @property
    def _url(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._MODEL}:generateContent?key={GEMINI_API_KEY}"
        )

    async def ask(self, prompt: str, max_tokens: int) -> str:
        if not GEMINI_API_KEY:
            raise ProviderError("GEMINI_API_KEY не задан")

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                # Отключаем «thinking» — иначе flash тратит бюджет на размышления
                # и возвращает ответ без текста, из-за чего провайдер пропускался зря.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

        # Gemini заблокирован по гео в ряде регионов (РФ) — при заданном GEMINI_PROXY
        # гоним ТОЛЬКО его запросы через прокси, остальные провайдеры идут напрямую.
        client_kwargs = {"timeout": 90.0, "trust_env": False}
        if GEMINI_PROXY:
            client_kwargs["proxy"] = GEMINI_PROXY
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(self._url, json=payload)

        if resp.status_code == 429:
            raise RateLimitError("Лимит Gemini исчерпан.")

        if resp.status_code in (500, 502, 503):
            raise ProviderError(f"Gemini {resp.status_code}: {resp.text[:200]}")

        if not resp.is_success:
            raise ProviderError(f"Gemini {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        # Достаём текст из всех частей (на случай нескольких parts)
        try:
            cand = data["candidates"][0]
            parts = cand.get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            reason = cand.get("finishReason", "?")
            raise ProviderError(f"Gemini: пустой ответ (finishReason={reason})")
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Gemini: неожиданный формат ответа — {e}") from e


# ── OpenRouter ────────────────────────────────────────────────────────────────

class OpenRouterProvider(LLMProvider):
    name = "OpenRouter"
    _URL   = "https://openrouter.ai/api/v1/chat/completions"
    # llama-3.1-8b-instruct:free OpenRouter отключил (модель платная теперь,
    # отдаёт 404 с "unavailable for free") — переехали на 3.3-70b:free.
    _MODEL = "meta-llama/llama-3.3-70b-instruct:free"

    async def ask(self, prompt: str, max_tokens: int) -> str:
        if not OPENROUTER_API_KEY:
            raise ProviderError("OPENROUTER_API_KEY не задан")

        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            resp = await client.post(
                self._URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://github.com/kurganprevedenie-lgtm/CueMe",
                    "X-Title": "CueMe",
                },
                json={
                    "model": self._MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )

        if resp.status_code == 429:
            raise RateLimitError("Лимит OpenRouter исчерпан.")

        if resp.status_code in (500, 502, 503):
            raise ProviderError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")

        if not resp.is_success:
            raise ProviderError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")

        return resp.json()["choices"][0]["message"]["content"].strip()


# ── Каскадный вызов ───────────────────────────────────────────────────────────

_PROVIDERS: list[LLMProvider] = [
    GeminiProvider(),
    GroqProvider(),
    OpenRouterProvider(),
]

# Имя принудительно выбранного провайдера (для отладки через /provider). None = авто.
_forced: str | None = None

PROVIDER_NAMES = [p.name for p in _PROVIDERS]


def set_forced_provider(name: str | None) -> str:
    """Ставит провайдера первым в цепочке. None — вернуть авто-каскад. Возвращает статус."""
    global _forced
    if not name or name.lower() == "auto":
        _forced = None
        return "auto"
    by_lower = {p.name.lower(): p.name for p in _PROVIDERS}
    if name.lower() not in by_lower:
        raise ValueError(
            f"Неизвестный провайдер «{name}». Доступны: {', '.join(PROVIDER_NAMES)}, auto."
        )
    _forced = by_lower[name.lower()]
    return _forced


def get_forced_provider() -> str:
    return _forced or "auto"


def _ordered_providers() -> list[LLMProvider]:
    if not _forced:
        return _PROVIDERS
    forced = [p for p in _PROVIDERS if p.name == _forced]
    rest   = [p for p in _PROVIDERS if p.name != _forced]
    return forced + rest


async def _ask(prompt: str, max_tokens: int = 1024) -> str:
    """Пробует провайдеров по цепочке. Пробрасывает ошибку только если все упали."""
    last_exc: Exception = RuntimeError("Нет доступных LLM-провайдеров")
    chain = _ordered_providers()

    for provider in chain:
        try:
            result = await provider.ask(prompt, max_tokens)
            if _forced or provider is not chain[0]:
                log.info("LLM: ответил %s", provider.name)
            return result
        except RateLimitError as e:
            log.warning("LLM [%s]: лимит исчерпан (%s), переключаемся дальше", provider.name, e)
            last_exc = e
        except (ProviderError, httpx.TimeoutException, httpx.NetworkError) as e:
            log.warning("LLM [%s]: временная ошибка (%s), переключаемся дальше", provider.name, e)
            last_exc = e

    if isinstance(last_exc, RateLimitError):
        raise last_exc
    raise RuntimeError(f"Все LLM-провайдеры недоступны. Последняя ошибка: {last_exc}") from last_exc


# ── Вспомогательные функции ───────────────────────────────────────────────────

def sample_texts(messages: list, n: int = 30) -> list[str]:
    texts = [m.text for m in messages if m.text and m.text.strip()]
    return random.sample(texts, min(n, len(texts)))


_MSG_BUDGET = 12_000


def _fit(msgs: list[str]) -> list[str]:
    """Берёт максимум сообщений, влезающих в символьный бюджет."""
    result, total = [], 0
    for t in msgs:
        total += len(t) + 3
        if total > _MSG_BUDGET:
            break
        result.append(t)
    return result


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


async def build_style_card(my_sample: list[str], user_features_summary: str) -> str:
    """Анализ голоса пользователя. Возвращает plain text."""
    my_sample = _fit(my_sample)
    prompt = (
        "Разбери, как пишет этот человек — его голос. Говоришь с ним самим: "
        "на «ты», живо и по-человечески, как опытный коуч по общению, а не сухой аналитик.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n"
        "Заголовки секций — ровно как ниже (с эмодзи), пункты через •.\n\n"
        f"СТАТИСТИКА:\n{user_features_summary}\n\n"
        f"ВСЕГО СООБЩЕНИЙ В АНАЛИЗЕ: {len(my_sample)}\n\n"
        "СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n"
        + "\n".join(f"- {t}" for t in my_sample)
        + "\n\n"
        "ПРАВИЛА (строго):\n"
        "• Только конкретные факты из сообщений — никаких общих слов\n"
        "• Цитата-пример только если реально показательна для пункта, не ради галочки\n"
        "• Статистика — первичный источник. Если emoji/сообщ < 0.3 → «эмодзи почти не используешь»\n"
        "• Регистр — обязательный пункт: пишешь с большой или маленькой — проверь\n"
        "• Запрещено: «общительный», «тёплый», «использует юмор» — без конкретики\n\n"
        "ДОПОЛНИТЕЛЬНО — пять поведенческих тенденций (по модели Big Five):\n"
        "• Это НЕ клинический тест и НЕ проценты с «уверенностью» — только то, что видно "
        "из текста: как человек пишет, о чём, в каком тоне\n"
        "• Каждая тенденция — 1 фраза-наблюдение + короткая цитата-подтверждение (≤ 15 слов)\n"
        "• Если по какому-то измерению в сообщениях мало опоры — честно напиши "
        "«мало данных для вывода», не выдумывай\n"
        "• Формулируй как тенденцию в письме («в переписке склонен к...»), а не как диагноз "
        "личности («ты — интроверт»)\n\n"
        "ФОРМАТ (секции разделены пустой строкой):\n\n"
        "🎙️ Голос и тон\n"
        "• [факт + цитата если показательна]\n\n"
        "✍️ Как ты строишь сообщения\n"
        "• [типичная длина в словах + пример]\n"
        "• [пунктуация, абзацы]\n\n"
        "🧩 Твой словарь\n"
        "• [характерные слова — цитаты]\n\n"
        "😄 Юмор и эмоции\n"
        "• [как выражаешь, с примером]\n\n"
        "🔤 Регистр и инициатива\n"
        "• [с большой или маленькой — факт]\n"
        "• [кто начинает темы]\n\n"
        "🧭 Пять поведенческих тенденций в переписке\n"
        "• Открытость новому: [тенденция + цитата, или «мало данных»]\n"
        "• Организованность: [склонность к чёткости/планированию в сообщениях + цитата, или «мало данных»]\n"
        "• Общительность: [инициативность, энергия в тексте + цитата, или «мало данных»]\n"
        "• Доброжелательность: [мягкость/прямота, забота о собеседнике + цитата, или «мало данных»]\n"
        "• Эмоциональная устойчивость: [как реагируешь на стресс/раздражение в переписке + цитата, или «мало данных»]"
    )
    return await _ask(prompt, max_tokens=1800)


async def build_interaction_card(
    my_sample: list[str],
    contact_sample: list[str],
    features_summary: str,
) -> str:
    """Наблюдения о собеседнике. Возвращает plain text."""
    my_sample      = _fit(my_sample)
    contact_sample = _fit(contact_sample)
    prompt = (
        "Разбери, как этот собеседник общается с тобой в переписке — его наблюдаемые "
        "привычки. Говоришь с автором: на «ты», уверенно и по делу, как коуч, который "
        "прямо говорит что реально работает, а что нет.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n"
        "Заголовки секций — ровно как ниже (с эмодзи), пункты через •.\n\n"
        f"СТАТИСТИКА:\n{features_summary}\n\n"
        f"ВСЕГО В АНАЛИЗЕ: твои — {len(my_sample)}, собеседника — {len(contact_sample)}\n\n"
        "ТВОИ СООБЩЕНИЯ:\n"
        + "\n".join(f"- {t}" for t in my_sample)
        + "\n\nСООБЩЕНИЯ СОБЕСЕДНИКА:\n"
        + "\n".join(f"- {t}" for t in contact_sample)
        + "\n\n"
        "РАМКА (строго):\n"
        "• Это наблюдаемые ПРИВЫЧКИ ОБЩЕНИЯ и практические советы, как эффективнее ему писать\n"
        "• НЕ психологический портрет — БЕЗ триггеров, слабых мест, «на что давить»\n"
        "• Глубина — в детальности наблюдений за манерой, БЕЗ домыслов о психике\n"
        "• Каждый пункт по делу. Цитата только если реально показательна, не ради галочки\n"
        "• Статистика — первичный источник для эмодзи и длины\n\n"
        "ФОРМАТ (секции через пустую строку; «Как писать» — первой и самой подробной):\n\n"
        "🎯 Как писать этому человеку\n"
        "• [4-5 конкретных применимых советов с опорой на наблюдения ниже: какой заход, "
        "длина, тон, формат у него заходят — каждый совет привязан к привычке]\n\n"
        "🗣️ Речевые паттерны\n"
        "• [характерные слова, обороты, манера — цитаты если показательны]\n\n"
        "📏 Длина и ритм\n"
        "• [типичный объём в словах, темп ответа, паузы]\n\n"
        "🔤 Регистр и язык\n"
        "• [ты/Вы, с большой или маленькой, сленг; эмодзи — цифра из статистики]\n\n"
        "🔥 Что развивает разговор\n"
        "• [на какие заходы охотно отвечает развёрнуто — конкретный пример из переписки]\n\n"
        "🧊 Что гасит разговор\n"
        "• [после чего отвечает сухо или не отвечает — конкретный пример]"
    )
    return await _ask(prompt, max_tokens=2000)


async def build_my_style_for_contact(my_msgs: list[str], stats_summary: str) -> str:
    """Как пользователь пишет конкретному собеседнику. Plain text."""
    my_msgs = _fit(my_msgs)
    prompt = (
        "Разбери, как ты пишешь ЭТОМУ конкретному человеку. Говоришь с автором: "
        "на «ты», уверенно и по делу, как коуч по общению.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n"
        "Заголовки секций — ровно как ниже (с эмодзи), пункты через •.\n\n"
        f"СТАТИСТИКА:\n{stats_summary}\n\n"
        f"ВСЕГО СООБЩЕНИЙ В АНАЛИЗЕ: {len(my_msgs)}\n\n"
        "ТВОИ СООБЩЕНИЯ К ЭТОМУ ЧЕЛОВЕКУ:\n"
        + "\n".join(f"- {t}" for t in my_msgs)
        + "\n\n"
        "ПРАВИЛА:\n"
        "• Анализируй ТОЛЬКО твои сообщения — не собеседника\n"
        "• Конкретные наблюдения; цитата только если показательна\n"
        "• Что специфично именно для этой переписки\n"
        "• Регистр обязательно: с большой или маленькой буквы\n"
        "• Запрещено: общие слова без конкретики\n\n"
        "ФОРМАТ:\n\n"
        "🎯 Тон и дистанция\n"
        "• [наблюдение + цитата если показательна]\n\n"
        "📏 Длина и структура\n"
        "• [типичная длина в словах + пример]\n\n"
        "🔤 Формальность и регистр\n"
        "• [ты/Вы, с большой или маленькой]\n\n"
        "🚪 Заходы и переходы\n"
        "• [как начинаешь, как заканчиваешь — цитаты]\n\n"
        "✨ Что характерно именно для этой переписки\n"
        "• [что может отличаться от других твоих чатов]"
    )
    return await _ask(prompt, max_tokens=1500)


async def build_overall_style(per_contact_cards: list[dict]) -> str:
    """Агрегат из per-contact карточек — общий портрет с паттернами адаптации. Plain text."""
    cards_text = ""
    for item in per_contact_cards:
        cards_text += f"=== {item['display_name']} ===\n{item['card_text']}\n\n"

    prompt = (
        "Ниже — как ты пишешь разным собеседникам. "
        "Говоришь с ним самим: на «ты», живо, как опытный коуч по общению.\n"
        "Найди паттерны. Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n"
        "Заголовки секций — ровно как ниже (с эмодзи), пункты через •.\n\n"
        f"СТИЛИ ПО СОБЕСЕДНИКАМ:\n{cards_text}"
        "ПРАВИЛА:\n"
        "• Ищи что МЕНЯЕТСЯ и что ОСТАЁТСЯ постоянным\n"
        "• Конкретные наблюдения — ссылайся на примеры из карточек\n"
        "• Называй собеседников ПО ИМЕНИ как в заголовках (=== Имя ===), не «Собеседник 1»\n"
        "• НЕ пересказывай каждую карточку — анализируй паттерны\n"
        "• Запрещено: «адаптируется к собеседнику» без конкретики\n\n"
        "ФОРМАТ:\n\n"
        "🧱 Что в тебе постоянно\n"
        "• [общее для всех переписок + пример]\n\n"
        "🎚️ Как меняется твой стиль\n"
        "• [с кем формально vs неформально — по именам]\n"
        "• [с кем длинно vs коротко — по именам]\n\n"
        "🔀 Как ты подстраиваешься\n"
        "• [как меняешься под разных людей — конкретно, по именам]\n\n"
        "💪 Твои сильные стороны\n"
        "• [что стабильно работает хорошо]"
    )
    return await _ask(prompt, max_tokens=2000)


async def rewrite_message(draft: str, style_card: str, interaction_card: str) -> str:
    """Переписывает черновик в голосе пользователя под конкретного собеседника."""
    prompt = (
        "Перепиши черновик автора под конкретного собеседника, сохранив голос автора.\n\n"
        f"ГОЛОС АВТОРА (style_card):\n{style_card}\n\n"
        f"СОБЕСЕДНИК — его привычки и что у него заходит (interaction_card):\n{interaction_card}\n\n"
        f"ЧЕРНОВИК АВТОРА:\n{draft}\n\n"
        "=== СОХРАНЯЕМ (жёстко) ===\n"
        "• Смысл и все содержательные куски — ничего важного не выкидывай\n"
        "• Длину — ПРИМЕРНО как в черновике. Не сокращай резко, не делай телеграфным\n"
        "• Голос автора: его слова, регистр (маленькая/большая), эмодзи (нет в черновике — не добавляй)\n"
        "=== АДАПТИРУЕМ под собеседника (из interaction_card) ===\n"
        "• Тон и теплоту, заход/первую фразу, формальность (ты/Вы), ритм и формат\n\n"
        "=== ПЛАНКА: адаптация ≠ корректура и ≠ сочинить заново ===\n"
        "(пример ТОЛЬКО для калибровки — НЕ копируй из него слова и тему)\n"
        "Условный черновик: «Здравствуйте, вы успеете сделать макет к четвергу?»\n"
        "✗ корректура (почти без изменений) — ПЛОХО\n"
        "✗ «макет к чт?» (потерян объём и голос) — ПЛОХО\n"
        "✓ «слушай, макет к четвергу реально успеть?» (смысл и объём те же, заход под него) — ХОРОШО\n\n"
        "Работай с РЕАЛЬНЫМ ЧЕРНОВИКОМ АВТОРА выше — его тема и смысл, пример не переноси.\n"
        "Верни только итоговое переписанное сообщение — без кавычек, без пояснений."
    )
    return await _ask(prompt)


_DELIM  = "===ПОЯСНЕНИЕ==="
_RATING = "===ОЦЕНКА==="


def _split_explained(raw: str) -> tuple[str, str]:
    if _DELIM in raw:
        msg, expl = raw.split(_DELIM, 1)
        return msg.strip(), expl.strip()
    return raw.strip(), ""


def _split_rated(raw: str) -> tuple[str, str, str]:
    """Разбирает: сообщение / пояснение / оценку. Любой маркер может отсутствовать."""
    work, rating = raw, ""
    if _RATING in work:
        work, rating = work.split(_RATING, 1)
        rating = rating.strip()
    msg, expl = _split_explained(work)
    return msg, expl, rating


async def rewrite_message_explained(
    draft: str, style_card: str, interaction_card: str
) -> tuple[str, str, str]:
    """Переписывает черновик + пояснение + оценку. Один вызов LLM.
    Возвращает (сообщение, пояснение, оценка)."""
    prompt = (
        "Ты — уверенный коуч по общению и свиданиям, говоришь прямо и по делу, без "
        "занудства. Помоги автору переписать черновик так, чтобы он звучал живее, "
        "увереннее и лучше цеплял конкретного собеседника — но это по-прежнему сам "
        "автор, его слова, не кто-то другой.\n\n"
        f"ГОЛОС АВТОРА:\n{style_card}\n\n"
        f"ПРИВЫЧКИ СОБЕСЕДНИКА (как он обычно пишет и что у него заходит):\n{interaction_card}\n\n"
        f"ЧЕРНОВИК АВТОРА:\n{draft}\n\n"
        "=== СОХРАНЯЕМ (жёстко) ===\n"
        "• Смысл и все содержательные куски — ничего важного не выкидывай\n"
        "• Длину — ПРИМЕРНО как в черновике. Не сокращай резко, не делай телеграфным\n"
        "• Голос автора: его слова, регистр, эмодзи (нет в черновике — не добавляй)\n"
        "=== АДАПТИРУЕМ под привычки собеседника ===\n"
        "• Тон и теплоту, заход/первую фразу, формальность (ты/Вы), ритм и формат\n\n"
        "=== ПЛАНКА: адаптация ≠ корректура и ≠ сочинить заново ===\n"
        "(пример ТОЛЬКО для калибровки глубины — НЕ копируй из него слова и тему)\n"
        "Условный черновик: «Здравствуйте, вы успеете сделать макет к четвергу?»\n"
        "✗ корректура (почти без изменений) — ПЛОХО\n"
        "✗ «макет к чт?» (потерян объём и голос) — ПЛОХО\n"
        "✓ «слушай, макет к четвергу реально успеть?» (смысл и объём те же, заход и регистр под него) — ХОРОШО\n\n"
        "=== ЯЗЫК ПОЯСНЕНИЯ И ОЦЕНКИ (строго) ===\n"
        "• Пиши ТОЛЬКО по-русски, простыми словами. Никаких английских слов.\n"
        "• НЕ упоминай технические названия и внутреннюю кухню: «interaction_card», "
        "«style_card», «раздел», названия секций анализа. Говори по-человечески: "
        "«он сам пишет на ты», «он не любит длинные сообщения».\n\n"
        "=== ВЫВОД (строго по формату) ===\n"
        "Работай с РЕАЛЬНЫМ ЧЕРНОВИКОМ АВТОРА выше — его смысл, его тема, его слова. "
        "Пример из планки не переноси.\n"
        "Сначала — ТОЛЬКО переписанное сообщение: в голосе автора, без кавычек, без коучинга и морали.\n"
        f"Затем строка: {_DELIM}\n"
        "Затем — на «ты», уверенно и по делу, как коуч, который знает что реально работает: "
        "ЧТО изменил и ПОЧЕМУ именно под этого собеседника, простым языком, со ссылкой на его привычку. "
        "Пример: «заход сделал теплее и убрал \"Вы\" — он сам пишет на ты и коротко, "
        "а длинные формальные сообщения у него повисают без ответа».\n"
        f"Затем строка: {_RATING}\n"
        "Затем — короткая честная оценка, как впишется под этого собеседника. "
        "БЕЗ процентов и цифр, по-русски. Начни со значка ✅ или ⚠️. "
        "Примеры: «✅ В его тоне, коротко — должно зайти» / "
        "«⚠️ Длинновато для него, такое часто не дочитывает — можно сжать»."
    )
    return _split_rated(await _ask(prompt))


async def suggest_reply(
    incoming_msg: str, style_card: str, interaction_card: str
) -> tuple[str, str, str]:
    """Предлагает как ответить на сообщение собеседника — в голосе автора.
    Возвращает (ответ, пояснение, оценка)."""
    prompt = (
        "Ты — уверенный коуч по общению и свиданиям. Собеседник прислал автору сообщение. "
        "Предложи КАК ответить так, чтобы звучать живо и уверенно — в голосе автора, "
        "с учётом привычек собеседника.\n\n"
        f"ГОЛОС АВТОРА:\n{style_card}\n\n"
        f"ПРИВЫЧКИ СОБЕСЕДНИКА (как он обычно пишет):\n{interaction_card}\n\n"
        f"СООБЩЕНИЕ СОБЕСЕДНИКА:\n{incoming_msg}\n\n"
        "ПРАВИЛА:\n"
        "• Ответ в стиле автора: его слова, регистр, длина под собеседника\n"
        "• Не выдумывай факты, которых автор знать не может\n"
        "• Если по сообщению нужна конкретика которой нет — предложи короткий уточняющий ответ\n"
        "• Это черновик ответа от лица автора, а не совет со стороны\n\n"
        "=== ЯЗЫК ПОЯСНЕНИЯ И ОЦЕНКИ (строго) ===\n"
        "• Пиши ТОЛЬКО по-русски, простыми словами. Никаких английских слов.\n"
        "• НЕ упоминай технические названия («interaction_card», «style_card»), названия "
        "секций анализа и внутреннюю кухню. Говори по-человечески.\n\n"
        "=== ВЫВОД (строго по формату) ===\n"
        "Сначала — ТОЛЬКО текст ответа: в голосе автора, без кавычек, без коучинга.\n"
        f"Затем строка: {_DELIM}\n"
        "Затем — на «ты», по-человечески: почему такой ответ и как он играет на привычку "
        "собеседника, простым языком.\n"
        f"Затем строка: {_RATING}\n"
        "Затем — короткая честная оценка, как зайдёт. БЕЗ процентов, по-русски. "
        "Начни со значка ✅ или ⚠️."
    )
    return _split_rated(await _ask(prompt))


async def suggest_reply_from_screenshot(
    chat_text: str,
    style: str,
    style_card: str,
    interaction_card: str,
    style_description: str,
) -> tuple[str, str, str]:
    """Ответ на распознанную переписку в голосе автора, в заданном стиле.
    Возвращает (ответ, пояснение, оценка)."""
    interaction_block = interaction_card or "нет данных о собеседнике — ориентируйся только на текст переписки"
    prompt = (
        "Ты — уверенный коуч по общению и свиданиям. Ниже — переписка (возможно "
        "распознанная со скриншота, могут быть мелкие ошибки OCR). Помоги автору "
        "ответить так, чтобы звучать живо и уверенно.\n\n"
        f"ГОЛОС АВТОРА:\n{style_card}\n\n"
        f"ПРИВЫЧКИ СОБЕСЕДНИКА:\n{interaction_block}\n\n"
        f"ПЕРЕПИСКА:\n{chat_text}\n\n"
        f"ТРЕБУЕМЫЙ СТИЛЬ ОТВЕТА: {style_description}\n"
        "Стиль влияет на ТОН и подачу, но НЕ отменяет голос автора — это "
        "по-прежнему его слова и манера, просто в этой подаче.\n\n"
        "ПРАВИЛА:\n"
        "• Ответ в стиле автора: его слова, регистр, длина под собеседника\n"
        "• Не выдумывай факты, которых автор знать не может\n"
        "• Если распознанный текст обрывочный — ориентируйся на последнюю реплику собеседника\n\n"
        "=== ЯЗЫК ПОЯСНЕНИЯ И ОЦЕНКИ (строго) ===\n"
        "• Пиши ТОЛЬКО по-русски, простыми словами. Никаких английских слов.\n"
        "• НЕ упоминай технические названия («interaction_card», «style_card»), "
        "названия секций анализа и внутреннюю кухню. Говори по-человечески.\n\n"
        "=== ВЫВОД (строго по формату) ===\n"
        "Сначала — ТОЛЬКО текст ответа: в голосе автора, без кавычек, без коучинга.\n"
        f"Затем строка: {_DELIM}\n"
        "Затем — на «ты», по-человечески: почему такой ответ и как он играет "
        "на привычку собеседника, простым языком.\n"
        f"Затем строка: {_RATING}\n"
        "Затем — короткая честная оценка, как зайдёт. БЕЗ процентов, по-русски. "
        "Начни со значка ✅ или ⚠️."
    )
    return _split_rated(await _ask(prompt))


ADJUSTMENTS = {
    "short":  "Сделай ЗАМЕТНО короче — выкинь лишнее, оставь суть.",
    "warm":   "Сделай теплее и мягче по тону, дружелюбнее.",
    "formal": "Сделай формальнее и вежливее, более деловой регистр.",
}


async def adjust_message(
    current_text: str, style_card: str, adjustment: str
) -> tuple[str, str]:
    """Подстраивает уже готовое сообщение под нужный регистр, сохраняя голос автора."""
    instruction = ADJUSTMENTS.get(adjustment, adjustment)
    prompt = (
        "Подправь готовое сообщение по инструкции, сохранив голос автора и смысл.\n\n"
        f"ГОЛОС АВТОРА:\n{style_card}\n\n"
        f"ТЕКУЩЕЕ СООБЩЕНИЕ:\n{current_text}\n\n"
        f"ИНСТРУКЦИЯ: {instruction}\n\n"
        "ПРАВИЛА:\n"
        "• Сохрани смысл и личный словарь автора\n"
        "• Регистр (большая/маленькая буква) и эмодзи — как в исходном, если инструкция не про них\n\n"
        "Сначала выведи ТОЛЬКО изменённое сообщение: в голосе автора, без кавычек, без коучинга.\n"
        f"Затем на отдельной строке: {_DELIM}\n"
        "Затем — 1 короткую фразу что изменилось: по-русски, простыми словами, без "
        "английских слов и технических терминов."
    )
    return _split_explained(await _ask(prompt))


def _split_by_markers(raw: str, markers: list[str]) -> list[str]:
    """Делит один текстовый ответ LLM на len(markers)+1 блоков по маркерам-разделителям."""
    pattern = "|".join(re.escape(m) for m in markers)
    parts = [p.strip() for p in re.split(pattern, raw)]
    while len(parts) < len(markers) + 1:
        parts.append("")
    return parts


_DA_HIST  = "===ИСТОРИЯ==="
_DA_SWOT  = "===СИЛЬНЫЕ_СЛАБЫЕ==="
_DA_GIFTS = "===ПОДАРКИ==="


def _split_deep_analysis(raw: str) -> tuple[str, str, str, str]:
    parts = _split_by_markers(raw, [_DA_HIST, _DA_SWOT, _DA_GIFTS])
    return parts[0], parts[1], parts[2], parts[3]


async def build_deep_analysis(dated_lines: list[str], stats_summary: str) -> tuple[str, str, str, str]:
    """Глубокий анализ пары: совместимость, история по периодам, сильные/слабые
    стороны + точки роста, рекомендации подарков. Один вызов LLM, четыре блока
    разделены маркерами. Возвращает (совместимость, история, swot, подарки)."""
    dated_lines = _fit(dated_lines)
    prompt = (
        "Ты — уверенный дейтинг-коуч, разбираешь переписку автора с его собеседником "
        "в романтическом/дейтинг контексте. Говоришь с автором напрямую: на «ты», прямо "
        "и по делу, без занудства и без клинических диагнозов — только то, что реально "
        "видно из переписки.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n\n"
        f"СТАТИСТИКА:\n{stats_summary}\n\n"
        "ПЕРЕПИСКА (хронологически, каждая строка — дата и автор):\n"
        + "\n".join(dated_lines)
        + "\n\n"
        "Собери ЧЕТЫРЕ блока строго в этом порядке, разделённые маркерами.\n\n"
        "БЛОК 1 — Совместимость (без маркера, первым):\n"
        "• Первая строка ровно: Совместимость: XX/100 (число от 0 до 100 — твоя честная "
        "оценка по видимой динамике: инициатива с обеих сторон, тон, взаимность, вовлечённость)\n"
        "• Затем 3-5 пунктов через • — что именно формирует эту оценку, с опорой на "
        "конкретику из переписки (кто чаще пишет первым, как быстро идёт сближение, "
        "симметрична ли теплота)\n\n"
        f"Затем строка: {_DA_HIST}\n"
        "БЛОК 2 — История отношений по периодам:\n"
        "• Раздели весь период переписки на 3-5 естественных отрезков по датам "
        "(например «Начало — дата–дата», «Середина — ...», «Сейчас — ...»)\n"
        "• На каждый отрезок: диапазон дат, короткая оценка тона (теплеет/охлаждается/"
        "ровно/суше) и 1 конкретный пример, который это показывает\n"
        "• Если данных мало для деления на периоды — честно скажи это одной строкой\n\n"
        f"Затем строка: {_DA_SWOT}\n"
        "БЛОК 3 — Сильные стороны / возможные проблемы / точки роста:\n"
        "💪 Сильные стороны\n"
        "• [2-3 пункта — что уже хорошо работает в этом общении, с опорой на переписку]\n\n"
        "⚠️ Возможные проблемы\n"
        "• [1-3 пункта — что может мешать сближению или создаёт трение, конкретно]\n\n"
        "🌱 Возможности для роста\n"
        "• [2-3 практических пункта — что можно попробовать, чтобы усилить связь]\n\n"
        f"Затем строка: {_DA_GIFTS}\n"
        "БЛОК 4 — Рекомендации подарков:\n"
        "• Найди в переписке реальные интересы, увлечения, упомянутые желания собеседника\n"
        "• Предложи 3-5 конкретных идей подарков, каждая — с привязкой к тому, что именно "
        "в переписке на это указывает (цитата или короткий пересказ)\n"
        "• Если зацепок в переписке недостаточно — честно скажи это и предложи 1-2 "
        "универсальных безопасных варианта"
    )
    raw = await _ask(prompt, max_tokens=2500)
    return _split_deep_analysis(raw)


_DSA_HIST = "===СТИЛЬ_ИСТОРИЯ==="
_DSA_SWOT = "===СТИЛЬ_СИЛЬНЫЕ_СЛАБЫЕ==="
_DSA_TIPS = "===СТИЛЬ_СОВЕТЫ==="


def _split_deep_style_analysis(raw: str) -> tuple[str, str, str, str]:
    parts = _split_by_markers(raw, [_DSA_HIST, _DSA_SWOT, _DSA_TIPS])
    return parts[0], parts[1], parts[2], parts[3]


async def build_deep_style_analysis(dated_lines: list[str], stats_summary: str) -> tuple[str, str, str, str]:
    """Глубокий анализ ТОЛЬКО своего стиля (агрегат по всем собеседникам):
    коммуникативный профиль, как менялся стиль по периодам, сильные/слабые
    стороны + точки роста, практические советы для дейтинга. Один вызов LLM,
    четыре блока разделены маркерами. Возвращает (профиль, история, swot, советы)."""
    dated_lines = _fit(dated_lines)
    prompt = (
        "Ты — уверенный дейтинг-коуч, разбираешь КАК этот человек пишет — все его "
        "исходящие сообщения разным собеседникам вместе, хронологически (без привязки "
        "к конкретному человеку). Говоришь с ним самим: на «ты», прямо и по делу, "
        "без занудства и без клинических диагнозов — только то, что реально видно "
        "из текста.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n\n"
        f"СТАТИСТИКА:\n{stats_summary}\n\n"
        "СООБЩЕНИЯ (хронологически, дата + текст):\n"
        + "\n".join(dated_lines)
        + "\n\n"
        "Собери ЧЕТЫРЕ блока строго в этом порядке, разделённые маркерами.\n\n"
        "БЛОК 1 — Коммуникативный профиль (без маркера, первым, БЕЗ числовой оценки):\n"
        "🎙️ Голос и манера\n"
        "• [3-5 конкретных наблюдений: тон, энергия, характерные обороты, регистр — "
        "каждое с цитатой-примером]\n\n"
        f"Затем строка: {_DSA_HIST}\n"
        "БЛОК 2 — Как менялся стиль по периодам:\n"
        "• Раздели весь период на 3-5 естественных отрезков по датам "
        "(например «Начало — дата–дата», «Середина — ...», «Сейчас — ...»)\n"
        "• На каждый отрезок: диапазон дат, что изменилось в манере письма "
        "(тон, длина, эмодзи, инициатива, уверенность) + конкретный пример\n"
        "• Если данных мало для деления на периоды — честно скажи это одной строкой\n\n"
        f"Затем строка: {_DSA_SWOT}\n"
        "БЛОК 3 — Сильные стороны / возможные проблемы / точки роста как собеседника:\n"
        "💪 Сильные стороны\n"
        "• [2-3 пункта — что уже хорошо работает в переписке, с опорой на текст]\n\n"
        "⚠️ Возможные проблемы\n"
        "• [1-3 пункта — что может мешать нравиться или создавать дистанцию, конкретно]\n\n"
        "🌱 Возможности для роста\n"
        "• [2-3 практических пункта — что можно прокачать в манере письма]\n\n"
        f"Затем строка: {_DSA_TIPS}\n"
        "БЛОК 4 — Практические рекомендации для дейтинга:\n"
        "• 3-5 конкретных советов, привязанных к наблюдениям выше — что попробовать "
        "в переписках, чтобы звучать увереннее и вызывать больше интереса\n"
        "• Без общих слов — каждый совет должен опираться на то, что реально видно в тексте"
    )
    raw = await _ask(prompt, max_tokens=2500)
    return _split_deep_style_analysis(raw)


async def compare_my_styles(per_contact_cards: list[dict]) -> str:
    """Сравнивает как пользователь пишет разным собеседникам. Plain text."""
    blocks = ""
    for item in per_contact_cards:
        blocks += f"=== {item['display_name']} ===\n{item['card_text']}\n\n"

    prompt = (
        "Ниже — как ты пишешь разным собеседникам. Покажи контраст между ними.\n"
        "Говоришь с автором: на «ты», живо, как опытный коуч по общению.\n"
        "Верни ТОЛЬКО текст — без JSON, без кавычек, без markdown.\n"
        "Заголовки секций — ровно как ниже (с эмодзи).\n\n"
        f"СТИЛИ ПО СОБЕСЕДНИКАМ:\n{blocks}"
        "ПРАВИЛА:\n"
        "• По каждому собеседнику — 1-2 строки: чем именно отличается стиль с ним\n"
        "• Опирайся на конкретику из карточек (длина, тон, формальность, эмодзи)\n"
        "• В конце — короткий вывод: где ты теплее/суше, длиннее/короче, формальнее\n"
        "• Имена собеседников бери как в заголовках\n\n"
        "ФОРМАТ:\n\n"
        "👥 Как ты пишешь каждому\n"
        "• [Имя]: [чем выделяется]\n\n"
        "🧭 Общий вывод\n"
        "• [с кем как — контраст в одну-две фразы]"
    )
    return await _ask(prompt, max_tokens=1500)
