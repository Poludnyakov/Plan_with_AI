import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .api import MaxApiClient, callback_button
from .models import MaxEvent, MaxReminder


logger = logging.getLogger("MaxScheduler")


async def send_due_reminders(client: MaxApiClient, session_factory) -> None:
    async with session_factory() as db:
        try:
            result = await db.execute(
                select(MaxReminder)
                .join(MaxReminder.event)
                .filter(
                    MaxReminder.status == "pending",
                    MaxReminder.remind_at <= datetime.now(timezone.utc),
                    MaxEvent.is_completed.is_(False),
                    MaxEvent.status == "confirmed",
                )
                .options(joinedload(MaxReminder.event).joinedload(MaxEvent.user))
            )
            for reminder in result.scalars().all():
                event = reminder.event
                try:
                    await client.send_message(
                        f"🔔 **Напоминание**\n📌 {event.title}\n⏰ {event.deadline:%d.%m.%Y в %H:%M}",
                        user_id=event.user.max_user_id,
                        buttons=[[callback_button("✅ Завершить", f"complete:{event.id}")]],
                    )
                    reminder.status = "sent"
                except Exception:
                    logger.exception("Could not send MAX reminder %s", reminder.id)
                    reminder.status = "failed"
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("MAX reminder cycle failed")


def setup_scheduler(client: MaxApiClient, session_factory) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_due_reminders,
        "interval",
        minutes=1,
        args=[client, session_factory],
        id="max_due_reminders",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler

