"""После разнесения main.py по модулям: все модули бота импортируются без
циклов, и каждое обращение «по ссылке» — `handlers.<модуль>.<имя>`,
`app_state.<имя>`, `main.<имя>` — указывает на реально существующее имя.
Такие вызовы срабатывают только во время работы бота, поэтому опечатку или
забытое имя обычные тесты могли бы не поймать."""
import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bot_module_files() -> list[Path]:
    files = [ROOT / "app_state.py"] if (ROOT / "app_state.py").exists() else []
    return files + sorted((ROOT / "handlers").glob("*.py")) if (ROOT / "handlers").exists() else files


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def _dotted(node) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def _refs(path: Path) -> list[tuple[int, list[str]]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and not isinstance(getattr(node, "_parent", None), ast.Attribute):
            chain = _dotted(node)
            if not chain:
                continue
            if chain[0] == "handlers" and len(chain) >= 3:
                out.append((node.lineno, chain[:3]))
            elif chain[0] in ("app_state", "main") and len(chain) >= 2:
                out.append((node.lineno, chain[:2]))
    return out


def test_all_bot_modules_import_in_order():
    import main  # noqa: F401 — грузит модули в порядке регистрации хендлеров
    for path in _bot_module_files():
        assert importlib.import_module(_module_name(path)) is sys.modules[_module_name(path)]


def test_module_refs_resolve():
    import main
    missing = []
    for path in _bot_module_files():
        for lineno, chain in _refs(path):
            if chain[0] == "handlers":
                mod = sys.modules.get(f"handlers.{chain[1]}")
                ok = mod is not None and hasattr(mod, chain[2])
            elif chain[0] == "app_state":
                ok = hasattr(sys.modules["app_state"], chain[1])
            else:
                ok = hasattr(main, chain[1])
            if not ok:
                missing.append(f"{path.name}:{lineno} {'.'.join(chain)}")
    assert not missing, "не разрешаются: " + ", ".join(missing)
