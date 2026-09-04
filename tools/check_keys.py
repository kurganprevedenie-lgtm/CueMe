"""Диагностика живости всех API-ключей (Gemini, Groq, Cloudflare, Cerebras,
Mistral, GitHub Models, OpenRouter) без ротации — каждый ключ бьётся отдельным
запросом, чтобы увидеть его реальный статус.

Название модели для каждого провайдера читается ИЗ llm.py (Provider._MODEL),
а не дублируется здесь строкой — раньше было дублирование, и правка модели в
llm.py (например миграция на новую модель после того, как старую сняли с
обслуживания) молча не долетала до этого скрипта: он продолжал проверять
старую, уже мёртвую модель, и диагностика врала. Актуально только для
провайдеров, у которых MODEL — не переменная запроса, а фиксированный
атрибут класса (Cloudflare/Cerebras/Mistral/GitHub Models/OpenRouter) —
Gemini/Groq модель тоже читают из своих провайдеров ниже, для единообразия.

Запуск на сервере: python3.13 -m tools.check_keys (или ./venv/bin/python -m
tools.check_keys, если зависимости стоят в venv, см. cueme-bot.service)
"""
import asyncio

import httpx

from config import (
    CEREBRAS_API_KEY,
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    GEMINI_API_KEYS,
    GEMINI_PROXY,
    GITHUB_MODELS_TOKEN,
    GROQ_API_KEYS,
    MISTRAL_API_KEY,
    OPENROUTER_API_KEY,
)
from llm import (
    CerebrasProvider,
    CloudflareProvider,
    GeminiProvider,
    GitHubModelsProvider,
    GroqProvider,
    MistralProvider,
    OpenRouterProvider,
)


def _mask(key: str) -> str:
    return f"...{key[-4:]}" if len(key) > 4 else "***"


async def check_gemini(key: str) -> tuple[bool, str]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GeminiProvider._MODEL}:generateContent?key={key}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: тест пройден?"}]}],
        # thinkingBudget=0 не всегда полностью гасит "мысли" модели (иногда всё
        # равно тратит часть бюджета до текста) — берём запас, тот же принцип,
        # что у reasoning-моделей Groq/OpenRouter ниже.
        "generationConfig": {"maxOutputTokens": 100, "thinkingConfig": {"thinkingBudget": 0}},
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
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_cerebras(key: str) -> tuple[bool, str]:
    url = CerebrasProvider._URL
    payload = {
        "model": CerebrasProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 200,  # gpt-oss тратит часть max_tokens на reasoning до финального content
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_mistral(key: str) -> tuple[bool, str]:
    url = MistralProvider._URL
    payload = {
        "model": MistralProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 20,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_cloudflare(account_id: str, token: str) -> tuple[bool, str]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    payload = {
        "model": CloudflareProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 20,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_github_models(token: str) -> tuple[bool, str]:
    url = GitHubModelsProvider._URL
    payload = {
        "model": GitHubModelsProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 20,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json=payload,
        )
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_openrouter(key: str) -> tuple[bool, str]:
    url = OpenRouterProvider._URL
    payload = {
        "model": OpenRouterProvider._MODEL,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 200,  # тоже reasoning-модель, см. комментарий в check_groq
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
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def _run_group(title: str, keys: list[str], checker) -> None:
    print(f"\n=== {title}: всего ключей {len(keys)} ===")
    if not keys:
        print("  (не задано)")
        return
    for i, key in enumerate(keys):
        try:
            ok, detail = await checker(key)
        except Exception as e:
            ok, detail = False, f"исключение: {e}"
        status = "OK" if ok else "FAIL"
        print(f"  ключ #{i} ({_mask(key)}): {status} -> {detail!r}")


async def main() -> None:
    await _run_group("Gemini", GEMINI_API_KEYS, check_gemini)
    await _run_group("Groq", GROQ_API_KEYS, check_groq)

    print("\n=== Cloudflare Workers AI ===")
    if not (CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN):
        print("  (не задано — нужны оба CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN)")
    else:
        try:
            ok, detail = await check_cloudflare(CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)
        except Exception as e:
            ok, detail = False, f"исключение: {e}"
        print(f"  {'OK' if ok else 'FAIL'} -> {detail!r}")

    await _run_group("Cerebras", [CEREBRAS_API_KEY] if CEREBRAS_API_KEY else [], check_cerebras)
    await _run_group("Mistral", [MISTRAL_API_KEY] if MISTRAL_API_KEY else [], check_mistral)
    await _run_group(
        "GitHub Models", [GITHUB_MODELS_TOKEN] if GITHUB_MODELS_TOKEN else [], check_github_models,
    )
    await _run_group("OpenRouter", [OPENROUTER_API_KEY] if OPENROUTER_API_KEY else [], check_openrouter)


if __name__ == "__main__":
    asyncio.run(main())
