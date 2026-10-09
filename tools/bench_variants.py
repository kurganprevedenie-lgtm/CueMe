"""Бенчмарк провайдеров/моделей на промпте «Ответа с CueMe» (варианты ответа).

Промпт — настоящий шаблон из llm.suggest_reply_variants (перехватывается до
вызова LLM), с синтетическими карточками стиля и собеседника реалистичной
длины (не данные реальных юзеров). Каждый кандидат получает одни и те же 5
входящих сообщений; время — каждого запроса, ответ прогоняется через
llm._parse_variants. В конце — таблица и сами варианты для сравнения качества.

Запуск на сервере из папки бота:
    cd /root/CueMe && venv/bin/python /root/bench.py
Тратит ~30 запросов к LLM. Бота не трогает, базу не открывает.
"""
import asyncio
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, "/root/CueMe")

import llm  # noqa: E402
from config import GEMINI_API_KEYS  # noqa: E402

INPUTS = [
    "ты сегодня какой-то подозрительно долго не писал, случайно не заскучал по мне? 😏",
    "ну не знаю, я в эти выходные вроде занята",
    "ахах, ты всегда такой самоуверенный?",
    "мне сегодня что-то грустно, день вообще не задался",
    "слушай, а давай как-нибудь кофе выпьем?",
]
STYLE = (
    "🧬 Голос: пишет коротко, 1-2 предложения, со строчной буквы, почти без "
    "точек в конце. Часто «ахах», «ну», «слушай». Шутит с лёгкой иронией, "
    "не пошлит. Эмодзи редко — 😏 и 😅.\n\n✍️ Длина: 6-14 слов, длинные "
    "сообщения дробит на 2-3 подряд.\n\n🗣️ Тон: уверенный, чуть дерзкий, "
    "на «ты», без канцелярита. Вопросы задаёт открытые, любит подхватывать "
    "детали из её сообщений.\n\n❗ Не делает: не оправдывается, не пишет "
    "простыни, не ставит «)))» больше одной скобки."
)
INTER = (
    "💚 Что заходит: лёгкий флирт с иронией, внимание к деталям её дня, "
    "конкретные предложения встретиться с местом и временем.\n\n🚩 Что "
    "отталкивает: долгие паузы без объяснений, банальные вопросы «как дела», "
    "давление после «не знаю».\n\n🎭 Манера: отвечает быстро вечером, днём "
    "коротко; проверяет интерес провокационными вопросами; смайлики 😏🙈.\n\n"
    "💡 Динамика: интерес высокий, сама инициирует примерно половину диалогов."
)


async def capture_prompt(text: str) -> str:
    box = {}

    async def fake_ask(prompt, max_tokens=1024, **kw):
        box["prompt"], box["max_tokens"] = prompt, max_tokens
        raise RuntimeError("captured")

    real = llm._ask
    llm._ask = fake_ask
    try:
        await llm.suggest_reply_variants(text, STYLE, INTER, user_gender="male")
    except RuntimeError:
        pass
    finally:
        llm._ask = real
    return box["prompt"], box["max_tokens"]


class Groq20b(llm.GroqProvider):
    _MODEL = "openai/gpt-oss-20b"


async def groq(provider_cls, fast: bool, prompt: str, max_tokens: int) -> str:
    llm._llm_fast.set(fast)
    return await provider_cls().ask(prompt, max_tokens)


async def gemini(model: str, prompt: str, max_tokens: int) -> str:
    last = None
    for key in GEMINI_API_KEYS:
        try:
            return await llm.GeminiProvider()._ask_with_key(prompt, max_tokens, key, model)
        except llm.RateLimitError as e:
            last = e
            continue
    raise last or RuntimeError("нет ключей Gemini")


CANDIDATES = [
    ("Groq 120b (как было)", lambda p, m: groq(llm.GroqProvider, False, p, m)),
    ("Groq 120b low", lambda p, m: groq(llm.GroqProvider, True, p, m)),
    ("Groq 20b low", lambda p, m: groq(Groq20b, True, p, m)),
    ("Gemini 3.1 Flash Lite", lambda p, m: gemini("gemini-3.1-flash-lite", p, m)),
    ("Gemini 3.5 Flash Lite", lambda p, m: gemini("gemini-3.5-flash-lite", p, m)),
    ("Gemma 4 26B (для сравнения)", lambda p, m: gemini("gemma-4-26b-a4b-it", p, m)),
]


async def main() -> None:
    prompts = [await capture_prompt(t) for t in INPUTS]
    print(f"Промпт: ~{len(prompts[0][0])} символов, max_tokens={prompts[0][1]}\n", flush=True)
    results = {}
    for name, call in CANDIDATES:
        rows = []
        for (prompt, mt), text in zip(prompts, INPUTS):
            t0 = time.monotonic()
            try:
                raw = await asyncio.wait_for(call(prompt, mt), timeout=60)
                dt = time.monotonic() - t0
                parsed = llm._parse_variants(raw, 3) if raw.strip() else []
                status = "ok" if len(parsed) == 3 else ("пусто" if not raw.strip() else f"распарсено {len(parsed)}/3")
            except Exception as e:
                dt, parsed, status = time.monotonic() - t0, [], f"{type(e).__name__}: {str(e)[:60]}"
            rows.append((text, dt, status, parsed))
            print(f"{name:30} {dt:5.1f} с  {status}", flush=True)
        results[name] = rows

    print("\n\n=== ИТОГ ===")
    print(f"{'кандидат':30} {'ок':>4} {'пусто/битых':>12} {'медиана':>8} {'худшее':>7}")
    for name, rows in results.items():
        ok = [r for r in rows if r[2] == "ok"]
        times = [r[1] for r in ok]
        med = f"{statistics.median(times):.1f} с" if times else "—"
        worst = f"{max(times):.1f} с" if times else "—"
        print(f"{name:30} {len(ok):>2}/5 {len(rows) - len(ok):>12} {med:>8} {worst:>7}")

    out = f"/root/bench_variants_{datetime.now():%Y%m%d_%H%M%S}.txt"
    with open(out, "w", encoding="utf-8") as f:
        for i, text in enumerate(INPUTS):
            f.write(f"\n\n######## «{text}» ########\n")
            for name, rows in results.items():
                _, dt, status, parsed = rows[i]
                f.write(f"\n--- {name} [{dt:.1f} с, {status}]\n")
                for label, body in parsed:
                    f.write(f"  {label}: {body}\n")
    print(f"\nВсе варианты для сравнения качества: {out}")
    print(f"Показать: cat {out}")


if __name__ == "__main__":
    asyncio.run(main())
