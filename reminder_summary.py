"""Summaries of the plan: today and the next seven days. Shared by Telegram and MAX.

style="markdown" is MAX (**bold**); style="html" is Telegram (<b>, escaped titles:
an «_» or «*» in a title must not break the message).
"""

import html
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession


WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _bold(text: str, style: str) -> str:
    return f"<b>{html.escape(text)}</b>" if style == "html" else f"**{text}**"


def _plain(text: str, style: str) -> str:
    return html.escape(text) if style == "html" else text


async def _rows(
    db: AsyncSession, platform: str, external_id: int, zone: ZoneInfo,
    first: date, last: date, current: datetime,
) -> dict[date, list[tuple[datetime, datetime, str, bool, bool]]]:
    """Per local day: (start, end, title, all_day, is_class) of what has not ended yet."""
    from schedule_service import list_schedule_occurrences
    from unified_calendar import list_linked_events

    days: dict[date, list] = {first + timedelta(days=offset): [] for offset in range((last - first).days + 1)}
    for entry in await list_linked_events(db, platform, external_id):
        start, end = aware(entry.timing.start_at), aware(entry.timing.end_at)
        if entry.event.is_completed or end <= current:
            continue
        all_day = bool(getattr(entry.timing, "all_day", False))
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if all_day:
            span = [day for day in days if local_start.date() <= day < local_end.date()]
        else:
            span = [local_start.date()] if local_start.date() in days else []
        for day in span:
            days[day].append((start, end, entry.event.title, all_day, False))
    for item in await list_schedule_occurrences(db, platform, external_id, first, last):
        start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        day = start.astimezone(zone).date()
        if end > current and day in days:
            days[day].append((start, end, item["title"], False, True))
    for items in days.values():
        items.sort(key=lambda value: (not value[3], value[0]))
    return days


def _line(row, zone: ZoneInfo, current: datetime, style: str) -> str:
    start, end, title, all_day, is_class = row
    label = _plain(("Занятие · " if is_class else "") + title, style)
    if all_day:
        return f"• весь день  {label}"
    marker = " · уже идёт" if start <= current < end else ""
    return f"• {start.astimezone(zone):%H:%M}–{end.astimezone(zone):%H:%M}  {label}{marker}"


def _overlaps(rows) -> list[tuple[str, str]]:
    """Personal timed events that overlap each other (classes and all-day ones may)."""
    timed = [row for row in rows if not row[3] and not row[4]]
    found = []
    for index, first in enumerate(timed):
        for second in timed[index + 1:]:
            if first[0] < second[1] and second[0] < first[1]:
                found.append((first[2], second[2]))
    return found


async def daily_summary_text(
    db: AsyncSession,
    platform: str,
    external_id: int,
    timezone_name: str,
    now: datetime | None = None,
    style: str = "markdown",
) -> str:
    current = aware(now or datetime.now(timezone.utc))
    zone = ZoneInfo(timezone_name or "Europe/Moscow")
    today = current.astimezone(zone).date()
    rows = (await _rows(db, platform, external_id, zone, today, today, current))[today]
    if not rows:
        return "🌅 " + _bold("Сегодня свободный день", style) + "\n\nЗапланированных мероприятий больше нет."
    lines = ["🌅 " + _bold("План на сегодня", style), ""]
    lines += [_line(row, zone, current, style) for row in rows[:20]]
    if len(rows) > 20:
        lines.append(f"…и ещё {len(rows) - 20}")
    for first, second in _overlaps(rows)[:3]:
        lines.append(f"⚠️ Пересекаются: «{_plain(first, style)}» и «{_plain(second, style)}»")
    return "\n".join(lines)


async def weekly_summary_text(
    db: AsyncSession,
    platform: str,
    external_id: int,
    timezone_name: str,
    now: datetime | None = None,
    style: str = "markdown",
) -> str:
    """The next seven days: what is planned each day, free days and overlaps."""
    current = aware(now or datetime.now(timezone.utc))
    zone = ZoneInfo(timezone_name or "Europe/Moscow")
    today = current.astimezone(zone).date()
    days = await _rows(db, platform, external_id, zone, today, today + timedelta(days=6), current)
    # A several-day event is listed on each day but counted once.
    total = len({row[:3] for rows in days.values() for row in rows if not row[4]})
    classes = sum(1 for rows in days.values() for row in rows if row[4])
    if not total and not classes:
        return "📊 " + _bold("Неделя свободна", style) + "\n\nНа ближайшие 7 дней ничего не запланировано."
    lines = ["📊 " + _bold("План на 7 дней", style)]
    counts = []
    if total:
        counts.append(f"событий: {total}")
    if classes:
        counts.append(f"занятий: {classes}")
    lines.append(", ".join(counts).capitalize())
    free, overlaps = [], []
    for day, rows in days.items():
        name = "Сегодня" if day == today else "Завтра" if day == today + timedelta(days=1) else WEEKDAYS[day.weekday()]
        if not rows:
            free.append(f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
            continue
        lines += ["", _bold(f"{name}, {day:%d.%m}", style)]
        lines += [_line(row, zone, current, style) for row in rows[:6]]
        if len(rows) > 6:
            lines.append(f"…и ещё {len(rows) - 6}")
        overlaps += [(day, pair) for pair in _overlaps(rows)]
    if free:
        lines += ["", "Свободные дни: " + ", ".join(free)]
    for day, (first, second) in overlaps[:5]:
        lines.append(f"⚠️ {WEEKDAYS[day.weekday()]} {day:%d.%m}: пересекаются «{_plain(first, style)}» и «{_plain(second, style)}»")
    return "\n".join(lines)
