from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from calendar_import_service import (
    CalendarImportError,
    _public_host,
    caldav_url,
    find_ics_link,
    import_calendar,
    import_from_caldav,
    is_ics_filename,
    normalize_link,
    parse_caldav_credentials,
    parse_ics,
    result_text,
    undo_calendar_import,
)
from database import Base
from history_service import addition_history, delete_history_item, history_page
from max_bot.models import MaxUser
from models import User
from unified_calendar import create_linked_event, list_linked_events


ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)  # 12:00 in Moscow
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
X-WR-CALNAME:Work
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:standup@example.com
SUMMARY:Standup
DTSTART;TZID=Europe/Berlin:20261012T100000
DTEND;TZID=Europe/Berlin:20261012T101500
RRULE:FREQ=WEEKLY;COUNT=4
LOCATION:Room 1
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:vacation@example.com
SUMMARY:Отпуск
DTSTART;VALUE=DATE:20261020
DTEND;VALUE=DATE:20261023
END:VEVENT
BEGIN:VEVENT
UID:old@example.com
SUMMARY:Прошло
DTSTART:20261001T100000Z
DTEND:20261001T110000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled@example.com
SUMMARY:Отменили
STATUS:CANCELLED
DTSTART:20261015T100000Z
DTEND:20261015T110000Z
END:VEVENT
BEGIN:VEVENT
UID:planiruy-event-5@planwithai.ru
SUMMARY:Our own feed
DTSTART:20261016T100000Z
DTEND:20261016T110000Z
END:VEVENT
BEGIN:VEVENT
UID:floating@example.com
SUMMARY:Floating deadline
DTSTART:20261014T180000
END:VEVENT
END:VCALENDAR
""".encode()


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([User(tg_id=11, timezone="Europe/Moscow"), MaxUser(max_user_id=33)])
            await session.commit()
            yield session
    await engine.dispose()


def test_parse_unfolds_repeats_and_skips_past_cancelled_and_own_events():
    parsed = parse_ics(ICS, "Europe/Moscow", NOW)
    titles = [item.title for item in parsed.occurrences]
    assert parsed.name == "Work"
    assert titles.count("Standup") == 4
    assert "Прошло" not in titles and "Отменили" not in titles and "Our own feed" not in titles
    standup = next(item for item in parsed.occurrences if item.title == "Standup")
    # 10:00 in Berlin (UTC+2 in October) is 11:00 in Moscow.
    assert standup.start.astimezone(ZONE).strftime("%d.%m %H:%M") == "12.10 11:00"
    assert standup.end - standup.start == timedelta(minutes=15)
    assert standup.description == "📍 Room 1"
    assert standup.alarms == [standup.start - timedelta(minutes=10)]
    vacation = next(item for item in parsed.occurrences if item.title == "Отпуск")
    assert vacation.all_day and vacation.start == datetime(2026, 10, 20, tzinfo=ZONE)
    assert vacation.end == datetime(2026, 10, 23, tzinfo=ZONE)
    floating = next(item for item in parsed.occurrences if item.title == "Floating deadline")
    # A floating time is the user's own; no end means a short deadline.
    assert floating.start == datetime(2026, 10, 14, 18, tzinfo=ZONE)
    assert floating.end - floating.start == timedelta(minutes=30)


def test_bad_input_gets_a_clear_message():
    with pytest.raises(CalendarImportError, match="iCalendar"):
        parse_ics(b"hello, this is not a calendar")


def test_link_and_file_detection():
    assert find_ics_link("вот webcal://p01-caldav.icloud.com/published/2/abc") == "webcal://p01-caldav.icloud.com/published/2/abc"
    assert find_ics_link("https://calendar.google.com/calendar/ical/x%40gmail.com/private-1/basic.ics.") == (
        "https://calendar.google.com/calendar/ical/x%40gmail.com/private-1/basic.ics"
    )
    assert find_ics_link("https://calendar.yandex.ru/export/ics.xml?private_token=1&tz_id=Europe/Moscow")
    assert find_ics_link("https://example.com/page") is None
    assert normalize_link("webcal://a.ru/x.ics") == "https://a.ru/x.ics"
    assert is_ics_filename("Work.ICS") and is_ics_filename("x", "text/calendar") and not is_ics_filename("a.xlsx")
    assert caldav_url("yandex") == "https://caldav.yandex.ru"
    assert caldav_url("cloud.example.com/remote.php/dav") == "https://cloud.example.com/remote.php/dav"
    assert parse_caldav_credentials("yandex\nme@yandex.ru\nabcdabcdabcdabcd") == ("yandex", "me@yandex.ru", "abcdabcdabcdabcd")
    assert parse_caldav_credentials("just text") is None


def test_links_into_the_server_network_are_refused():
    for url in ("http://127.0.0.1:8000/x.ics", "http://localhost/x.ics", "http://10.0.0.5/x.ics", "file:///etc/passwd"):
        with pytest.raises(CalendarImportError):
            _public_host(url)


@pytest.mark.anyio
async def test_import_is_one_history_entry_repeats_only_new_and_deletes_whole(db):
    await create_linked_event(
        db, "telegram", 11, "Своё событие", "",
        datetime(2026, 10, 13, 15, tzinfo=ZONE), datetime(2026, 10, 13, 16, tzinfo=ZONE),
    )
    parsed = parse_ics(ICS, "Europe/Moscow", NOW)
    result = await import_calendar(db, "telegram", 11, "file", "Work", parsed, now=NOW)
    assert result.created == 6 and result.duplicates == 0
    # Overlapping events from another calendar are imported, not refused.
    titles = [entry.event.title for entry in await list_linked_events(db, "telegram", 11)]
    assert titles.count("Standup") == 4 and "Своё событие" in titles
    assert "добавлено 6 событий" in result_text([result])

    again = await import_calendar(db, "telegram", 11, "file", "Work", parse_ics(ICS, "Europe/Moscow", NOW), now=NOW)
    assert again.created == 0 and again.duplicates == 6 and again.import_id is None

    history = await addition_history(db, "telegram", 11)
    refs = [item.ref for item in history]
    assert f"cal:{result.import_id}" in refs
    # Imported events are not listed one by one.
    assert [item.title for item in history if item.kind == "event"] == ["Своё событие"]
    text, _, _ = history_page(history)
    assert "📆 Work" in text and "6 событий · из файла" in text

    reply = await delete_history_item(db, "telegram", 11, f"cal:{result.import_id}")
    assert reply == "🗑 Импорт календаря удалён: убрано 6 событий."
    assert [entry.event.title for entry in await list_linked_events(db, "telegram", 11)] == ["Своё событие"]
    assert await undo_calendar_import(db, "telegram", 11, result.import_id) == ("already_undone", 0)
    # After deleting, the same calendar can be imported again.
    third = await import_calendar(db, "telegram", 11, "file", "Work", parse_ics(ICS, "Europe/Moscow", NOW), now=NOW)
    assert third.created == 6


@pytest.mark.anyio
async def test_max_import_and_reminders_from_the_calendar(db):
    result = await import_calendar(db, "max", 33, "link", "Work", parse_ics(ICS, "Europe/Moscow", NOW), now=NOW)
    entries = await list_linked_events(db, "max", 33)
    standup = next(entry for entry in entries if entry.event.title == "Standup")
    assert standup.reminders == [standup.timing.start_at.replace(tzinfo=timezone.utc) - timedelta(minutes=10)] or \
        [value - standup.timing.start_at.replace(tzinfo=timezone.utc) for value in standup.reminders] == [timedelta(minutes=-10)]
    vacation = next(entry for entry in entries if entry.event.title == "Отпуск")
    assert vacation.timing.all_day
    # Another account cannot delete this import.
    assert await undo_calendar_import(db, "telegram", 11, result.import_id) == ("missing", 0)


@pytest.mark.anyio
async def test_caldav_imports_each_calendar_separately(db):
    single = ICS.replace(b"X-WR-CALNAME:Work\n", b"")
    with patch("calendar_import_service.fetch_caldav", new_callable=AsyncMock,
               return_value=[("Работа", [single]), ("Пустой", [])]):
        results = await import_from_caldav(db, "telegram", 11, "yandex", "me", "secret", now=NOW)
    assert [(item.source_name, item.created > 0) for item in results] == [("Работа", True)]
