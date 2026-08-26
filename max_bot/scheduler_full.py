import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select

from .models import MaxInboxUpdate
from .scheduler import send_daily_summaries, send_due_reminders


logger = logging.getLogger("MaxFullScheduler")


async def retry_inbox(handler, session_factory) -> None:
    """Retry acknowledged webhook updates that did not finish after the HTTP response."""
    async with session_factory() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = await db.execute(
            select(MaxInboxUpdate).filter(
                MaxInboxUpdate.attempts < 5,
                or_(
                    MaxInboxUpdate.status.in_(["pending", "failed"]),
                    (MaxInboxUpdate.status == "processing") & (MaxInboxUpdate.created_at < cutoff),
                ),
            ).limit(20)
        )
        for record in result.scalars().all():
            record.status = "processing"
            record.attempts += 1
            await db.commit()
            try:
                await handler.handle_update(record.payload, db)
                record.status, record.last_error = "done", None
            except Exception as error:
                await db.rollback()
                record = await db.get(MaxInboxUpdate, record.key)
                record.status, record.last_error = "failed", str(error)[:2000]
                logger.exception("Retry of MAX update %s failed", record.key)
            await db.commit()


def setup_full_scheduler(client, handler, session_factory) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_due_reminders, "interval", minutes=1, args=[client, session_factory],
        id="max_due_reminders", replace_existing=True,
    )
    scheduler.add_job(
        retry_inbox, "interval", minutes=1, args=[handler, session_factory],
        id="max_inbox_retry", replace_existing=True,
    )
    scheduler.add_job(
        send_daily_summaries, "interval", minutes=5,
        args=[client, session_factory], id="max_daily_summaries",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
