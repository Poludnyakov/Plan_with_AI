from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from account_service import consume_link_code, create_link_code
from chat_edit_service import apply_chat_edit
from conversation_service import recent_dialogue_context, remember_dialogue_turn
from database import Base
from unified_calendar import (
    create_linked_event,
    find_linked_all_day_overlaps,
)


@pytest.mark.anyio
async def test_context_is_shared_after_linking_accounts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        await remember_dialogue_turn(
            db, "telegram", 7101, "Контрольная по физике завтра в 13", commit=True
        )
        code = await create_link_code(db, "telegram", 7101)
        await consume_link_code(db, "max", 7202, code)
        assert await recent_dialogue_context(db, "max", 7202) == [
            "user: Контрольная по физике завтра в 13"
        ]
    await engine.dispose()


@pytest.mark.anyio
async def test_all_day_overlap_warns_but_does_not_block_event_creation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=4)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            trip = await create_linked_event(
                db, "telegram", 7303, "Поездка", "", start,
                start + timedelta(days=3), all_day=True,
            )
            lesson = await create_linked_event(
                db, "telegram", 7303, "Лекция", "", start + timedelta(hours=10),
                start + timedelta(hours=11),
            )
            overlaps = await find_linked_all_day_overlaps(
                db, "telegram", 7303, lesson.timing.start_at, lesson.timing.end_at,
                exclude_ref=lesson.ref,
            )
            assert [entry.ref for entry in overlaps] == [trip.ref]
    await engine.dispose()


@pytest.mark.anyio
async def test_contextual_chat_edit_moves_last_confirmed_event():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=5)
    target_start = start + timedelta(days=1, hours=2)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            event = await create_linked_event(
                db, "telegram", 7404, "Контрольная по физике", "",
                start, start + timedelta(hours=1),
            )
            await remember_dialogue_turn(
                db, "telegram", 7404, "Событие: Контрольная по физике",
                role="assistant", event_ref=event.ref, commit=True,
            )
            result = await apply_chat_edit(
                db, "telegram", 7404, "перенеси его завтра в 15:00",
                [{
                    "title": "Контрольная по физике",
                    "start_at": target_start,
                    "end_at": target_start + timedelta(hours=1),
                    "description": "",
                    "all_day": False,
                }],
            )
            assert result.status == "updated"
            assert result.entry is not None
            assert result.entry.ref == event.ref
            assert result.entry.timing.start_at == target_start
    await engine.dispose()


@pytest.mark.anyio
async def test_chat_edit_uses_source_title_when_renaming():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=6)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            event = await create_linked_event(
                db, "telegram", 7505, "Контрольная по русскому", "",
                start, start + timedelta(hours=1),
            )
            result = await apply_chat_edit(
                db, "telegram", 7505,
                "переименуй контрольную по русскому в зачёт",
                [{
                    "title": "Зачёт",
                    "start_at": start,
                    "end_at": start + timedelta(hours=1),
                    "description": "",
                    "all_day": False,
                }],
            )
            assert result.status == "updated"
            assert result.entry is not None
            assert result.entry.ref == event.ref
            assert result.entry.event.title == "Зачёт"
    await engine.dispose()


@pytest.mark.anyio
async def test_chat_edit_selects_only_future_event_with_same_title():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    past_start = now - timedelta(days=2)
    future_start = now + timedelta(days=2)
    moved_start = future_start + timedelta(days=1, hours=2)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            await create_linked_event(
                db, "telegram", 7606, "Контрольная по русскому", "",
                past_start, past_start + timedelta(hours=1),
            )
            future = await create_linked_event(
                db, "telegram", 7606, "Контрольная по русскому", "",
                future_start, future_start + timedelta(hours=1),
            )
            result = await apply_chat_edit(
                db, "telegram", 7606,
                "перенеси контрольную по русскому на завтра в 15:00",
                [{
                    "title": "Контрольная по русскому",
                    "start_at": moved_start,
                    "end_at": moved_start + timedelta(hours=1),
                    "description": "",
                    "all_day": False,
                }],
            )
            assert result.status == "updated"
            assert result.entry is not None
            assert result.entry.ref == future.ref
    await engine.dispose()
