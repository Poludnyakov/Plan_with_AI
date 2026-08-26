from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def daily_summary_text(
    db: AsyncSession,
    platform: str,
    external_id: int,
    timezone_name: str,
    now: datetime | None = None,
) -> str:
    from schedule_service import list_schedule_occurrences
    from unified_calendar import list_linked_events

    current = aware(now or datetime.now(timezone.utc))
    zone = ZoneInfo(timezone_name or "Europe/Moscow")
    today = current.astimezone(zone).date()
    rows: list[tuple[datetime, datetime, str, bool]] = []
    for entry in await list_linked_events(db, platform, external_id):
        start = aware(entry.timing.start_at)
        end = aware(entry.timing.end_at)
        all_day = bool(getattr(entry.timing, "all_day", False))
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        occurs_today = (
            local_start.date() <= today < local_end.date()
            if all_day else local_start.date() == today
        )
        if not entry.event.is_completed and occurs_today and end > current:
            rows.append((start, end, entry.event.title, all_day))
    for item in await list_schedule_occurrences(db, platform, external_id, today, today):
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        if end > current:
            rows.append((start, end, item["title"], False))
    rows.sort(key=lambda value: (not value[3], value[0]))
    if not rows:
        return "🌅 **Сегодня свободный день**\n\nЗапланированных мероприятий больше нет."
    lines = ["🌅 **План на сегодня**", ""]
    for start, end, title, all_day in rows[:20]:
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if all_day:
            lines.append(f"• весь день  {title}")
            continue
        marker = " · уже идёт" if start <= current < end else ""
        lines.append(f"• {local_start:%H:%M}–{local_end:%H:%M}  {title}{marker}")
    if len(rows) > 20:
        lines.append(f"…и ещё {len(rows) - 20}")
    return "\n".join(lines)
