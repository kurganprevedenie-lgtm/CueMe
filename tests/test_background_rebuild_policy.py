import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import main  # сохраняет порядок регистрации обработчиков
import app_state
from handlers import cards, subscription


@pytest.mark.parametrize(
    "activity_days, expiry_days, remaining, expected",
    [
        (1, None, None, "trial"),  # новый пользователь до открытия пула
        (13, None, 1, "trial"),
        (14, None, 1, None),
        (None, None, 1, None),
        (1, None, 0, None),
        (60, 1, 0, "premium"),
        (60, -13, 0, "trial"),
        (60, -14, 0, None),
        (1, -20, 1, "trial"),
    ],
)
def test_rebuild_uses_direct_activity_trial_and_paid_window(
    monkeypatch, activity_days, expiry_days, remaining, expected
):
    now = datetime.now(timezone.utc)
    user = {
        "last_action_at": (now - timedelta(days=activity_days)).isoformat() if activity_days is not None else None,
        "stars_premium_until": None,
        "tribute_premium_until": (now + timedelta(days=expiry_days)).isoformat() if expiry_days is not None else None,
        "reply_trial_bonus": 2 if remaining is not None else 0,
        "trial_used": 2 - remaining if remaining is not None else 0,
        "analysis_trial_bonus": 0,
        "analysis_trial_used": 0,
        "date_trial_bonus": 0,
        "date_trial_used": 0,
    }
    monkeypatch.setattr(cards, "get_user", lambda _: user)
    monkeypatch.setattr(subscription, "_premium_cache", {})
    assert cards._background_rebuild_tier("42") == expected


def test_trial_jobs_cannot_occupy_both_slots(monkeypatch):
    async def run():
        monkeypatch.setattr(cards, "_rebuild_slots", asyncio.Semaphore(2))
        monkeypatch.setattr(app_state, "_rebuild_trial_slot", asyncio.Semaphore(1))
        monkeypatch.setattr(cards, "_rebuilding", set())
        monkeypatch.setattr(cards, "_rebuild_checking", set())
        monkeypatch.setattr(app_state, "_rebuild_trial_jobs", set())
        monkeypatch.setattr(cards, "_background_rebuild_tier", lambda owner: owner)
        monkeypatch.setattr(cards, "_rebuild_on_cooldown", lambda _: False)
        monkeypatch.setattr(cards, "get_my_style_last_rebuild_count", lambda _: 0)
        monkeypatch.setattr(cards, "count_biz_messages_for_contact", lambda *_: 1000)
        monkeypatch.setattr(cards, "get_my_style_per_contact", lambda _: "existing")
        monkeypatch.setattr(cards, "_refresh_samples", lambda *_: None)
        started = {contact: asyncio.Event() for contact in (1, 2, 3)}
        release = asyncio.Event()

        async def rebuild(_, contact):
            started[contact].set()
            if contact != 3:
                await release.wait()
            return False

        monkeypatch.setattr(cards, "_rebuild_contact", rebuild)
        first = asyncio.create_task(cards._maybe_rebuild("trial", 1))
        second = asyncio.create_task(cards._maybe_rebuild("trial", 2))
        try:
            await asyncio.wait_for(started[1].wait(), 3)
            await asyncio.wait_for(cards._maybe_rebuild("premium", 3), 3)
            assert started[3].is_set()
            assert not started[2].is_set()
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert started[2].is_set()

    asyncio.run(run())
