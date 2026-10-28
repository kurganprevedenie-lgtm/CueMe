"""Снимок всех хендлеров, зарегистрированных на диспетчере бота, в порядке, в
котором aiogram их реально перебирает: сначала хендлеры самого роутера, потом
вложенные роутеры по порядку подключения (так работает propagation в
aiogram 3). Нужен тесту tests/test_handler_registry.py — страховка на время
разнесения main.py по модулям."""


def _describe(obj, depth=0) -> str:
    """Стабильное текстовое описание фильтра без адресов памяти: MagicFilter —
    по цепочке операций, Command/StateFilter — по полям, функции — по имени."""
    if depth > 6:
        return "…"
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return repr(obj)
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [_describe(x, depth + 1) for x in obj]
        return "[" + ", ".join(sorted(items) if isinstance(obj, (set, frozenset)) else items) + "]"
    if callable(obj) and hasattr(obj, "__qualname__") and not hasattr(obj, "_operations"):
        self_ = getattr(obj, "__self__", None)
        if self_ is not None and hasattr(self_, "_operations"):  # bound MagicFilter.resolve
            return _describe(self_, depth + 1)
        return obj.__qualname__
    ops = getattr(obj, "_operations", None)
    if ops is not None:
        return "F" + "".join("." + _describe(op, depth + 1) for op in ops)
    fields = getattr(obj, "__slots__", None) or list(vars(obj)) if hasattr(obj, "__dict__") or hasattr(obj, "__slots__") else []
    if isinstance(fields, str):
        fields = [fields]
    parts = []
    for name in fields:
        if name.startswith("__"):
            continue
        try:
            parts.append(f"{name.lstrip('_')}={_describe(getattr(obj, name), depth + 1)}")
        except AttributeError:
            pass
    if not parts and hasattr(obj, "state"):  # FSM State
        return f"State({obj.state})"
    return f"{type(obj).__name__}({', '.join(parts)})"


def _filter_repr(f) -> str:
    return _describe(f.magic if f.magic is not None else f.callback)


def collect(router) -> list[dict]:
    out = []
    for event_name, observer in router.observers.items():
        for h in observer.handlers:
            out.append({
                "event": event_name,
                "callback": f"{h.callback.__module__.split('.')[-1]}.{h.callback.__qualname__}".split(".", 1)[1],
                "filters": [_filter_repr(f) for f in h.filters or []],
            })
    for sub in router.sub_routers:
        out.extend(collect(sub))
    return out


def by_event(entries: list[dict]) -> dict[str, list[dict]]:
    """Порядок важен только внутри одного типа события — aiogram для каждого
    апдейта перебирает хендлеры своего типа."""
    res: dict[str, list[dict]] = {}
    for e in entries:
        res.setdefault(e["event"], []).append(e)
    return res
