"""Страховка на время разнесения main.py по модулям: список всех хендлеров
диспетчера (тип события, фильтры, имя функции) в порядке, в котором aiogram
их перебирает, должен один в один совпадать с эталоном
tests/fixtures/handler_registry.json, снятым с main.py до рефакторинга.

Порядок важен: aiogram берёт ПЕРВЫЙ подходящий хендлер. Например, в
состоянии «жду имя» (UnifiedReply.waiting_for_name) хендлер без фильтра
команд перехватывает /help, потому что зарегистрирован раньше cmd_help.

Пересоздать эталон (только осознанно, если поведение меняют намеренно):
    py -3.13 -m tests.test_handler_registry --update
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import handler_registry  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "handler_registry.json"


def _current() -> list[dict]:
    import main
    return handler_registry.collect(main.dp)


def test_registry_matches_fixture():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    current = _current()
    assert len(current) == len(expected), f"хендлеров {len(current)}, в эталоне {len(expected)}"
    for i, (cur, exp) in enumerate(zip(current, expected)):
        assert cur == exp, f"расхождение на позиции {i}: сейчас {cur}, в эталоне {exp}"


if __name__ == "__main__" and "--update" in sys.argv:
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(json.dumps(_current(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"эталон обновлён: {FIXTURE}")
