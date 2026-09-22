"""Разовая диагностика: реальные id моделей Gemini/Gemma, доступные текущему
ключу, через GET /v1beta/models — источник правды вместо угадывания имён.
Ключ читается из config.py (.env), нигде не печатается.

Запуск: py -3.13 -m tools.list_gemini_models
"""
import asyncio

import httpx

from config import GEMINI_API_KEYS, GEMINI_PROXY


async def main() -> None:
    keys = GEMINI_API_KEYS
    if not keys:
        print("GEMINI_API_KEY(S) не задан в .env")
        return

    key = keys[0]
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    kwargs = {"timeout": 30.0, "trust_env": False}
    if GEMINI_PROXY:
        kwargs["proxy"] = GEMINI_PROXY

    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(url)

    if not resp.is_success:
        print(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return

    data = resp.json()
    models = data.get("models", [])
    print(f"Всего моделей: {len(models)}\n")
    for m in models:
        name = m.get("name", "")  # "models/gemini-flash-latest"
        display = m.get("displayName", "")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            print(f"{name}\n  displayName: {display}\n")


if __name__ == "__main__":
    asyncio.run(main())
