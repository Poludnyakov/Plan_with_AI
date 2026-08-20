from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from intelligent_reminders import contextual_fallback, standard_reminders


def test_homework_deadline_at_noon_gets_work_time_in_the_morning():
    zone = ZoneInfo("Europe/Moscow")
    start = datetime(2026, 8, 24, 12, 0, tzinfo=zone)
    now = datetime(2026, 8, 22, 8, 0, tzinfo=zone)
    local = [value.astimezone(zone) for value in contextual_fallback(
        "Домашняя работа — дедлайн", start, zone.key, now
    )]
    assert any(value.date() == start.date() and value.hour == 9 for value in local)
    assert any(value.date().day == 23 and value.hour == 20 for value in local)
    assert all(now < value < start for value in local)


def test_standard_mode_keeps_fixed_non_spam_offsets():
    start = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
    values = standard_reminders(start, now)
    assert len(values) == 4
    assert values == sorted(set(values))
    assert all(now < value < start for value in values)
