from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from account_service import consume_link_code, create_link_code
from calendar_feed_service import build_feed, feed_links, feed_token
from database import Base, get_db
from max_bot.models import MaxUser
from models import User
from schedule_service import confirm_import, create_import_draft, set_draft_range
from unified_calendar import create_linked_event


ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def events_of(body: bytes) -> dict[str, object]:
    return {
        str(item.get("summary")): item for item in Calendar.from_ical(body).walk("VEVENT")
    }


@pytest.mark.anyio
async def test_feed_has_events_of_both_messengers_and_classes(sessions):
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with sessions() as db:
            db.add_all([User(tg_id=101, timezone="Europe/Moscow"), MaxUser(max_user_id=202)])
            await db.commit()
            await create_linked_event(
                db, "telegram", 101, "Встреча", "кабинет 5",
                datetime(2026, 10, 12, 10, tzinfo=ZONE), datetime(2026, 10, 12, 11, tzinfo=ZONE),
            )
            await create_linked_event(
                db, "max", 202, "Отпуск", "",
                datetime(2026, 11, 1, tzinfo=ZONE), datetime(2026, 11, 8, tzinfo=ZONE), None, True,
            )
            await consume_link_code(db, "max", 202, await create_link_code(db, "telegram", 101))
            draft = await create_import_draft(db, "telegram", 101, {"confidence": 1, "slots": [{
                "title": "кр. 1 н. Физика", "weekday": 0, "start_time": "09:00",
                "end_time": "10:30", "week_pattern": "odd",
            }]})
            await set_draft_range(db, draft, date(2026, 9, 1), date(2026, 9, 30))
            await confirm_import(db, "telegram", 101, draft.id)

            token = await feed_token(db, "max", 202)
            assert token == await feed_token(db, "telegram", 101)  # one account, one link
            body = await build_feed(db, token, now=NOW)

    calendar = Calendar.from_ical(body)
    assert str(calendar.get("x-wr-calname")) == "планиИруй!"
    items = list(calendar.walk("VEVENT"))
    meeting = next(item for item in items if str(item.get("summary")) == "Встреча")
    assert meeting.decoded("dtstart") == datetime(2026, 10, 12, 10, tzinfo=ZONE)
    assert str(meeting.get("description")) == "кабинет 5"
    holiday = next(item for item in items if str(item.get("summary")) == "Отпуск")
    assert holiday.decoded("dtstart") == date(2026, 11, 1)
    assert holiday.decoded("dtend") == date(2026, 11, 8)
    # Odd weeks of September except week I: 14 and 28 September.
    classes = sorted(
        item.decoded("dtstart").date() for item in items if str(item.get("summary")) == "Физика"
    )
    assert classes == [date(2026, 9, 14), date(2026, 9, 28)]
    assert len({str(item.get("uid")) for item in items}) == len(items)


@pytest.mark.anyio
async def test_reset_disables_the_old_link(sessions):
    async with sessions() as db:
        db.add(User(tg_id=303, timezone="Europe/Moscow"))
        await db.commit()
        old = await feed_token(db, "telegram", 303)
        new = await feed_token(db, "telegram", 303, reset=True)
        assert old != new
        assert await build_feed(db, old) is None
        assert await build_feed(db, new) is not None


def test_links_for_each_calendar():
    links = feed_links("abcDEF123_-abcDEF123", "https://planwithai.ru")
    assert links["webcal"] == "webcal://planwithai.ru/calendar/feed/abcDEF123_-abcDEF123.ics"
    assert links["https"].startswith("https://planwithai.ru/calendar/feed/")
    assert links["google"].startswith("https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2F")
    assert links["connect"] == "https://planwithai.ru/calendar/connect/abcDEF123_-abcDEF123"


@pytest.mark.anyio
async def test_feed_routes(sessions):
    from calendar_feed_router import router
    from fastapi import FastAPI

    async with sessions() as db:
        db.add(User(tg_id=404, timezone="Europe/Moscow"))
        await db.commit()
        token = await feed_token(db, "telegram", 404)

    app = FastAPI()
    app.include_router(router)

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    client = TestClient(app)
    response = client.get(f"/calendar/feed/{token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert client.get("/calendar/feed/" + "x" * 30 + ".ics").status_code == 404
    assert client.get("/calendar/feed/<script>.ics").status_code == 404
    page = client.get(f"/calendar/connect/{token}")
    assert page.status_code == 200 and "webcal://" in page.text and "Яндекс" in page.text
