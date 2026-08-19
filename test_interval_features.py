from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import interval_models
from app_intervals import app
from dashboard_router import get_cookie_secret, sign_tg_id
from database import Base, get_db
from handlers.pipeline_handlers_intervals import handle_confirm_callback
from interval_ai_service import IntervalExtraction
from interval_models import EventTiming
from interval_pipeline import IntervalActionPipelineService
from models import Event, EventStatus, User


@pytest.fixture
async def interval_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        yield session
    await engine.dispose()


def test_ai_interval_requires_end_after_start():
    start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    valid = IntervalExtraction(
        title="Контрольная",
        start_at=start,
        end_at=start + timedelta(hours=2),
    )
    assert valid.end_at - valid.start_at == timedelta(hours=2)
    with pytest.raises(ValueError):
        IntervalExtraction(title="Ошибка", start_at=start, end_at=start)


@pytest.mark.anyio
async def test_pipeline_persists_start_and_end(interval_db):
    pipeline = IntervalActionPipelineService()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    end = start + timedelta(hours=2)
    events = await pipeline._persist(
        123456,
        [{"title": "Контрольная", "start_at": start, "end_at": end, "description": "ауд. 1"}],
        interval_db,
    )
    assert len(events) == 1
    timing_result = await interval_db.execute(
        select(EventTiming).filter(EventTiming.event_id == events[0].id)
    )
    timing = timing_result.scalar_one()
    assert timing.start_at.replace(tzinfo=timezone.utc) == start
    assert timing.end_at.replace(tzinfo=timezone.utc) == end
    assert events[0].deadline.replace(tzinfo=timezone.utc) == end
    assert len(events[0].reminders) == 5


@pytest.mark.anyio
async def test_confirmation_survives_external_calendar_failure():
    telegram_user = TelegramUser(id=999222111, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=telegram_user.id, type="private"),
        from_user=telegram_user,
        text="draft",
    )
    callback = CallbackQuery(
        id="callback",
        from_user=telegram_user,
        chat_instance="chat",
        message=message,
        data="confirm:42",
    )
    start = datetime.now(timezone.utc) + timedelta(days=1)
    event = Event(
        id=42,
        user_id=5,
        title="Контрольная [группа]",
        deadline=start + timedelta(hours=2),
        status=EventStatus.DRAFT,
    )
    timing = EventTiming(event_id=42, start_at=start, end_at=start + timedelta(hours=2))
    session = MagicMock(spec=AsyncSession)
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = event
    timing_result = MagicMock()
    timing_result.scalar_one_or_none.return_value = timing
    conflict_result = MagicMock()
    conflict_result.first.return_value = None
    session.execute = AsyncMock(side_effect=[event_result, timing_result, conflict_result])

    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer, \
         patch.object(Message, "edit_text", new_callable=AsyncMock) as edit, \
         patch("handlers.pipeline_handlers_intervals.asyncio.create_task") as create_task:
        await handle_confirm_callback(callback, session)

    assert event.status == EventStatus.CONFIRMED
    session.commit.assert_awaited_once()
    answer.assert_awaited_once_with("✅ Подтверждено")
    assert "Добавлено в календарь" in edit.await_args.args[0]
    create_task.assert_called_once()
    create_task.call_args.args[0].close()


def test_timeline_template_is_dependency_free(interval_db):
    mock_session = MagicMock(spec=AsyncSession)
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    mock_session.execute = AsyncMock(return_value=user_result)

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("planiruy_session", sign_tg_id(12345, get_cookie_secret()))
    try:
        response = client.get("/mini-timeline")
        assert response.status_code == 200
        assert 'id="timeline"' in response.text
        assert 'id="start-at"' in response.text
        assert 'id="end-at"' in response.text
        assert "/api/v2/events" in response.text
        assert "fullcalendar" not in response.text.lower()
    finally:
        app.dependency_overrides.clear()
