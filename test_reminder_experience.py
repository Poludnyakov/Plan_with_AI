from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from account_service import consume_link_code, create_link_code
from database import Base
from interval_models import EventTiming
from max_bot.models import MaxEvent, MaxEventTiming, MaxReminder, MaxUser
from max_bot.scheduler import send_daily_summaries as send_max_daily_summaries
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



def moscow_midnight(days_ahead: int) -> datetime:
    zone = ZoneInfo("Europe/Moscow")
    day = datetime.now(zone).date() + timedelta(days=days_ahead)
    return datetime(day.year, day.month, day.day, tzinfo=zone)


@pytest.mark.anyio
async def test_all_day_default_reminder_is_the_evening_before(reminder_db):
    start = moscow_midnight(4)
    values = await build_user_reminders(
        reminder_db, "telegram", 313, "Собрать чемоданы", "",
        start, start + timedelta(days=1), "Europe/Moscow", all_day=True,
    )
    # Not at midnight: 19:00 the day before.
    assert values == [start - timedelta(hours=5)]


def test_all_day_reminder_for_tomorrow_after_19_is_the_morning_of_the_day():
    from reminder_service import all_day_reminder

    start = moscow_midnight(1)
    late_evening = start - timedelta(hours=3)  # 21:00 today
    assert all_day_reminder(start, start + timedelta(days=1), "Europe/Moscow", late_evening) == [
        start + timedelta(hours=9)
    ]


@pytest.mark.anyio
async def test_all_day_event_with_a_morning_reminder_can_be_saved():
    """Created after 19:00 for tomorrow: the 09:00 reminder is within the day."""
    from unified_calendar import create_linked_event

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = moscow_midnight(1)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(User(tg_id=4242, timezone="Europe/Moscow"))
        await db.commit()
        with patch("unified_calendar._sync"):
            entry = await create_linked_event(
                db, "telegram", 4242, "Выходной", "", start, start + timedelta(days=1),
                [start + timedelta(hours=9)], True,
            )
        assert entry.timing.all_day
    await engine.dispose()


@pytest.mark.anyio
async def test_all_day_ai_keeps_day_before_and_adds_ai_suggestion(reminder_db):
    start = moscow_midnight(5)
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
    assert values == [start - timedelta(hours=5), ai_suggestion]

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
    local_now = now.astimezone(ZoneInfo("Europe/Moscow"))
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    event = Event(
        user_id=user.id, title="Пара", deadline=start + timedelta(days=1),
        status=EventStatus.CONFIRMED,
    )
    reminder_db.add(event)
    await reminder_db.flush()
    reminder_db.add(EventTiming(
        event_id=event.id, start_at=start, end_at=event.deadline, all_day=True,
    ))
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
    buttons = [button for row in keyboard for button in row]
    calendar = next(button for button in buttons if button.text == "📅 Открыть календарь")
    # The Mini App, not the site in a browser.
    assert calendar.web_app and calendar.web_app.url.endswith("/miniapp?destination=calendar") and not calendar.url
    assert any(button.callback_data == "summary_switch:week" for button in buttons)


@pytest.mark.anyio
async def test_daily_summary_reaches_next_user_after_a_failed_send(reminder_db):
    # A failed send rolls the session back, which expires loaded ORM objects;
    # the next user must not trigger a lazy reload (MissingGreenlet).
    for tg_id in (801, 802):
        reminder_db.add(User(tg_id=tg_id, timezone="Europe/Moscow"))
    await reminder_db.flush()
    for tg_id in (801, 802):
        preference = await reminder_preference(reminder_db, "telegram", tg_id)
        preference.summary_hour = 0
        preference.notification_platform = "telegram"
    await reminder_db.commit()
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[RuntimeError("blocked"), None])

    await send_daily_summaries(bot, SessionFactory(reminder_db))

    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [801, 802]


@pytest.mark.anyio
async def test_max_daily_summary_reaches_next_user_after_a_failed_send(reminder_db):
    for max_user_id in (901, 902):
        reminder_db.add(MaxUser(max_user_id=max_user_id, timezone="Europe/Moscow"))
    await reminder_db.flush()
    for max_user_id in (901, 902):
        preference = await reminder_preference(reminder_db, "max", max_user_id)
        preference.summary_hour = 0
        preference.notification_platform = "max"
    await reminder_db.commit()
    client = MagicMock()
    client.send_message = AsyncMock(side_effect=[RuntimeError("blocked"), None])

    await send_max_daily_summaries(client, SessionFactory(reminder_db))

    assert [call.kwargs["user_id"] for call in client.send_message.await_args_list] == [901, 902]
