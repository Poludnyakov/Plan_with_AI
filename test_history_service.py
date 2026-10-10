from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from history_service import (
    addition_history,
    delete_history_item,
    deletion_question,
    find_history_item,
    history_page,
)
from max_bot.models import MaxUser
from models import User
from schedule_models import ScheduleSeries
from schedule_service import confirm_import, create_import_draft, import_source_label, set_draft_range
from unified_calendar import create_linked_event, list_linked_events


ZONE = ZoneInfo("Europe/Moscow")
SLOTS = [
    {"title": "Математика", "weekday": 0, "start_time": "09:00", "end_time": "10:30", "week_pattern": "every"},
    {"title": "Физика", "weekday": 2, "start_time": "11:00", "end_time": "12:30", "week_pattern": "odd"},
]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"), patch("unified_calendar.delete_yandex_interval", new_callable=AsyncMock), \
         patch("unified_calendar.delete_max_yandex", new_callable=AsyncMock):
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([User(tg_id=11, timezone="Europe/Moscow"), User(tg_id=22, timezone="Europe/Moscow"),
                             MaxUser(max_user_id=33)])
            await session.commit()
            yield session
    await engine.dispose()


async def imported(db, platform, user, name, slots=SLOTS):
    draft = await create_import_draft(db, platform, user, {"confidence": 1, "slots": slots}, name)
    await set_draft_range(db, draft, date(2026, 9, 1), date(2026, 12, 31))
    await confirm_import(db, platform, user, draft.id)
    return draft


def test_source_label():
    assert import_source_label("ОСЕНЬ 26_27.xlsx") == "ОСЕНЬ 26_27.xlsx"
    assert import_source_label("schedule.jpg") == "Фото расписания"
    assert import_source_label(None) == "Фото расписания"


@pytest.mark.anyio
async def test_history_lists_imports_and_events_and_deletes_a_whole_import(db):
    first = await imported(db, "telegram", 11, "ОСЕНЬ.xlsx")
    await create_linked_event(
        db, "telegram", 11, "Встреча", "",
        datetime(2026, 10, 12, 10, tzinfo=ZONE), datetime(2026, 10, 12, 11, tzinfo=ZONE),
    )
    second = await imported(db, "telegram", 11, "Фото расписания", [{**SLOTS[0], "title": "Химия"}])
    repeated = await imported(db, "telegram", 11, "ОСЕНЬ.xlsx")  # nothing new
    other_user = await imported(db, "telegram", 22, "Чужое.xlsx")

    items = await addition_history(db, "telegram", 11)
    assert {item.ref for item in items} >= {f"imp:{first.id}", f"imp:{second.id}"}
    assert f"imp:{other_user.id}" not in {item.ref for item in items}
    assert f"imp:{repeated.id}" not in {item.ref for item in items}
    autumn = next(item for item in items if item.ref == f"imp:{first.id}")
    assert autumn.title == "ОСЕНЬ.xlsx" and autumn.detail.startswith("2 занятия · период 01.09.2026")
    assert "Все занятия этого импорта" in deletion_question(autumn)
    text, page, more = history_page(items)
    assert "ОСЕНЬ.xlsx" in text and "Встреча" in text and not more

    # Somebody else's import cannot be deleted by reference.
    assert await delete_history_item(db, "telegram", 11, f"imp:{other_user.id}") == "Импорт не найден."

    reply = await delete_history_item(db, "telegram", 11, f"imp:{first.id}")
    assert reply == "🗑 Импорт удалён: из календаря убрано 2 занятия."
    sources = {row for row in (await db.execute(select(ScheduleSeries.source))).scalars()}
    assert f"schedule_import:{first.id}" not in sources
    assert {f"schedule_import:{second.id}", f"schedule_import:{other_user.id}"} <= sources
    assert [entry.event.title for entry in await list_linked_events(db, "telegram", 11)] == ["Встреча"]
    undone = await find_history_item(db, "telegram", 11, f"imp:{first.id}")
    assert undone.detail == "импорт отменён" and not undone.deletable
    assert await delete_history_item(db, "telegram", 11, f"imp:{first.id}") == "Этот импорт уже удалён."


@pytest.mark.anyio
async def test_event_is_deleted_from_history_on_max(db):
    entry = await create_linked_event(
        db, "max", 33, "Стоматолог", "",
        datetime(2026, 10, 14, 15, tzinfo=ZONE), datetime(2026, 10, 14, 16, tzinfo=ZONE),
    )
    item = await find_history_item(db, "max", 33, f"ev:{entry.ref}")
    assert item.detail == "14.10.2026 15:00–16:00"
    assert await delete_history_item(db, "max", 33, f"ev:{entry.ref}") == "🗑 Событие удалено: «Стоматолог»."
    assert await list_linked_events(db, "max", 33) == []
    # Another user's event is not reachable.
    assert "не найдено" in await delete_history_item(db, "telegram", 11, f"ev:{entry.ref}")


@pytest.mark.anyio
async def test_empty_history(db):
    text, page, more = history_page(await addition_history(db, "telegram", 22))
    assert "пока пуста" in text and page == [] and not more
