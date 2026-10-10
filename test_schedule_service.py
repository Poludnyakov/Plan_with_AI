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
    discard_pending_imports,
    finish_import_source,
    list_schedule_occurrences,
    parse_date_range,
    parse_occurrence_date,
    pending_import_source,
    save_import_source,
    set_draft_range,
    undo_import,
    import_summary,
    skip_occurrence,
    supersedes_pending_import,
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


@pytest.mark.parametrize("text, expected", [
    # The semester is under way: it stays in this year.
    ("с 1 сентября по 31 декабря", (date(2026, 9, 1), date(2026, 12, 31))),
    # Already over this year: the next one is meant.
    ("с 1 февраля по 31 мая", (date(2027, 2, 1), date(2027, 5, 31))),
    # Across the new year.
    ("с 1 сентября по 31 января", (date(2026, 9, 1), date(2027, 1, 31))),
    ("с 1 ноября 2026 по 1 марта", (date(2026, 11, 1), date(2027, 3, 1))),
])
def test_period_without_year_is_the_current_or_next_one(text, expected):
    assert parse_date_range(text, date(2026, 10, 8)) == expected


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
async def test_undo_import_removes_only_its_own_schedule_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with sessions() as db:
        draft = await create_import_draft(
            db, "telegram", 8080, {"confidence": 0.97, "slots": SLOTS}
        )
        await set_draft_range(db, draft, date(2026, 9, 1), date(2026, 12, 1))
        assert await confirm_import(db, "telegram", 8080, draft.id) == ("created", 2)
        db.add(ScheduleSeries(
            account_id=draft.account_id, title="Старое расписание", description="",
            weekday=4, start_time=datetime.strptime("19:00", "%H:%M").time(),
            end_time=datetime.strptime("20:00", "%H:%M").time(),
            valid_from=date(2026, 9, 1), valid_until=date(2026, 12, 1),
            week_pattern="every", source="manual",
        ))
        await db.commit()

        assert await undo_import(db, "telegram", 8080, draft.id) == ("undone", 2)
        assert await undo_import(db, "telegram", 8080, draft.id) == ("already_undone", 0)
        remaining = (await db.execute(select(ScheduleSeries))).scalars().all()
        assert [(item.title, item.source) for item in remaining] == [("Старое расписание", "manual")]
        summary = import_summary(draft, 2)
        assert "Расписание добавлено в календарь" in summary
        assert "Создано элементов: 2" in summary

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


def test_week_marks_leave_the_title():
    from schedule_service import split_week_marks
    base = {"weekday": 0, "start_time": "10:40", "end_time": "12:10", "week_pattern": "odd"}
    assert split_week_marks({**base, "title": "кр. 1 н. Системы массового обслуживания"}) == {
        **base, "title": "Системы массового обслуживания", "except_weeks": [1],
    }
    assert split_week_marks({**base, "title": "1,5,9,13 н. Физика"})["weeks"] == [1, 5, 9, 13]
    assert split_week_marks({**base, "title": "2-6 нед. Химия"})["weeks"] == [2, 3, 4, 5, 6]
    tail = split_week_marks({**base, "title": "Программирование (кр. 1 н.)"})
    assert (tail["title"], tail["except_weeks"]) == ("Программирование", [1])
    # A title that only starts with a number keeps it.
    assert split_week_marks({**base, "title": "1 неделя адаптации"})["title"] == "1 неделя адаптации"
    assert split_week_marks({**base, "title": "3D-моделирование"})["title"] == "3D-моделирование"


def test_week_parity_counts_from_the_start_of_the_semester():
    from schedule_service import academic_week
    # 1 September 2026 is a Tuesday in ISO week 36, but it is week I.
    assert academic_week(date(2026, 9, 1), date(2026, 9, 1)) == 1
    assert academic_week(date(2026, 9, 1), date(2026, 9, 7)) == 2
    # Imported later in the term, numbering still starts on 1 September.
    assert academic_week(date(2026, 10, 12), date(2026, 10, 12)) == 7
    # A spring term starts on its own first day.
    assert academic_week(date(2027, 2, 8), date(2027, 2, 15)) == 2
    # 1 September 2030 is a Sunday: week I starts on Monday 2 September.
    assert academic_week(date(2030, 9, 1), date(2030, 9, 2)) == 1


@pytest.mark.anyio
async def test_odd_weeks_and_except_first_week_are_respected():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    slot = {
        "title": "кр. 1 н. Системы массового обслуживания", "weekday": 0,
        "start_time": "10:40", "end_time": "12:10", "week_pattern": "odd",
    }
    async with sessions() as db:
        draft = await create_import_draft(db, "telegram", 9090, {"confidence": 1, "slots": [slot]})
        await set_draft_range(db, draft, date(2026, 9, 1), date(2026, 10, 4))
        assert await confirm_import(db, "telegram", 9090, draft.id) == ("created", 1)
        items = await list_schedule_occurrences(db, "telegram", 9090, date(2026, 9, 1), date(2026, 10, 4))
    # Odd weeks I, III, V start 31.08, 14.09, 28.09; week I is skipped.
    assert [item["id"].rsplit(":", 1)[1] for item in items] == ["2026-09-14", "2026-09-28"]
    assert {item["title"] for item in items} == {"Системы массового обслуживания"}
    await engine.dispose()


def test_group_codes_and_class_numbers_are_not_dates():
    from schedule_service import is_bare_period
    today = date(2026, 10, 9)
    for text in ("КМБО-03-19", "БИ-26-1", "1-2 пара 8:30–9:50, 3-4 пара 10:00–11:20"):
        assert parse_date_range(text, today) is None
        assert parse_occurrence_date(text, today) is None
        assert not supersedes_pending_import(text)
    assert is_bare_period("с 1 сентября по 31 декабря")
    assert not is_bare_period("отпуск с 1 по 7 ноября")
