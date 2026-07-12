"""Диагностика живости всех API-ключей (Gemini, Groq, OpenRouter) без ротации —
каждый ключ бьётся отдельным запросом, чтобы увидеть его реальный статус.

Запуск на сервере: py -3.13 tools/check_keys.py
"""
import asyncio

import httpx

from config import GEMINI_API_KEYS, GEMINI_PROXY, GROQ_API_KEYS, OPENROUTER_API_KEY


def _mask(key: str) -> str:
    return f"...{key[-4:]}" if len(key) > 4 else "***"


async def check_gemini(key: str) -> tuple[bool, str]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={key}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: тест пройден?"}]}],
        "generationConfig": {"maxOutputTokens": 20, "thinkingConfig": {"thinkingBudget": 0}},
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
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 20,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return True, text


async def check_openrouter(key: str) -> tuple[bool, str]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": 20,
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
    await _run_group("OpenRouter", [OPENROUTER_API_KEY] if OPENROUTER_API_KEY else [], check_openrouter)


if __name__ == "__main__":
    asyncio.run(main())
