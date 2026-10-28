"""/users: срез отчёта по полу (переключатель «Все / Парни / Девушки»)."""
from tests.bot_patch import patch_bot
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import storage


class _Bot:
    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, username=f"u{chat_id}", full_name=None, first_name=None)

    async def get_chat_member(self, chat, uid):
        return SimpleNamespace(status="left")


@pytest.fixture()
def data(monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    storage.init_db()
    import main
    patch_bot(monkeypatch, "PREMIUM_CHANNEL_ID", "")
    main._premium_cache.clear()
    for tid, gender in [("1", "male"), ("2", "male"), ("3", "female"), ("4", None)]:
        storage.upsert_user(tid, f"user{tid}")
        if gender:
            storage.set_gender(tid, gender)
    storage.get_or_create_contact("1", "user9", "Маша")
    storage.record_event("3", "gen_reply_variants", "")
    rows, totals = asyncio.run(main._collect_users_data(_Bot()))
    return main, rows, totals


def test_totals_from_rows_match_loop_totals(data):
    main, rows, totals = data
    assert main._users_totals(rows, totals["gate_tracking_start"]) == totals


def test_subset_totals(data):
    main, rows, totals = data
    male = main._users_totals([r for r in rows if r["gender"] == "парень"], None)
    assert male["total"] == 2 and male["with_contact"] == 1 and male["used_reply"] == 0
    female = main._users_totals([r for r in rows if r["gender"] == "девушка"], None)
    assert female["total"] == 1 and female["used_reply"] == 1


def test_html_has_switch_and_variant_per_gender(data):
    main, rows, totals = data
    page = main._build_users_html(rows, totals).decode("utf-8")
    assert 'class="g-switch"' in page
    for key, label in [("all", "Все"), ("парень", "Парни · 2"), ("девушка", "Девушки · 1"), ("?", "Не указан · 1")]:
        assert f'class="dash-variant" data-g="{key}"' in page
        assert label in page


def test_no_switch_when_nobody_has_gender(data, monkeypatch):
    main, rows, totals = data
    for r in rows:
        r["gender"] = "?"
    page = main._build_users_html(rows, totals).decode("utf-8")
    assert 'class="g-switch"' not in page and page.count('class="dash-variant"') == 1
