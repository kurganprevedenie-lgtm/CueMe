"""Прогон банка сценариев через реальные функции генерации + детерминированный
скоринг (eval/checks.py). Печатает скоркарту и список провалов.

Запуск:  PYTHONPATH=<repo> python eval/run_eval.py [--limit N] [--out FILE]
LLM-судья (если доступен eval/judge.py) подключается флагом --judge.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import llm  # noqa: E402
import checks  # noqa: E402
from scenarios import SCENARIOS, STYLE_CARD, INTERACTION_CARD  # noqa: E402


def _gen(sc):
    """Возвращает корутину генерации для сценария."""
    if sc["kind"] == "rewrite":
        return llm.rewrite_message_explained(
            sc["draft"], STYLE_CARD, INTERACTION_CARD, sc["style"])
    return llm.suggest_reply(
        sc["incoming"], STYLE_CARD, INTERACTION_CARD, sc["style"],
        data_signals=sc.get("data_signals"))


def evaluate(text: str, expects: dict) -> list[str]:
    """Список ЖЁСТКИХ нарушений (пусто = всё ок). Латиница сюда не входит —
    она мягкий флаг (см. latin_flag)."""
    fails = []
    if expects.get("no_foreign") and checks.has_exotic_script(text):
        fails.append("exotic_script")
    if expects.get("no_ai_stock") and checks.has_ai_stock(text):
        fails.append("ai_stock")
    if expects.get("no_begging") and checks.has_begging(text):
        fails.append("begging")
    if expects.get("no_cliche_opener") and checks.opens_with_cliche(text):
        fails.append(f"cliche_opener:{checks.opener_word(text)}")
    mw = expects.get("max_words")
    if mw and checks.word_count(text) > mw:
        fails.append(f"too_long:{checks.word_count(text)}>{mw}")
    return fails


def effective_score(judge_score: int, has_hard_fail: bool) -> int:
    """Гибрид: детерминированное нарушение принудительно роняет балл судьи (≤3) —
    объективный факт важнее мнения LLM. Иначе — балл судьи как есть."""
    return min(judge_score, 3) if has_hard_fail else judge_score


async def _gen_with_retry(sc, retries=5):
    last = None
    for _ in range(retries):
        try:
            return await _gen(sc)
        except Exception as e:
            last = e
            await asyncio.sleep(25)
    raise last


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(SCENARIOS))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "eval_report.txt"))
    ap.add_argument("--pace", type=float, default=20.0, help="пауза между вызовами, сек")
    ap.add_argument("--judge", action="store_true", help="подключить LLM-судью (eval/judge.py)")
    args = ap.parse_args()

    judge = None
    if args.judge:
        try:
            import judge as judge_mod  # noqa: E402
            judge = judge_mod
        except Exception as e:
            print(f"судья недоступен: {e}")

    out = open(args.out, "w", encoding="utf-8")

    def w(line=""):
        print(line)
        out.write(line + "\n")
        out.flush()

    total = passed = latin_count = 0
    fail_counter: dict[str, int] = {}
    opener_counter: dict[str, int] = {}
    judge_scores = []
    eff_scores = []

    scs = SCENARIOS[: args.limit]
    for i, sc in enumerate(scs):
        try:
            msg, expl, rating = await _gen_with_retry(sc)
        except Exception as e:
            w(f"✗ {sc['id']}: ГЕНЕРАЦИЯ УПАЛА — {type(e).__name__}: {e}")
            total += 1
            await asyncio.sleep(args.pace)
            continue

        fails = evaluate(msg, sc["expects"])
        total += 1
        opener_counter[checks.opener_word(msg)] = opener_counter.get(checks.opener_word(msg), 0) + 1
        if not fails:
            passed += 1
        for f in fails:
            key = f.split(":")[0]
            fail_counter[key] = fail_counter.get(key, 0) + 1

        latin = checks.has_latin(msg)
        if latin:
            latin_count += 1

        mark = "✓" if not fails else "✗"
        w(f"{mark} {sc['id']} [{sc['style']}]")
        w(f"    ОТВЕТ: {msg.replace(chr(10), ' / ')}")
        if fails:
            w(f"    НАРУШЕНИЯ: {', '.join(fails)}")
        if latin:
            w("    ⚠ латиница (мягкий флаг — проверь глазами: бренд/ссылка или протечка)")
        if judge:
            try:
                score, verdict = await judge.score(sc, msg)
                eff = effective_score(score, bool(fails))
                judge_scores.append(score)
                eff_scores.append(eff)
                extra = f" → эффективный {eff}/10 (нарушения)" if eff != score else ""
                w(f"    СУДЬЯ: {score}/10{extra} — {verdict}")
            except Exception as e:
                w(f"    СУДЬЯ: ошибка {e}")
        w()
        await asyncio.sleep(args.pace)

    w("=" * 60)
    w(f"ИТОГ: {passed}/{total} без детерминированных нарушений")
    if fail_counter:
        w("Нарушения по типам: " + ", ".join(f"{k}={v}" for k, v in sorted(fail_counter.items())))
    w(f"Мягкий флаг латиницы: {latin_count}/{total}")
    dom = sorted(opener_counter.items(), key=lambda kv: -kv[1])[:5]
    w("Топ зачинов: " + ", ".join(f"«{k}»×{v}" for k, v in dom))
    if judge_scores:
        avg_raw = sum(judge_scores) / len(judge_scores)
        avg_eff = sum(eff_scores) / len(eff_scores)
        w(f"Средний балл судьи: raw {avg_raw:.1f} / эффективный {avg_eff:.1f} из 10")
    out.close()


if __name__ == "__main__":
    asyncio.run(main())
