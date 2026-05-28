import asyncio
import logging
import uuid
from datetime import datetime, timedelta
import caldav
import vobject

from config import settings

logger = logging.getLogger("YandexCalendarService")

class YandexCalendarService:
    """
    Coordinates interactions with the Yandex Calendar via CalDAV.
    All synchronous CalDAV/vobject network and serialization calls are executed
    in background worker threads to prevent blocking the async event loop.
    """
    async def add_deadline_to_yandex(self, title: str, deadline: datetime, description: str = None) -> None:
        """
        Asynchronously schedules an event in the user's primary Yandex Calendar using CalDAV.
        Runs connection and save operations in a background thread via asyncio.to_thread.
        """
        if not settings.YANDEX_EMAIL or not settings.YANDEX_APP_PASSWORD:
            logger.warning("YANDEX_EMAIL or YANDEX_APP_PASSWORD is not set. Skipping Yandex Calendar sync.")
            return

        if not deadline:
            logger.warning(f"Event '{title}' has no deadline. Skipping Yandex Calendar sync.")
            return

        # Prefix the title with the required emoji
        event_title = f"📝 ДЕДЛАЙН: {title}"

        def sync_save():
            nonlocal deadline
            logger.info(f"Connecting to Yandex CalDAV for user: {settings.YANDEX_EMAIL}")
            
            # Handle timezone-aware datetimes for compatibility with vobject serialization
            if deadline.tzinfo is not None:
                import pytz
                deadline = deadline.astimezone(pytz.utc)

            client = caldav.DAVClient(
                url="https://caldav.yandex.ru",
                username=settings.YANDEX_EMAIL,
                password=settings.YANDEX_APP_PASSWORD
            )
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                raise ValueError("No calendars found in Yandex account!")
            
            calendar = calendars[0]
            logger.info(f"Using Yandex Calendar: {calendar.name if hasattr(calendar, 'name') else 'Primary'} (URL: {calendar.url})")

            # Create iCalendar structure using vobject
            cal = vobject.iCalendar()
            event = cal.add('vevent')
            event.add('summary').value = event_title
            
            # Make event start at deadline and end after 30 minutes
            event.add('dtstart').value = deadline
            event.add('dtend').value = deadline + timedelta(minutes=30)
            event.add('description').value = description or ''
            event.add('uid').value = str(uuid.uuid4())

            serialized_ics = cal.serialize()
            logger.info(f"Saving event to Yandex Calendar: '{event_title}' at {deadline}")
            calendar.save_event(ical=serialized_ics)
            logger.info("Successfully saved event to Yandex Calendar.")

        try:
            await asyncio.to_thread(sync_save)
        except Exception as e:
            logger.error(f"Error synchronizing event '{title}' to Yandex Calendar: {e}", exc_info=True)
            raise
