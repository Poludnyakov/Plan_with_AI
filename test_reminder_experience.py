from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from account_service import consume_link_code, create_link_code
from database import Base
from interval_models import EventTiming
from max_bot.models import MaxEvent, MaxEventTiming, MaxReminder, MaxUser
from max_bot.scheduler import send_due_reminders as send_max_reminders
from models import Event, EventStatus, Reminder, ReminderStatus, User
from reminder_models import ReminderDeliveryState
from reminder_service import (
    acknowledge_delivery,
    build_user_reminders,
    reminder_preference,
    snooze_delivery,
    update_reminder_preference,
)
from scheduler import check_and_send_reminders, send_daily_summaries


class SessionFactory:
    def __init__(self, session: AsyncSession):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_):
        return None


@pytest.fixture
async def reminder_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        yield db
    await engine.dispose()


@pytest.mark.anyio
async def test_preferences_are_per_linked_account_and_ai_is_off_by_default(reminder_db):
    code = await create_link_code(reminder_db, "telegram", 101)
    await consume_link_code(reminder_db, "max", 202, code)
    telegram = await reminder_preference(reminder_db, "telegram", 101)
    assert telegram.frequency == "minimal"
    assert telegram.use_ai is False
    await update_reminder_preference(reminder_db, "telegram", 101, "frequency")
    from_max = await reminder_preference(reminder_db, "max", 202)
    assert from_max.account_id == telegram.account_id
    assert from_max.frequency == "balanced"


@pytest.mark.anyio
async def test_existing_target_preferences_are_copied_when_accounts_link(reminder_db):
    target = await reminder_preference(reminder_db, "max", 212)
    target.frequency = "frequent"
    target.summary_hour = 9
    await reminder_db.commit()
    code = await create_link_code(reminder_db, "telegram", 111)

    await consume_link_code(reminder_db, "max", 212, code)

    linked = await reminder_preference(reminder_db, "telegram", 111)
    assert linked.frequency == "frequent"
    assert linked.summary_hour == 9


@pytest.mark.anyio
async def test_default_reminder_policy_does_not_call_paid_ai(reminder_db):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    with patch("reminder_service.recommend_reminders", new_callable=AsyncMock) as paid:
        values = await build_user_reminders(
            reminder_db, "telegram", 303, "Экзамен", "",
            start, start + timedelta(hours=2), "Europe/Moscow",
        )
    paid.assert_not_awaited()
    assert len(values) == 1
    assert values[0] < start



@pytest.mark.anyio
async def test_all_day_default_reminder_is_one_day_before(reminder_db):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=4)
    values = await build_user_reminders(
        reminder_db, "telegram", 313, "Собрать чемоданы", "",
        start, start + timedelta(days=1), "Europe/Moscow", all_day=True,
    )
    assert values == [start - timedelta(days=1)]


@pytest.mark.anyio
async def test_all_day_ai_keeps_day_before_and_adds_ai_suggestion(reminder_db):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=5)
    preference = await reminder_preference(reminder_db, "telegram", 314)
    preference.use_ai = True
    await reminder_db.commit()
    ai_suggestion = start - timedelta(hours=2)
    with patch("reminder_service.recommend_reminders", new_callable=AsyncMock, return_value=[ai_suggestion]) as paid:
        values = await build_user_reminders(
            reminder_db, "telegram", 314, "Поездка", "",
            start, start + timedelta(days=1), "Europe/Moscow", all_day=True,
        )
    paid.assert_awaited_once()
    assert values == [start - timedelta(days=1), ai_suggestion]

