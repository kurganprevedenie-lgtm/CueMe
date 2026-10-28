"""Подмена имени бота в тестах после разнесения main.py по модулям.

Раньше весь бот был одним пространством имён, и monkeypatch.setattr(main,
"X", ...) подменял X для всего кода сразу. Теперь X живёт в одном модуле
(app_state / handlers.*), а другие модули держат свою ссылку на него (from …
import X) — подменять надо везде, где имя есть, иначе вызывающий код увидит
оригинал. patch_bot делает ровно это: тот же эффект, что раньше давал main.
"""
import sys

_BOT_MODULE_PREFIXES = ("app_state", "handlers.", "main")


def _bot_modules():
    import main  # noqa: F401 — модули бота загружаются в порядке main
    for name, mod in list(sys.modules.items()):
        if mod is not None and (name in ("app_state", "main") or name.startswith("handlers.")):
            yield mod


def patch_bot(monkeypatch, name: str, value) -> None:
    found = False
    for mod in _bot_modules():
        if name in vars(mod):
            monkeypatch.setattr(mod, name, value)
            found = True
    assert found, f"{name} нет ни в одном модуле бота"
