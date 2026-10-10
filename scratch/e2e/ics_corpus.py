"""Parse every .ics we can find: no crash, sane events, clear messages for bad files."""
import re, sys, traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from calendar_import_service import CalendarImportError, parse_ics  # noqa: E402

ROOTS = [Path(p) for p in sys.argv[1:]] or [
    Path(__file__).resolve().parents[2] / "venv/lib/python3.12/site-packages/icalendar/tests",
    Path(__file__).resolve().parents[2] / "venv/lib/python3.12/site-packages/recurring_ical_events/test/calendars",
]
DT = re.compile(rb"DTSTART[^:\r\n]*:(\d{8})")


def first_day(data: bytes):
    days = sorted(m.group(1) for m in DT.finditer(data))
    for day in days:
        try:
            return datetime.strptime(day.decode(), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


stats, crashes, insane, messages = Counter(), [], [], Counter()
for path in sorted(p for root in ROOTS for p in root.rglob("*.ics")):
    data = path.read_bytes()
    start = first_day(data)
    now = (start - timedelta(days=1)) if start else datetime.now(timezone.utc)
    try:
        parsed = parse_ics(data, "Europe/Moscow", now)
    except CalendarImportError as error:
        stats["clear message"] += 1
        messages[str(error)] += 1
        continue
    except Exception as error:
        crashes.append((path.name, f"{type(error).__name__}: {error}", traceback.format_exc(limit=-3)))
        continue
    stats["ok"] += 1
    stats["events"] += len(parsed.occurrences)
    for item in parsed.occurrences:
        problems = []
        if item.start.tzinfo is None or item.end.tzinfo is None:
            problems.append("naive time")
        if item.end <= item.start:
            problems.append("end<=start")
        if not item.title.strip():
            problems.append("no title")
        if item.all_day and (item.start.hour or item.start.minute):
            problems.append("all-day not at midnight")
        if not item.all_day and item.end - item.start > timedelta(days=31):
            problems.append(f"timed event of {item.end - item.start}")
        if problems:
            insane.append((path.name, item.title, item.start, item.end, problems))

print(f"файлов: {sum(v for k, v in stats.items() if k != 'events')}, разобрано: {stats['ok']}, "
      f"понятный отказ: {stats['clear message']}, событий: {stats['events']}")
print("сообщения об отказе:", dict(messages))
print(f"\nПАДЕНИЙ: {len(crashes)}")
for name, error, trace in crashes:
    print(f"  {name}: {error}\n{trace}")
print(f"СТРАННЫХ СОБЫТИЙ: {len(insane)}")
for row in insane[:40]:
    print("  ", row)
