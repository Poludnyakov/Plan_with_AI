from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from account_service import consume_link_code, create_link_code
from database import Base
from models import Reminder
from max_bot.models import MaxReminder
from unified_calendar import (
    create_linked_event,
    list_linked_events,
    payload,
    update_linked_event,
)


@pytest.mark.anyio
async def test_linked_accounts_share_edit_and_conflict_across_platforms():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=4)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            code = await create_link_code(db, "telegram", 10101)
            await consume_link_code(db, "max", 20202, code)
            telegram_event = await create_linked_event(
                db, "telegram", 10101, "Контрольная", "",
                start, start + timedelta(hours=1),
            )
            from_max = await list_linked_events(db, "max", 20202)
            assert [item.ref for item in from_max] == [telegram_event.ref]

            moved = await update_linked_event(
                db, "max", 20202, telegram_event.ref,
                start + timedelta(hours=1), start + timedelta(hours=2),
            )
            assert moved.ref.startswith("t:")
            assert moved.timing.start_at.replace(tzinfo=timezone.utc) == start + timedelta(hours=1)

            with pytest.raises(ValueError, match="пересекается"):
                await create_linked_event(
                    db, "max", 20202, "Встреча", "",
                    start + timedelta(hours=1, minutes=30),
                    start + timedelta(hours=2, minutes=30),
                )

            max_event = await create_linked_event(
                db, "max", 20202, "Лекция", "",
                start + timedelta(hours=3), start + timedelta(hours=4),
            )
            from_telegram = await list_linked_events(db, "telegram", 10101)
            assert {item.ref for item in from_telegram} == {moved.ref, max_event.ref}
    await engine.dispose()


@pytest.mark.anyio
async def test_manual_reminders_are_returned_and_can_be_removed():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)
    requested = [start - timedelta(hours=1), start - timedelta(minutes=15)]
    with patch("unified_calendar._sync"):
        async with sessions() as db:
            event = await create_linked_event(
                db, "telegram", 30303, "Экзамен", "",
                start, start + timedelta(hours=2), requested,
            )
            assert event.reminders == requested
            assert payload(event)["reminders"] == [value.isoformat() for value in requested]

            listed = await list_linked_events(db, "telegram", 30303)
            assert listed[0].reminders == requested

            updated = await update_linked_event(
                db, "telegram", 30303, event.ref,
                start, start + timedelta(hours=2), reminder_times=[],
            )
            assert updated.reminders == []
            assert (await db.execute(select(Reminder))).scalars().all() == []

            max_start = start + timedelta(hours=5)
            max_requested = [max_start - timedelta(days=1)]
            max_event = await create_linked_event(
                db, "max", 40404, "Встреча", "",
                max_start, max_start + timedelta(hours=1), max_requested,
            )
            assert max_event.reminders == max_requested
            assert (await db.execute(select(MaxReminder))).scalars().one().remind_at is not None

            await update_linked_event(
                db, "max", 40404, max_event.ref,
                max_start, max_start + timedelta(hours=1), reminder_times=[],
            )
            assert (await db.execute(select(MaxReminder))).scalars().all() == []
    await engine.dispose()


@pytest.mark.anyio
async def test_all_day_events_can_overlap_timed_events_and_are_exposed_in_payload():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=5)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            timed = await create_linked_event(
                db, "telegram", 50505, "Встреча", "", start + timedelta(hours=10),
                start + timedelta(hours=11),
            )
            all_day = await create_linked_event(
                db, "telegram", 50505, "Поездка", "", start,
                start + timedelta(days=3), all_day=True,
            )
            assert timed.ref != all_day.ref
            assert all_day.timing.all_day is True
            assert payload(all_day)["all_day"] is True
    await engine.dispose()