@pytest.mark.anyio
async def test_telegram_waits_for_reaction_and_ack_collapses_overdue_queue(reminder_db):
    user = User(tg_id=404, timezone="Europe/Moscow")
    reminder_db.add(user)
    await reminder_db.flush()
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    event = Event(
        user_id=user.id, title="Контрольная", description="", deadline=start + timedelta(hours=1),
        status=EventStatus.CONFIRMED,
    )
    reminder_db.add(event)
    await reminder_db.flush()
    reminder_db.add(EventTiming(event_id=event.id, start_at=start, end_at=event.deadline))
    for minutes in (10, 5):
        reminder_db.add(Reminder(
            event_id=event.id,
            remind_at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
            status=ReminderStatus.PENDING,
        ))
    await reminder_db.commit()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    factory = SessionFactory(reminder_db)

    await check_and_send_reminders(bot, factory)
    await check_and_send_reminders(bot, factory)

    assert bot.send_message.await_count == 1
    state = await reminder_db.get(ReminderDeliveryState, ("telegram", event.id))
    assert state.awaiting_action is True
    await acknowledge_delivery(reminder_db, "telegram", event.id, user.tg_id)
    pending = (await reminder_db.execute(select(Reminder).filter(
        Reminder.event_id == event.id,
        Reminder.status == ReminderStatus.PENDING,
    ))).scalars().all()
    assert pending == []


@pytest.mark.anyio
async def test_snooze_creates_one_later_reminder(reminder_db):
    user = User(tg_id=505, timezone="Europe/Moscow")
    reminder_db.add(user)
    await reminder_db.flush()
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    event = Event(
        user_id=user.id, title="Встреча", deadline=start + timedelta(hours=1),
        status=EventStatus.CONFIRMED,
    )
    reminder_db.add(event)
    await reminder_db.flush()
    reminder_db.add(Reminder(
        event_id=event.id, remind_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        status=ReminderStatus.PENDING,
    ))
    await reminder_db.commit()

    snooze_at, minutes = await snooze_delivery(
        reminder_db, "telegram", event.id, user.tg_id, start
    )

    assert minutes == 30
    pending = (await reminder_db.execute(select(Reminder).filter(
        Reminder.event_id == event.id,
        Reminder.status == ReminderStatus.PENDING,
    ))).scalars().all()
    assert len(pending) == 1
    assert abs((pending[0].remind_at.replace(tzinfo=timezone.utc) - snooze_at).total_seconds()) < 1


@pytest.mark.anyio
async def test_max_also_blocks_second_notification_until_reaction(reminder_db):
    user = MaxUser(max_user_id=606, timezone="Europe/Moscow")
    reminder_db.add(user)
    await reminder_db.flush()
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    event = MaxEvent(
        user_id=user.id, title="Защита", deadline=start + timedelta(hours=1), status="confirmed"
    )
    reminder_db.add(event)
    await reminder_db.flush()
    reminder_db.add(MaxEventTiming(event_id=event.id, start_at=start, end_at=event.deadline))
    reminder_db.add_all([
        MaxReminder(event_id=event.id, remind_at=datetime.now(timezone.utc) - timedelta(minutes=2)),
        MaxReminder(event_id=event.id, remind_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
    ])
    await reminder_db.commit()
    client = SimpleNamespace(send_message=AsyncMock())
    factory = SessionFactory(reminder_db)

    await send_max_reminders(client, factory)
    await send_max_reminders(client, factory)

    assert client.send_message.await_count == 1
    assert client.send_message.await_args.kwargs["buttons"][0][0]["payload"].startswith("reminder_ack:m:")


@pytest.mark.anyio
async def test_daily_summary_is_sent_once_with_calendar_button(reminder_db):
    user = User(tg_id=707, timezone="Europe/Moscow")
    reminder_db.add(user)
    await reminder_db.flush()
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=2)
    event = Event(
        user_id=user.id, title="Пара", deadline=start + timedelta(hours=1),
        status=EventStatus.CONFIRMED,
    )
    reminder_db.add(event)
    await reminder_db.flush()
    reminder_db.add(EventTiming(event_id=event.id, start_at=start, end_at=event.deadline))
    preference = await reminder_preference(reminder_db, "telegram", user.tg_id)
    preference.summary_hour = 0
    preference.notification_platform = "telegram"
    await reminder_db.commit()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    factory = SessionFactory(reminder_db)

    await send_daily_summaries(bot, factory)
    await send_daily_summaries(bot, factory)

    assert bot.send_message.await_count == 1
    assert "План на сегодня" in bot.send_message.await_args.kwargs["text"]
    keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert keyboard[0][0].text == "📅 Открыть календарь"
