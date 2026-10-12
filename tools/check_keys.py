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
import re

import httpx

import config
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


# Было: check_groq выше — крошечный запрос (max_tokens=200) к одной модели.
# Он проходил даже на ключе, у которого на сегодня почти кончился лимит
# токенов, а настоящий запрос бота (промпт на тысячи токенов + резерв под
# ответ) на том же ключе получал 429 — ключ выглядел «живым», хотя бот его
# использовать не мог. Теперь (check_groq_for_bot): тот же короткий вопрос,
# но с ТАКИМ ЖЕ резервом под ответ, как у бота — Groq сверяет с лимитом весь
# заявленный объём, поэтому ключ без запаса честно отвечает 429, а реально
# тратится лишь десяток токенов. Плюс остатки из заголовков ответа и
# расшифровка 429 (какой лимит, сколько израсходовано, когда сброс).
# Старая check_groq оставлена для отката.

# Резерв под ответ, как у бота: самый большой обычный вызов (карточка
# стиля/варианты ~1800) + буфер под рассуждения gpt-oss.
_BOT_RESERVE_TOKENS = 1800 + GroqProvider._REASONING_BUFFER
_LIMIT_RE = re.compile(r"on ([a-z ]+) \((\w+)\): Limit (\d+), Used (\d+), Requested (\d+)", re.IGNORECASE)
_RETRY_RE = re.compile(r"try again in ([\d.hms]+)", re.IGNORECASE)
_LIMIT_NAMES = {
    "TPD": "токенов за сутки", "TPM": "токенов в минуту",
    "RPD": "запросов за сутки", "RPM": "запросов в минуту",
}


def _groq_models() -> list[str]:
    """Модели Groq, которые реально использует бот: основная + для «Ответа с CueMe»."""
    variants = getattr(config, "VARIANTS_GROQ_MODELS", None)
    if variants is None:
        single = getattr(config, "VARIANTS_GROQ_MODEL", "")
        variants = [single] if single else []
    out = [GroqProvider._MODEL]
    for m in variants:
        if m and m not in out:
            out.append(m)
    return out


def _groq_left(resp: httpx.Response) -> str:
    h = resp.headers
    parts = []
    if h.get("x-ratelimit-remaining-requests"):
        parts.append(f"запросов сегодня {h['x-ratelimit-remaining-requests']}/{h.get('x-ratelimit-limit-requests', '?')}")
    if h.get("x-ratelimit-remaining-tokens"):
        parts.append(f"токенов в минуту {h['x-ratelimit-remaining-tokens']}/{h.get('x-ratelimit-limit-tokens', '?')}")
    return ", ".join(parts)


async def check_groq_for_bot(key: str, model: str) -> tuple[bool, str]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Ответь одним словом: тест пройден?"}],
        "max_tokens": _BOT_RESERVE_TOKENS,
        "reasoning_effort": "low",
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        resp = await client.post(GroqProvider._URL, headers={"Authorization": f"Bearer {key}"}, json=payload)
    left = _groq_left(resp)
    if resp.status_code == 429:
        m = _LIMIT_RE.search(resp.text)
        retry = _RETRY_RE.search(resp.text)
        if m:
            kind = _LIMIT_NAMES.get(m.group(2).upper(), m.group(1))
            detail = f"лимит {kind}: израсходовано {int(m.group(4)):,} из {int(m.group(3)):,}".replace(",", " ")
        else:
            detail = resp.text[:120]
        when = f", сброс через {retry.group(1).rstrip('.')}" if retry else ""
        return False, f"НЕ ПОТЯНЕТ запрос бота — {detail}{when}"
    if not resp.is_success:
        return False, f"HTTP {resp.status_code} — {resp.text[:150]}"
    return True, f"запрос бота потянет" + (f" (осталось: {left})" if left else "")


async def run_groq_check() -> None:
    models = _groq_models()
    print(f"\n=== Groq: всего ключей {len(GROQ_API_KEYS)}, модели бота: {', '.join(models)} ===")
    print(f"  (проверка с резервом под ответ {_BOT_RESERVE_TOKENS} токенов, как у бота)")
    if not GROQ_API_KEYS:
        print("  (не задано)")
        return
    totals = {m: 0 for m in models}
    for i, key in enumerate(GROQ_API_KEYS):
        print(f"  ключ #{i} ({_mask(key)}):")
        for model in models:
            try:
                ok, detail = await check_groq_for_bot(key, model)
            except Exception as e:
                ok, detail = False, f"исключение: {type(e).__name__}: {e}"
            totals[model] += ok
            print(f"    {model}: {'OK' if ok else 'FAIL'} -> {detail}")
    print("  итого годных для бота: " + ", ".join(f"{m} — {n} из {len(GROQ_API_KEYS)}" for m, n in totals.items()))


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
    # Было: await _run_group("Groq", GROQ_API_KEYS, check_groq) — см. комментарий у check_groq_for_bot.
    await run_groq_check()
    await run_gemini_check()
    await _run_group("OpenRouter", [OPENROUTER_API_KEY] if OPENROUTER_API_KEY else [], check_openrouter)


if __name__ == "__main__":
    asyncio.run(main())
