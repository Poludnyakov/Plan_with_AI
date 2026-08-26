from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from account_service import consume_link_code, create_link_code
from database import Base
from schedule_models import ScheduleException, ScheduleImportSource, ScheduleSeries
from schedule_service import (
    confirm_import,
    create_import_draft,
    finish_import_source,
    list_schedule_occurrences,
    parse_date_range,
    parse_occurrence_date,
    pending_import_source,
    save_import_source,
    set_draft_range,
    skip_occurrence,
)
from unified_calendar import create_linked_event


SLOTS = [
    {
        "title": "Математика",
        "weekday": 0,
        "start_time": "09:00",
        "end_time": "10:30",
        "description": "ауд. 201",
        "week_pattern": "every",
        "confidence": 0.98,
    },
    {
        "title": "Физика",
        "weekday": 2,
        "start_time": "11:00",
        "end_time": "12:30",
        "description": "",
        "week_pattern": "every",
        "confidence": 0.95,
    },
]


def test_russian_range_and_occurrence_date_are_deterministic():
    today = date(2026, 8, 20)
    assert parse_date_range("с 1 сентября по 1 декабря", today) == (
        date(2026, 9, 1), date(2026, 12, 1)
    )
    assert parse_date_range("01.09.2026 - 01.12.2026", today) == (
        date(2026, 9, 1), date(2026, 12, 1)
    )
    assert parse_occurrence_date("не иду на физику завтра", today) == date(2026, 8, 21)


@pytest.mark.anyio
async def test_schedule_is_shared_idempotent_skippable_and_does_not_block_events():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            draft = await create_import_draft(
                db, "telegram", 1001, {"confidence": 0.97, "slots": SLOTS}
            )
            await set_draft_range(db, draft, date(2026, 8, 24), date(2026, 9, 6))
            assert await confirm_import(db, "telegram", 1001, draft.id) == ("created", 2)
            assert await confirm_import(db, "telegram", 1001, draft.id) == ("imported", 0)
            assert (await db.scalar(select(func.count()).select_from(ScheduleSeries))) == 2

            code = await create_link_code(db, "telegram", 1001)
            await consume_link_code(db, "max", 2002, code)
            from_max = await list_schedule_occurrences(
                db, "max", 2002, date(2026, 8, 24), date(2026, 8, 30)
            )
            assert {item["title"] for item in from_max} == {"Математика", "Физика"}
            assert all(item["kind"] == "schedule" for item in from_max)

            maths = next(item for item in from_max if item["title"] == "Математика")
            assert await skip_occurrence(db, "max", 2002, maths["id"])
            assert await skip_occurrence(db, "telegram", 1001, maths["id"])
            assert (await db.scalar(select(func.count()).select_from(ScheduleException))) == 1
            after_skip = await list_schedule_occurrences(
                db, "telegram", 1001, date(2026, 8, 24), date(2026, 8, 30)
            )
            assert [item["title"] for item in after_skip] == ["Физика"]

            # A personal event may overlap the background class by design.
            start = datetime(2026, 8, 26, 8, 15, tzinfo=timezone.utc)
            event = await create_linked_event(
                db, "telegram", 1001, "Личная встреча", "",
                start, start + timedelta(hours=1),
            )
            assert event.event.title == "Личная встреча"
    await engine.dispose()


@pytest.mark.anyio
async def test_saved_import_file_survives_dialogue_and_is_shared_after_linking():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with sessions() as db:
        code = await create_link_code(db, "telegram", 3003)
        await consume_link_code(db, "max", 4004, code)
        saved = await save_import_source(
            db, "telegram", 3003, b"original workbook", "schedule.xlsx",
            "группа ПИ-241",
        )

    async with sessions() as db:
        restored = await pending_import_source(db, "max", 4004)
        assert restored is not None
        assert restored.id == saved.id
        assert restored.content == b"original workbook"
        assert restored.prompt == "группа ПИ-241"
        await finish_import_source(db, restored)

    async with sessions() as db:
        assert await pending_import_source(db, "telegram", 3003) is None
        stored = await db.get(ScheduleImportSource, saved.id)
        assert stored.status == "processed"
        assert stored.content == b""
    await engine.dispose()
