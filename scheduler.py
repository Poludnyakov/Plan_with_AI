import logging
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import Reminder, ReminderStatus, Event

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
            # Query pending reminders that are due
            stmt = (
                select(Reminder)
                .join(Reminder.event)
                .filter(
                    Reminder.status == ReminderStatus.PENDING,
                    Reminder.remind_at <= now_utc,
                    Event.is_completed == False
                )
                .options(
                    joinedload(Reminder.event).joinedload(Event.user)
                )
            )
            
            result = await session.execute(stmt)
            reminders = result.scalars().all()
            
            if not reminders:
                logger.info("No pending reminders due for delivery.")
                return
                
            logger.info(f"Processing {len(reminders)} due reminder(s)...")
            
            for reminder in reminders:
                event = reminder.event
                user = event.user
                
                # Format push notification message
                message_text = (
                    f"🔔 **Напоминание о дедлайне!**\n"
                    f"📌 **Задача:** {event.title}\n"
                    f"⏰ **Срок:** {event.deadline.strftime('%d.%m.%Y в %H:%M')}"
                )
                
                # Create completion button
                builder = InlineKeyboardBuilder()
                builder.button(text="✅ Завершить задачу", callback_data=f"complete_event:{event.id}")
                
                try:
                    await bot.send_message(
                        chat_id=user.tg_id,
                        text=message_text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
                    logger.info(f"Successfully sent reminder ID={reminder.id} to user tg_id={user.tg_id}.")
                    reminder.status = ReminderStatus.SENT
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
    
    scheduler.start()
    logger.info("AsyncIOScheduler started successfully in background.")
    return scheduler
