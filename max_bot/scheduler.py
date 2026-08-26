import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload

from reminder_service import delivery_state, reminder_preference
from reminder_summary import daily_summary_text

from .api import MaxApiClient, callback_button, open_app_button
from .config import settings
from .models import MaxEvent, MaxEventTiming, MaxReminder, MaxUser


logger = logging.getLogger("MaxScheduler")


async def send_due_reminders(client: MaxApiClient, session_factory) -> None:
    async with session_factory() as db:
        try:
            now_utc = datetime.now(timezone.utc)
            expired = (await db.execute(
                select(MaxReminder)
                .join(MaxReminder.event)
                .outerjoin(MaxEventTiming, MaxEventTiming.event_id == MaxEvent.id)
                .filter(
                    MaxReminder.status == "pending",
                    or_(
                        MaxEvent.is_completed.is_(True),
                        MaxEventTiming.start_at <= now_utc,
                        and_(
                            MaxEventTiming.event_id.is_(None),
                            MaxEvent.deadline <= now_utc,
                        ),
                    ),
                )
            )).scalars().all()
            for reminder in expired:
                reminder.status = "sent"
            result = await db.execute(
                select(MaxReminder, MaxEventTiming)
                .join(MaxReminder.event)
                .outerjoin(MaxEventTiming, MaxEventTiming.event_id == MaxEvent.id)
                .filter(
                    MaxReminder.status == "pending",
                    MaxReminder.remind_at <= datetime.now(timezone.utc),
                    MaxEvent.is_completed.is_(False),
                    MaxEvent.status == "confirmed",
                )
                .options(joinedload(MaxReminder.event).joinedload(MaxEvent.user))
            )
            for reminder, timing in result.all():
                event = reminder.event
                start_at = timing.start_at if timing else event.deadline
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                state = await delivery_state(
                    db, "max", event.id, event.user.max_user_id
                )
                preference = await reminder_preference(
                    db, "max", event.user.max_user_id
                )
                if start_at <= now_utc or not preference.enabled:
                    reminder.status = "sent"
                    state.awaiting_action = False
                    continue
                if state.awaiting_action:
                    continue
                if timing and bool(getattr(timing, "all_day", False)):
                    zone = ZoneInfo(event.user.timezone)
                    local_start = start_at.astimezone(zone)
                    local_end = timing.end_at.astimezone(zone) - timedelta(days=1)
                    date_label = f"{local_start:%d.%m.%Y}" if local_start.date() == local_end.date() else f"{local_start:%d.%m}–{local_end:%d.%m.%Y}"
                    message_text = f"🔔 **Напоминание**\n📌 {event.title}\n📅 Весь день: {date_label}"
                else:
                    message_text = (
                        f"🔔 **Напоминание**\n📌 {event.title}\n"
                        f"⏰ Начало: {start_at.astimezone(ZoneInfo(event.user.timezone)):%d.%m.%Y в %H:%M}"
                    )
                try:
                    await client.send_message(
                        message_text,
                        user_id=event.user.max_user_id,
                        buttons=[
                            [
                                callback_button("Понятно", f"reminder_ack:m:{event.id}"),
                                callback_button(
                                    f"⏰ Через {preference.snooze_minutes} мин",
                                    f"reminder_snooze:m:{event.id}",
                                ),
                            ],
                            [callback_button("✅ Выполнено", f"complete:{event.id}")],
                        ],
                    )
                    reminder.status = "sent"
                    state.awaiting_action = True
                    state.reminder_id = reminder.id
                    state.sent_at = now_utc
                except Exception:
                    logger.exception("Could not send MAX reminder %s", reminder.id)
                    reminder.status = "failed"
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("MAX reminder cycle failed")


async def send_daily_summaries(client: MaxApiClient, session_factory) -> None:
    now_utc = datetime.now(timezone.utc)
    async with session_factory() as db:
        users = (await db.execute(select(MaxUser))).scalars().all()
        for user in users:
            preference = await reminder_preference(db, "max", user.max_user_id)
            zone = ZoneInfo(user.timezone)
            local_now = now_utc.astimezone(zone)
            if (
                not preference.daily_summary
                or preference.notification_platform != "max"
                or local_now.hour < preference.summary_hour
                or preference.last_summary_date == local_now.date()
            ):
                continue
            try:
                text = await daily_summary_text(
                    db, "max", user.max_user_id, user.timezone, now_utc
                )
                await client.send_message(
                    text,
                    user_id=user.max_user_id,
                    buttons=[[open_app_button(
                        "📅 Открыть календарь", settings.miniapp_name or None
                    )]],
                )
                preference.last_summary_date = local_now.date()
                await db.commit()
            except Exception:
                logger.exception(
                    "Could not send MAX daily summary to user=%s", user.max_user_id
                )
                await db.rollback()


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
    scheduler.add_job(
        send_daily_summaries,
        "interval",
        minutes=5,
        args=[client, session_factory],
        id="max_daily_summaries",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
