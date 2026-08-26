import asyncio
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import caldav
import vobject

from config import settings


logger = logging.getLogger("IntervalCalendarSync")


def yandex_interval_uid(event_id: int) -> str:
    return f"planiruy-event-{event_id}@planwithai.ru"

async def sync_yandex_interval(
    title: str,
    start_at: datetime,
    end_at: datetime,
    description: str = "",
    event_id: int | None = None,
    all_day: bool = False,
    timezone_name: str = "Europe/Moscow",
) -> None:
    """Best-effort CalDAV sync that never breaks Telegram confirmation."""
    if not settings.YANDEX_EMAIL or not settings.YANDEX_APP_PASSWORD:
        return

    def save() -> None:
        client = caldav.DAVClient(
            url="https://caldav.yandex.ru",
            username=settings.YANDEX_EMAIL,
            password=settings.YANDEX_APP_PASSWORD,
        )
        calendars = client.principal().calendars()
        if not calendars:
            raise ValueError("No Yandex calendars found")
        calendar_data = vobject.iCalendar()
        item = calendar_data.add("vevent")
        item.add("summary").value = title
        if all_day:
            zone = ZoneInfo(timezone_name)
            item.add("dtstart").value = start_at.astimezone(zone).date()
            item.add("dtend").value = end_at.astimezone(zone).date()
        else:
            item.add("dtstart").value = start_at
            item.add("dtend").value = end_at
        item.add("description").value = description or ""
        item.add("uid").value = (
            yandex_interval_uid(event_id) if event_id is not None else str(uuid.uuid4())
        )
        calendars[0].save_event(ical=calendar_data.serialize())

    try:
        await asyncio.to_thread(save)
    except Exception as error:
        logger.warning("Yandex Calendar sync failed for %r: %s", title, error, exc_info=True)


async def delete_yandex_interval(event_id: int) -> None:
    """Delete only the Yandex event carrying the deterministic Planiruy UID."""
    if not settings.YANDEX_EMAIL or not settings.YANDEX_APP_PASSWORD:
        return

    uid = yandex_interval_uid(event_id)

    def delete() -> None:
        client = caldav.DAVClient(
            url="https://caldav.yandex.ru",
            username=settings.YANDEX_EMAIL,
            password=settings.YANDEX_APP_PASSWORD,
        )
        for calendar in client.principal().calendars():
            try:
                resource = calendar.get_event_by_uid(uid)
            except caldav.error.NotFoundError:
                continue
            resource.delete()
            return
        logger.info("Yandex Calendar event %s was not found", uid)

    try:
        await asyncio.to_thread(delete)
    except Exception as error:
        logger.warning(
            "Yandex Calendar deletion failed for event %s: %s",
            event_id,
            error,
            exc_info=True,
        )
