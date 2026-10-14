"""«Анализ собеседника» на контакте с двумя источниками (инцидент 2026-10-14:
после загрузки JSON-экспорта поверх Business анализ падал TypeError —
даты «с поясом» и «без пояса» вычитались друг из друга)."""
import asyncio
import json
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import storage


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    storage.init_db()
    storage.upsert_user("1", "user1")
    cid = storage.get_or_create_contact("1", "user777", "Маша")
    with storage._conn() as c:
        c.execute("INSERT OR REPLACE INTO business_chat_refs VALUES ('1','ref1',?)", (cid,))
    return cid


def _fill_big_mixed(cid, n_json=2300, n_biz=40):
    random.seed(3)
    t = datetime(2026, 4, 1, 9, 0)
    imported = []
    for i in range(n_json):
        t += timedelta(minutes=random.randint(1, 120))
        imported.append({"direction": "out" if i % 2 else "in", "text": f"сообщение {i} про кино", "date": t.isoformat()})
    storage.save_imported_messages(cid, imported)  # даты без пояса, как у JSON-экспорта
    tb = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    for i in range(n_biz):
        tb += timedelta(minutes=5)
        storage.save_business_message("c1", "1", "ref1", "in" if i % 2 else "out", f"новое {i}", tb.isoformat(), 10_000 + i, {})


def test_dates_are_utc_aware_and_text_is_str(db):
    _fill_big_mixed(db, n_json=50, n_biz=5)
    rows = storage.get_all_dated_messages("1", db)
    assert all(datetime.fromisoformat(r["date"]).tzinfo is not None for r in rows)
    assert all(isinstance(r["text"], str) for r in rows)


def test_json_naive_date_is_moscow_and_duplicate_is_merged(db):
    # одно и то же сообщение: в JSON 13:00 (локальное МСК), в Business 10:00 UTC
    storage.save_imported_messages(db, [{"direction": "in", "text": "привет", "date": "2026-10-01T13:00:00"}])
    storage.save_business_message("c1", "1", "ref1", "in", "привет", "2026-10-01T10:00:00+00:00", 1, {})
    rows = storage.get_all_dated_messages("1", db)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-10-01T10:00:00+00:00"


def test_export_timezone_is_guessed_from_overlap(db):
    # экспорт сделан на компьютере в UTC+2: в JSON 12:00, в Business 10:00 UTC
    base = datetime(2026, 10, 13, 10, 0, 7)
    imported, n = [], 30
    for i in range(n):
        local = base + timedelta(hours=2, minutes=3 * i)
        imported.append({"direction": "in" if i % 2 else "out", "text": f"текст {i}", "date": local.isoformat()})
        storage.save_business_message("c1", "1", "ref1", "in" if i % 2 else "out", f"текст {i}",
                                      (base + timedelta(minutes=3 * i)).replace(tzinfo=timezone.utc).isoformat(), i, {})
    storage.save_imported_messages(db, imported)
    rows = storage.get_all_dated_messages("1", db)
    assert len(rows) == n


def test_metrics_on_big_mixed_contact_do_not_crash(db):
    from compatibility_metrics import compute_all
    _fill_big_mixed(db)
    rows = storage.get_all_dated_messages("1", db)
    metrics = compute_all(rows)
    metrics.pop("_volume_trend")
    json.dumps(metrics, ensure_ascii=False)


def test_parse_dt_always_aware():
    from compatibility_metrics import _parse_dt
    assert _parse_dt("2026-10-01T10:00:00").tzinfo is not None
    assert _parse_dt("2026-10-01T10:00:00+03:00").tzinfo is not None


def test_tg_parser_text_entities_become_string():
    from tg_parser import _normalize_text
    assert _normalize_text([{"type": "bold", "text": "смотри"}, " ", {"type": "link", "text": "ya.ru"}]) == "смотри ya.ru"


def _patch_gen(monkeypatch, interp):
    import main
    monkeypatch.setattr(main, "build_compatibility_interpretation", interp)
    monkeypatch.setattr(main, "get_gender", lambda tid: "male")
    saved = []
    monkeypatch.setattr(main, "save_deep_analysis", lambda *a: saved.append(a))
    return main, saved


def test_llm_failure_degrades_to_metrics_and_is_not_cached(db, monkeypatch):
    _fill_big_mixed(db)

    async def boom(*a, **k):
        raise RuntimeError("Все LLM-провайдеры недоступны")
    main, saved = _patch_gen(monkeypatch, boom)
    data = asyncio.run(main._gen_deep_analysis(db, "1"))
    assert data["degraded"] is True
    assert "Обновить анализ" in data["synthesis_text"]
    assert json.loads(data["metrics_json"])["balance"]["fact"]
    assert saved == []


def test_success_is_cached(db, monkeypatch):
    _fill_big_mixed(db)

    async def ok(*a, **k):
        return {}, "динамика", "вывод", "что дальше", "сообщение"
    main, saved = _patch_gen(monkeypatch, ok)
    data = asyncio.run(main._gen_deep_analysis(db, "1"))
    assert data["degraded"] is False and len(saved) == 1


def test_degraded_result_does_not_charge_trial(db, monkeypatch):
    import main
    from types import SimpleNamespace
    _fill_big_mixed(db)

    async def boom(*a, **k):
        raise RuntimeError("мусор от модели")
    monkeypatch.setattr(main, "build_compatibility_interpretation", boom)

    async def prem(bot, tid):
        return False
    monkeypatch.setattr(main, "_is_premium", prem)
    storage.add_analysis_trial_bonus("1", 3)

    async def passthrough(bot, chat_id, message_id, coro, **kw):
        return await coro
    monkeypatch.setattr(main, "with_progress_animation", passthrough)

    class Msg:
        message_id = 1
        chat = SimpleNamespace(id=1)

        async def answer(self, *a, **k):
            return self

        async def edit_text(self, *a, **k):
            return self

        async def delete(self):
            pass

    class Bot:
        async def send_rich_message(self, *a, **k):
            raise RuntimeError("нет rich")

    asyncio.run(main._run_deep_analysis(Bot(), Msg(), "1", db))
    assert storage.get_analysis_trial_count("1") == 0
