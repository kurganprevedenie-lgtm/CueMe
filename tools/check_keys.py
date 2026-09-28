"""Диагностика живости всех API-ключей (Gemini, Groq, OpenRouter) без
ротации — каждый ключ бьётся отдельным запросом, чтобы увидеть его реальный
статус. Gemini/Groq поддерживают несколько ключей (мультиаккаунтинг, см.
config.py) — проверяется каждый ключ из списка по отдельности, не только
первый.

2026-09-28: Cerebras/Mistral/Cloudflare Workers AI/GitHub Models/NVIDIA
NIM/Intern AI убраны из диагностики вместе с их провайдерами в llm.py (см.
докстринг там) — первые три оказались гарантированно мёртвыми (402/401/
ConnectTimeout), остальные три никогда не были настроены.

Название модели для каждого провайдера читается ИЗ llm.py (Provider._MODEL),
а не дублируется здесь строкой — раньше было дублирование, и правка модели в
llm.py (например миграция на новую модель после того, как старую сняли с
обслуживания) молча не долетала до этого скрипта: он продолжал проверять
старую, уже мёртвую модель, и диагностика врала. Актуально только для
провайдеров, у которых MODEL — не переменная запроса, а фиксированный
атрибут класса (OpenRouter) — Gemini/Groq модель тоже читают из своих
провайдеров ниже, для единообразия.

Запуск на сервере: python3.13 -m tools.check_keys (или ./venv/bin/python -m
tools.check_keys, если зависимости стоят в venv, см. cueme-bot.service)
"""
import asyncio

import httpx

from config import (
    GEMINI_API_KEYS,
    GEMINI_PROXY,
    GROQ_API_KEYS,
    OPENROUTER_API_KEY,
)
from llm import (
    GeminiProvider,
    GroqProvider,
    OpenRouterProvider,
)


def _mask(key: str) -> str:
    return f"...{key[-4:]}" if len(key) > 4 else "***"


async def check_gemini_model(key: str, model: str) -> tuple[bool, str]:
    """Один запрос к ОДНОЙ модели каскада (GeminiProvider._MODEL_CASCADE) —
    id моделей и payload (thinkingConfig — не все модели его поддерживают,
    см. GeminiProvider._NO_THINKING_CONFIG) читаются прямо из llm.py, не
    дублируются здесь строкой, чтобы правка каскада не расходилась с
    диагностикой молча."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    generation_config = {"maxOutputTokens": 100}
    if model not in GeminiProvider._NO_THINKING_CONFIG:
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: тест пройден?"}]}],
        "generationConfig": generation_config,
    }
    kwargs = {"timeout": 30.0, "trust_env": False}
    if GEMINI_PROXY:
        kwargs["proxy"] = GEMINI_PROXY
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.post(url, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return True, text
    except (KeyError, IndexError):
        return False, f"неожиданный ответ: {resp.text[:150]}"


async def check_groq(key: str) -> tuple[bool, str]:
    url = GroqProvider._URL
    payload = {
        "model": GroqProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 200,  # gpt-oss тратит часть max_tokens на reasoning до финального content
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        return False, "content пустой/null — вероятно, reasoning съел весь бюджет max_tokens"
    return True, text


async def check_openrouter(key: str) -> tuple[bool, str]:
    # 2026-09-28: живая диагностика падала AttributeError: 'NoneType' object
    # has no attribute 'strip' — openrouter/free это self-updating роутер по
    # ~24 бесплатным моделям (см. OpenRouterProvider выше), какая из них
    # ответит на конкретный запрос — не гарантировано, и часть из них
    # reasoning-модели (тот же класс, что GroqProvider._REASONING_BUFFER):
    # тратят часть max_tokens на размышления ДО финального content, на
    # малом бюджете content приходит null. Тут его не хватало (200, без
    # запаса) — добавили тот же _REASONING_BUFFER, что и в проде
    # (OpenRouterProvider.ask()), и .get(...) or "" вместо прямого
    # индексирования — на случай, если content всё равно придёт null.
    url = OpenRouterProvider._URL
    payload = {
        "model": OpenRouterProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 200 + OpenRouterProvider._REASONING_BUFFER,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/kurganprevedenie-lgtm/CueMe",
                "X-Title": "CueMe",
            },
            json=payload,
        )
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        return False, "content пустой/null — вероятно, reasoning-модель роутера съела весь бюджет"
    return True, text


async def run_gemini_check() -> None:
    """Отдельно от _run_group ниже — Gemini теперь каскад МОДЕЛЕЙ на каждый
    ключ (см. GeminiProvider._MODEL_CASCADE в llm.py), простого OK/FAIL на
    ключ уже недостаточно: важно видеть, какая именно модель прошла первой
    (обычно Gemma — самый большой лимит) и где стоит следующая по приоритету
    модель, если основная упёрлась в лимит/перегрузку."""
    keys = GEMINI_API_KEYS
    print(f"\n=== Gemini: всего ключей {len(keys)}, моделей в каскаде {len(GeminiProvider._MODEL_CASCADE)} ===")
    if not keys:
        print("  (не задано)")
        return
    for i, key in enumerate(keys):
        print(f"  ключ #{i} ({_mask(key)}):")
        for model in GeminiProvider._MODEL_CASCADE:
            try:
                ok, detail = await check_gemini_model(key, model)
            except Exception as e:
                ok, detail = False, f"исключение: {type(e).__name__}: {e}"
            status = "OK" if ok else "FAIL"
            print(f"    {model}: {status} -> {detail!r}")


async def _run_group(title: str, keys: list[str], checker) -> None:
    print(f"\n=== {title}: всего ключей {len(keys)} ===")
    if not keys:
        print("  (не задано)")
        return
    for i, key in enumerate(keys):
        try:
            ok, detail = await checker(key)
        except Exception as e:
            ok, detail = False, f"исключение: {type(e).__name__}: {e}"
        status = "OK" if ok else "FAIL"
        print(f"  ключ #{i} ({_mask(key)}): {status} -> {detail!r}")


async def main() -> None:
    await _run_group("Groq", GROQ_API_KEYS, check_groq)
    await run_gemini_check()
    await _run_group("OpenRouter", [OPENROUTER_API_KEY] if OPENROUTER_API_KEY else [], check_openrouter)


if __name__ == "__main__":
    asyncio.run(main())
