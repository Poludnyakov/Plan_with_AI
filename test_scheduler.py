import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

from database import Base
from models import User, Event, Reminder, ReminderStatus, EventStatus
from scheduler import check_and_send_reminders, setup_scheduler
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError


@pytest.fixture(scope="function")
async def test_db():
    """
    Sets up an in-memory SQLite database for testing scheduler database operations.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with async_session() as session:
        yield session
        
    await engine.dispose()


@pytest.mark.anyio
async def test_check_and_send_reminders_success(test_db):
    """
    Tests that check_and_send_reminders correctly fetches due pending reminders,
    sends them via bot, and updates their status to 'sent' in the DB.
    """
    # 1. Populate DB with test User, Event, and Reminder
    db_user = User(tg_id=12345, timezone="Europe/Moscow")
    test_db.add(db_user)
    await test_db.flush()
    
    db_event = Event(
        user_id=db_user.id,
        title="Лекция по физике",
        description="Ауд. 301",
        deadline=datetime.now(timezone.utc) + timedelta(days=1),
        status=EventStatus.CONFIRMED
    )
    test_db.add(db_event)
    await test_db.flush()
    
    # Reminder (due 5 minutes ago)
    due_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_reminder = Reminder(
        event_id=db_event.id,
        remind_at=due_time,
        status=ReminderStatus.PENDING
    )
    test_db.add(db_reminder)
    await test_db.commit()
    
    # 2. Mock Bot
    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()
    
    # Context manager mock returning our in-memory SQLite session
    class MockSessionFactory:
        def __init__(self, db):
            self.db = db
        async def __aenter__(self):
            return self.db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def __call__(self):
            return self
            
    mock_factory = MockSessionFactory(test_db)
    
    # 3. Execute
    await check_and_send_reminders(mock_bot, mock_factory)
    
    # 4. Verify message sending
    mock_bot.send_message.assert_called_once()
    args, kwargs = mock_bot.send_message.call_args
    assert kwargs["chat_id"] == 12345
    assert "Напоминание о дедлайне!" in kwargs["text"]
    assert "Лекция по физике" in kwargs["text"]
    assert kwargs["reply_markup"] is not None
    
    # Verify inline keyboard details
    inline_keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(inline_keyboard) == 1
    assert len(inline_keyboard[0]) == 1
    button = inline_keyboard[0][0]
    assert button.text == "✅ Завершить задачу"
    assert button.callback_data == f"complete_event:{db_event.id}"
    
    # 5. Verify database state is updated to 'sent'
    result = await test_db.execute(select(Reminder).filter(Reminder.id == db_reminder.id))
    updated_reminder = result.scalar_one()
    assert updated_reminder.status == ReminderStatus.SENT


@pytest.mark.anyio
async def test_check_and_send_reminders_completed_ignored(test_db):
    """
    Tests that check_and_send_reminders ignores pending reminders if the
    associated event is marked as completed (is_completed = True).
    """
    db_user = User(tg_id=12345, timezone="Europe/Moscow")
    test_db.add(db_user)
    await test_db.flush()
    
    # Completed event
    db_event = Event(
        user_id=db_user.id,
        title="Завершенная задача",
        description="",
        deadline=datetime.now(timezone.utc) + timedelta(days=1),
        status=EventStatus.CONFIRMED,
        is_completed=True
    )
    test_db.add(db_event)
    await test_db.flush()
    
    due_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_reminder = Reminder(
        event_id=db_event.id,
        remind_at=due_time,
        status=ReminderStatus.PENDING
    )
    test_db.add(db_reminder)
    await test_db.commit()
    
    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()
    
    class MockSessionFactory:
        def __init__(self, db):
            self.db = db
        async def __aenter__(self):
            return self.db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def __call__(self):
            return self
            
    mock_factory = MockSessionFactory(test_db)
    
    await check_and_send_reminders(mock_bot, mock_factory)
    
    # The bot should not send any message
    mock_bot.send_message.assert_not_called()
    
    # Database status remains PENDING
    result = await test_db.execute(select(Reminder).filter(Reminder.id == db_reminder.id))
    updated_reminder = result.scalar_one()
    assert updated_reminder.status == ReminderStatus.PENDING


@pytest.mark.anyio
async def test_check_and_send_reminders_blocked(test_db):
    """
    Tests that if a user has blocked the bot (TelegramForbiddenError),
    the reminder's status is updated to 'failed'.
    """
    db_user = User(tg_id=67890, timezone="Europe/Moscow")
    test_db.add(db_user)
    await test_db.flush()
    
    db_event = Event(
        user_id=db_user.id,
        title="Сдача лабораторной",
        description="",
        deadline=datetime.now(timezone.utc) + timedelta(days=2),
        status=EventStatus.CONFIRMED
    )
    test_db.add(db_event)
    await test_db.flush()
    
    due_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_reminder = Reminder(
        event_id=db_event.id,
        remind_at=due_time,
        status=ReminderStatus.PENDING
    )
    test_db.add(db_reminder)
    await test_db.commit()
    
    mock_bot = MagicMock(spec=Bot)
    # Simulate blocked bot exception
    mock_bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(message="Forbidden", method=MagicMock())
    )
    
    class MockSessionFactory:
        def __init__(self, db):
            self.db = db
        async def __aenter__(self):
            return self.db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def __call__(self):
            return self
            
    mock_factory = MockSessionFactory(test_db)
    
    await check_and_send_reminders(mock_bot, mock_factory)
    
    # Verify status is failed
    result = await test_db.execute(select(Reminder).filter(Reminder.id == db_reminder.id))
    updated_reminder = result.scalar_one()
    assert updated_reminder.status == ReminderStatus.FAILED


@pytest.mark.anyio
async def test_setup_scheduler():
    """
    Tests setup_scheduler successfully registers the check task and starts the scheduler.
    """
    mock_bot = MagicMock(spec=Bot)
    mock_factory = MagicMock()
    
    with patch("apscheduler.schedulers.asyncio.AsyncIOScheduler.start") as mock_start, \
         patch("apscheduler.schedulers.asyncio.AsyncIOScheduler.add_job") as mock_add_job:
         
        scheduler = setup_scheduler(mock_bot, mock_factory)
        
        assert scheduler is not None
        mock_start.assert_called_once()
        mock_add_job.assert_called_once()
        
        # Verify job parameters
        args, kwargs = mock_add_job.call_args
        assert args[0] == check_and_send_reminders
        assert args[1] == "interval"
        assert kwargs["minutes"] == 1
        assert kwargs["args"] == [mock_bot, mock_factory]
