"""Тестовые измерения не попадают в журнал рабочего процесса."""
import pytest


@pytest.fixture(autouse=True)
def isolated_llm_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_USAGE_DIR", str(tmp_path / "usage"))
    yield
    import llm_usage
    path = str(tmp_path / "usage" / "usage.jsonl")
    handler = llm_usage._handlers.pop(path, None)
    if handler:
        handler.close()
