import pytest

from llm_usage_report import build_report


def test_action_join_is_linear_and_retains_costs():
    class CountedRow(dict):
        action_reads = 0

        def get(self, key, *args):
            if key == "action_id":
                type(self).action_reads += 1
            return super().get(key, *args)

    events = []
    for n in range(300):
        action_id = str(n)
        events.append(dict(type="action", ts="2026-11-08T12:00:00+00:00",
                           action_id=action_id, origin="user", outcome="ok", functions=["variants"]))
        for attempt in range(3):
            events.append(CountedRow(type="api", ts="2026-11-08T12:00:00+00:00",
                                     request_id=f"{n}-{attempt}", action_id=action_id,
                                     origin="user", function="variants", model="test", provider="test",
                                     tariff_cost_usd=.001, retry=attempt > 0))
    # Фон не присваивается успешному действию; повтор записи не удваивает сумму.
    events.append(dict(type="api", ts="2026-11-08T12:00:00+00:00", request_id="bg",
                       action_id="background", origin="background", tariff_cost_usd=.5))
    events.append(events[1])
    report = build_report(events, "2026-11-08", "2026-11-09")
    assert report["successful_user_actions"] == 300
    assert report["production"]["api_calls"] == 901
    assert report["production"]["retries"] == 600
    assert report["average_successful_action_tariff_usd"] == pytest.approx(.003)
    assert report["production"]["total_tariff_usd"] == pytest.approx(1.4)
    # Проверяем число операций, а не зависимый от компьютера тайм-аут.
    # Прежний вложенный цикл выполнял 270000 таких чтений.
    assert CountedRow.action_reads <= 2 * 900
