"""Calendar files as different services export them."""
import io
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from calendar_import_service import (
    MAX_EVENTS,
    CalendarImportError,
    calendar_files,
    link_name,
    parse_ics,
)


MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


def wrap(body: str, head: str = "") -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n" + head + body + "END:VCALENDAR\r\n").encode()


def event(uid: str, summary: str, start: str, end: str | None = None, extra: str = "") -> str:
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART{start}\r\n"
        + (f"DTEND{end}\r\n" if end else "") + extra + "END:VEVENT\r\n"
    )


def local(item):
    return item.start.astimezone(MSK).strftime("%d.%m %H:%M"), item.end.astimezone(MSK).strftime("%d.%m %H:%M")


def test_outlook_windows_time_zone_with_and_without_vtimezone():
    vtimezone = (
        "BEGIN:VTIMEZONE\r\nTZID:Russian Standard Time\r\nBEGIN:STANDARD\r\nDTSTART:16010101T000000\r\n"
        "TZOFFSETFROM:+0300\r\nTZOFFSETTO:+0300\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    )
    body = event("o1", "Outlook", ';TZID="Russian Standard Time":20261012T100000', ';TZID="Russian Standard Time":20261012T110000',
                 "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\n")
    for head in (vtimezone, ""):
        parsed = parse_ics(wrap(body, head), "Europe/Moscow", NOW)
        assert [local(item) for item in parsed.occurrences] == [("12.10 10:00", "12.10 11:00")]


def test_yandex_style_olson_zone_without_vtimezone():
    parsed = parse_ics(wrap(event("y1", "Яндекс", ";TZID=Asia/Yekaterinburg:20261012T100000", ";TZID=Asia/Yekaterinburg:20261012T110000")), "Europe/Moscow", NOW)
    assert local(parsed.occurrences[0]) == ("12.10 08:00", "12.10 09:00")


def test_apple_floating_time_uses_the_calendar_time_zone():
    parsed = parse_ics(wrap(event("a1", "Apple", ":20261012T100000", ":20261012T110000"), "X-WR-TIMEZONE:Asia/Yekaterinburg\r\n"), "Europe/Moscow", NOW)
    assert local(parsed.occurrences[0]) == ("12.10 08:00", "12.10 09:00")
    # Without a calendar zone a floating time is the user's own.
    parsed = parse_ics(wrap(event("a2", "Apple", ":20261012T100000", ":20261012T110000")), "Europe/Moscow", NOW)
    assert local(parsed.occurrences[0]) == ("12.10 10:00", "12.10 11:00")


def test_google_export_zip_with_excluded_and_moved_occurrences():
    series = (
        event("g1", "Планёрка", ";TZID=Europe/Moscow:20261012T090000", ";TZID=Europe/Moscow:20261012T093000",
              "RRULE:FREQ=WEEKLY;COUNT=4\r\nEXDATE;TZID=Europe/Moscow:20261019T090000\r\n")
        + event("g1", "Планёрка (перенесена)", ";TZID=Europe/Moscow:20261026T120000", ";TZID=Europe/Moscow:20261026T123000",
                "RECURRENCE-ID;TZID=Europe/Moscow:20261026T090000\r\n")
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("me@gmail.com.ics", wrap(series, "X-WR-CALNAME:me@gmail.com\r\n"))
        zipped.writestr("Дни рождения.ics", wrap(event("b1", "ДР Маши", ";VALUE=DATE:20261015", ";VALUE=DATE:20261016")))
        zipped.writestr("__MACOSX/._junk.ics", b"junk")
    files = calendar_files(archive.getvalue(), "calendar.zip")
    assert [name for name, _ in files] == ["me@gmail.com.ics", "Дни рождения.ics"]
    parsed = parse_ics(files[0][1], "Europe/Moscow", NOW)
    assert [(item.title, local(item)[0]) for item in parsed.occurrences] == [
        ("Планёрка", "12.10 09:00"),  # 19.10 is excluded
        ("Планёрка (перенесена)", "26.10 12:00"),
        ("Планёрка", "02.11 09:00"),
    ]
    birthday = parse_ics(files[1][1], "Europe/Moscow", NOW).occurrences[0]
    assert birthday.all_day and local(birthday) == ("15.10 00:00", "16.10 00:00")


def test_zip_problems_have_clear_messages():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("notes.txt", "hello")
    with pytest.raises(CalendarImportError, match="нет файлов календаря"):
        calendar_files(archive.getvalue(), "x.zip")
    with pytest.raises(CalendarImportError, match="повреждён"):
        calendar_files(b"PK\x03\x04broken", "x.zip")


def test_encodings_line_endings_and_lowercase_names():
    body = event("e1", "Встреча с деканом", ":20261012T100000Z", ":20261012T110000Z")
    cp1251 = wrap(body).decode().encode("cp1251")
    assert parse_ics(cp1251, "Europe/Moscow", NOW).occurrences[0].title == "Встреча с деканом"
    bom_lf = b"\xef\xbb\xbf" + wrap(body).replace(b"\r\n", b"\n")
    assert parse_ics(bom_lf, "Europe/Moscow", NOW).occurrences[0].title == "Встреча с деканом"
    lower = wrap(body).replace(b"SUMMARY:", b"summary:").replace(b"DTSTART:", b"dtstart:")
    assert parse_ics(lower, "Europe/Moscow", NOW).occurrences[0].title == "Встреча с деканом"
    folded = wrap(event("e2", "Очень длинное название встречи, которое календарь пере\r\n носит на новую строку", ":20261012T100000Z", ":20261012T110000Z"))
    assert parse_ics(folded, "Europe/Moscow", NOW).occurrences[0].title.endswith("переносит на новую строку")


def test_durations_midnight_and_long_events():
    parsed = parse_ics(wrap(
        event("d1", "Через полночь", ";TZID=Europe/Moscow:20261012T230000", ";TZID=Europe/Moscow:20261013T010000")
        + event("d2", "С DURATION", ";TZID=Europe/Moscow:20261014T100000", extra="DURATION:PT2H\r\n")
        + event("d3", "Конференция 3 дня", ";TZID=Europe/Moscow:20261020T090000", ";TZID=Europe/Moscow:20261022T180000")
        + event("d4", "Без конца, весь день", ";VALUE=DATE:20261025")
    ), "Europe/Moscow", NOW)
    by_title = {item.title: item for item in parsed.occurrences}
    assert local(by_title["Через полночь"]) == ("12.10 23:00", "13.10 01:00")
    assert local(by_title["С DURATION"]) == ("14.10 10:00", "14.10 12:00")
    assert local(by_title["Конференция 3 дня"]) == ("20.10 09:00", "22.10 18:00")
    assert by_title["Без конца, весь день"].all_day and local(by_title["Без конца, весь день"])[1] == "26.10 00:00"


def test_today_earlier_and_running_events():
    parsed = parse_ics(wrap(
        event("t1", "Утром прошло", ";TZID=Europe/Moscow:20261010T080000", ";TZID=Europe/Moscow:20261010T090000")
        + event("t2", "Идёт сейчас", ";TZID=Europe/Moscow:20261010T110000", ";TZID=Europe/Moscow:20261010T130000")
        + event("t3", "Позже сегодня", ";TZID=Europe/Moscow:20261010T180000", ";TZID=Europe/Moscow:20261010T190000")
    ), "Europe/Moscow", NOW)
    assert [item.title for item in parsed.occurrences] == ["Идёт сейчас", "Позже сегодня"]
    assert parsed.skipped_past == 1


def test_only_tasks_vcalendar_1_and_size_limits():
    tasks = wrap("BEGIN:VTODO\r\nUID:x\r\nSUMMARY:Купить хлеб\r\nDUE:20261012T100000Z\r\nEND:VTODO\r\n")
    with pytest.raises(CalendarImportError, match="нет событий"):
        parse_ics(tasks, "Europe/Moscow", NOW)
    vcs = (b"BEGIN:VCALENDAR\r\nVERSION:1.0\r\nBEGIN:VEVENT\r\nSUMMARY:Old phone\r\n"
           b"DTSTART:20261012T100000Z\r\nDTEND:20261012T110000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert parse_ics(vcs, "Europe/Moscow", NOW).occurrences[0].title == "Old phone"
    many = wrap("".join(
        event(f"m{index}", f"Событие {index}", f":{(datetime(2026, 10, 11) + timedelta(hours=6 * index)):%Y%m%dT%H%M%S}Z",
              f":{(datetime(2026, 10, 11, 1) + timedelta(hours=6 * index)):%Y%m%dT%H%M%S}Z")
        for index in range(1300)
    ))
    parsed = parse_ics(many, "Europe/Moscow", NOW)
    assert len(parsed.occurrences) == MAX_EVENTS and parsed.truncated
    with pytest.raises(CalendarImportError, match="5 МБ"):
        parse_ics(b"x" * (5 * 1024 * 1024 + 1))


def test_link_names():
    assert link_name("https://www.thunderbird.net/media/caldata/autogen/GermanHolidays.ics", "www.thunderbird.net") == "GermanHolidays"
    assert link_name("https://calendar.google.com/calendar/ical/x/public/basic.ics", "calendar.google.com") == "calendar.google.com"
    assert link_name("webcal://example.com/", "example.com") == "example.com"
