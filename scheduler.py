import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from interval_models import EventTiming
from models import Event, EventStatus, Reminder, ReminderStatus, User
from reminder_service import delivery_state, reminder_preference
from reminder_summary import daily_summary_text

logger = logging.getLogger("Scheduler")


async def check_and_send_reminders(bot: Bot, session_factory) -> None:
    """
    Checks for pending reminders in the database whose scheduled time has passed,
    sends push notifications to users via Telegram bot, and updates the reminder status.
    """
    logger.info("Executing scheduled push reminders check...")
    now_utc = datetime.now(timezone.utc)
    
    async with session_factory() as session:
        try:
            expired = (await session.execute(
                select(Reminder)
                .join(Reminder.event)
                .outerjoin(EventTiming, EventTiming.event_id == Event.id)
                .filter(
                    Reminder.status == ReminderStatus.PENDING,
                    or_(
                        Event.is_completed.is_(True),
                        EventTiming.start_at <= now_utc,
                        and_(EventTiming.event_id.is_(None), Event.deadline <= now_utc),
                    ),
                )
            )).scalars().all()
            for reminder in expired:
                reminder.status = ReminderStatus.SENT
            # Query pending reminders that are due
            stmt = (
                select(Reminder, EventTiming)
                .join(Reminder.event)
                .outerjoin(EventTiming, EventTiming.event_id == Event.id)
                .filter(
                    Reminder.status == ReminderStatus.PENDING,
                    Reminder.remind_at <= now_utc,
                    Event.is_completed == False,
                    Event.status == EventStatus.CONFIRMED,
                )
                .options(
                    joinedload(Reminder.event).joinedload(Event.user)
                )
            )
            
            result = await session.execute(stmt)
            reminders = result.all()
            
            if not reminders:
                if expired:
                    await session.commit()
                logger.info("No pending reminders due for delivery.")
                return
                
            logger.info(f"Processing {len(reminders)} due reminder(s)...")
            
            for reminder, timing in reminders:
                event = reminder.event
                user = event.user
                start_at = timing.start_at if timing else event.deadline
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                state = await delivery_state(
                    session, "telegram", event.id, user.tg_id
                )
                preference = await reminder_preference(
                    session, "telegram", user.tg_id
                )
                if start_at <= now_utc or not preference.enabled:
                    reminder.status = ReminderStatus.SENT
                    state.awaiting_action = False
                    continue
                if state.awaiting_action:
                    continue

                if timing and bool(getattr(timing, "all_day", False)):
                    zone = ZoneInfo(user.timezone)
                    local_start = start_at.astimezone(zone)
                    local_end = timing.end_at.astimezone(zone) - timedelta(days=1)
                    date_label = f"{local_start:%d.%m.%Y}" if local_start.date() == local_end.date() else f"{local_start:%d.%m}–{local_end:%d.%m.%Y}"
                    message_text = f"🔔 **Напоминание**\n📌 {event.title}\n📅 Весь день: {date_label}"
                else:
                    message_text = (
                        f"🔔 **Напоминание**\n"
                        f"📌 {event.title}\n"
                        f"⏰ Начало: {start_at.astimezone(ZoneInfo(user.timezone)).strftime('%d.%m.%Y в %H:%M')}"
                    )
                
                # Create completion button
                builder = InlineKeyboardBuilder()
                builder.button(text="Понятно", callback_data=f"reminder_ack:t:{event.id}")
                builder.button(
                    text=f"⏰ Через {preference.snooze_minutes} мин",
                    callback_data=f"reminder_snooze:t:{event.id}",
                )
                builder.button(text="✅ Выполнено", callback_data=f"complete_event:{event.id}")
                builder.adjust(2, 1)
                
                try:
                    await bot.send_message(
                        chat_id=user.tg_id,
                        text=message_text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
                    logger.info(f"Successfully sent reminder ID={reminder.id} to user tg_id={user.tg_id}.")
                    reminder.status = ReminderStatus.SENT
                    state.awaiting_action = True
                    state.reminder_id = reminder.id
                    state.sent_at = now_utc
                except TelegramForbiddenError:
                    logger.warning(
                        f"User tg_id={user.tg_id} has blocked the bot. "
                        f"Marking reminder ID={reminder.id} as failed."
                    )
                    reminder.status = ReminderStatus.FAILED
                except Exception as send_err:
                    logger.error(
                        f"General error sending reminder ID={reminder.id} to user tg_id={user.tg_id}: {send_err}"
                    )
                    reminder.status = ReminderStatus.FAILED
            
            await session.commit()
            logger.info("Successfully updated all delivered/failed reminder states in the database.")
            
        except Exception as err:
            logger.error(f"Failed to check or process push reminders: {err}", exc_info=True)
            await session.rollback()


async def send_daily_summaries(bot: Bot, session_factory) -> None:
    now_utc = datetime.now(timezone.utc)
    async with session_factory() as session:
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            preference = await reminder_preference(
                session, "telegram", user.tg_id
            )
            local_now = now_utc.astimezone(ZoneInfo(user.timezone))
            if (
                not preference.daily_summary
                or preference.notification_platform != "telegram"
                or local_now.hour < preference.summary_hour
                or preference.last_summary_date == local_now.date()
            ):
                continue
            try:
                text = await daily_summary_text(
                    session, "telegram", user.tg_id, user.timezone, now_utc
                )
                builder = InlineKeyboardBuilder()
                builder.button(
                    text="📅 Открыть календарь", url=f"{settings.APP_URL}/calendar"
                )
                await bot.send_message(
                    chat_id=user.tg_id, text=text, parse_mode="Markdown",
                    reply_markup=builder.as_markup(),
                )
                preference.last_summary_date = local_now.date()
                await session.commit()
            except Exception:
                logger.exception("Could not send daily summary to tg_id=%s", user.tg_id)
                await session.rollback()


def setup_scheduler(bot: Bot, session_factory) -> AsyncIOScheduler:
    """
    Initializes and starts the AsyncIOScheduler to check and send reminders
    periodically every 1 minute.
    """
    logger.info("Setting up AsyncIOScheduler for push reminders...")
    scheduler = AsyncIOScheduler()
    
    # Run the reminder task every 1 minute
    scheduler.add_job(
        check_and_send_reminders,
        "interval",
        minutes=1,
        args=[bot, session_factory],
        id="check_and_send_reminders",
        replace_existing=True
    )
    scheduler.add_job(
        send_daily_summaries,
        "interval",
        minutes=5,
        args=[bot, session_factory],
        id="daily_summaries",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("AsyncIOScheduler started successfully in background.")
    return scheduler
