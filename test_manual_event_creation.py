import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from models import Event, User, EventStatus, Reminder, ReminderStatus
from database import get_db
from dashboard_router import sign_tg_id, get_cookie_secret


@pytest.mark.anyio
async def test_create_event_manually_success():
    """
    Tests that POST /api/events manually creates a confirmed event,
    calculates future pre-emptive reminders, and returns a 200 JSON success response.
    Requires a signed session cookie.
    """
    client = TestClient(app)
    
    # Inject signed session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    
    # We setup the deadline to be in 2 days so all 5 reminders are in the future
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    # Mock repositories
    mock_event = Event(
        id=42,
        user_id=1,
        title="Новый дедлайн",
        description="Сдать лабораторную работу",
        deadline=deadline,
        status=EventStatus.CONFIRMED
    )
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    # Patch repositories inside the handler
    with patch("repositories.EventRepository.create", new_callable=AsyncMock) as mock_event_create, \
         patch("repositories.ReminderRepository.create", new_callable=AsyncMock) as mock_reminder_create, \
         patch("yandex_calendar_service.YandexCalendarService.add_deadline_to_yandex", new_callable=AsyncMock) as mock_yandex_sync:
         
        mock_event_create.return_value = mock_event
        
        payload = {
            "title": "Новый дедлайн",
            "description": "Сдать лабораторную работу",
            "deadline": deadline.isoformat()
        }
        
        try:
            response = client.post("/api/events", json=payload)
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["event_id"] == 42
            assert data["title"] == "Новый дедлайн"
            
            # Since the deadline is in 2 days, all 5 reminders (24h, 12h, 1h, 30m, 15m) are in the future
            assert mock_reminder_create.call_count == 5
            
            # Verify Yandex CalDAV Sync was triggered
            mock_yandex_sync.assert_called_once_with(
                title="Новый дедлайн",
                deadline=deadline,
                description="Сдать лабораторную работу"
            )
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_event_manually_filters_past_reminders():
    """
    Tests that POST /api/events filters out past reminders.
    If the deadline is in 2 hours, only 3 reminders (1h, 30m, 15m before deadline) are in the future.
    """
    client = TestClient(app)
    
    # Inject signed session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    
    # Deadline in 2 hours (120 minutes)
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    mock_event = Event(
        id=43,
        user_id=1,
        title="Срочный дедлайн",
        description="Быстрая задача",
        deadline=deadline,
        status=EventStatus.CONFIRMED
    )
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    with patch("repositories.EventRepository.create", new_callable=AsyncMock) as mock_event_create, \
         patch("repositories.ReminderRepository.create", new_callable=AsyncMock) as mock_reminder_create, \
         patch("yandex_calendar_service.YandexCalendarService.add_deadline_to_yandex", new_callable=AsyncMock) as mock_yandex_sync:
         
        mock_event_create.return_value = mock_event
        
        payload = {
            "title": "Срочный дедлайн",
            "description": "Быстрая задача",
            "deadline": deadline.isoformat()
        }
        
        try:
            response = client.post("/api/events", json=payload)
            assert response.status_code == 200
            
            # Since the deadline is in 2 hours (<24h), it schedules an immediate reminder (1),
            # plus the 3 future reminders (1h, 30m, 15m), making a total of 4 reminders.
            assert mock_reminder_create.call_count == 4
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_event_manually_under_24_hours_creates_immediate_reminder():
    """
    Tests that POST /api/events registers an immediate reminder
    if the event's deadline is scheduled in under 24 hours (e.g., 23 hours).
    """
    client = TestClient(app)
    
    # Inject signed session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    
    # Deadline in 23 hours
    deadline = datetime.now(timezone.utc) + timedelta(hours=23)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    mock_event = Event(
        id=44,
        user_id=1,
        title="Дедлайн завтра",
        deadline=deadline,
        status=EventStatus.CONFIRMED
    )
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    with patch("repositories.EventRepository.create", new_callable=AsyncMock) as mock_event_create, \
         patch("repositories.ReminderRepository.create", new_callable=AsyncMock) as mock_reminder_create, \
         patch("yandex_calendar_service.YandexCalendarService.add_deadline_to_yandex", new_callable=AsyncMock):
         
        mock_event_create.return_value = mock_event
        
        payload = {
            "title": "Дедлайн завтра",
            "deadline": deadline.isoformat()
        }
        
        try:
            response = client.post("/api/events", json=payload)
            assert response.status_code == 200
            
            # 23 hours is < 24 hours, so:
            # - Immediate reminder (1) is scheduled.
            # - Loop through intervals: 24h is in the past (skipped), 12h, 1h, 30m, 15m are in the future (4).
            # Total call count should be 5.
            assert mock_reminder_create.call_count == 5
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_event_manually_user_not_found():
    """
    Tests that POST /api/events returns a 404 error
    if the student is authorized but not found in the system database.
    """
    client = TestClient(app)
    
    # Inject signed session cookie for 99999
    signed_cookie = sign_tg_id(99999, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    payload = {
        "title": "Дедлайн без студента",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    }
    
    try:
        response = client.post("/api/events", json=payload)
        assert response.status_code == 404
        assert "не найден" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_event_manually_conflict():
    """
    Tests that POST /api/events returns a 400 Bad Request
    if there's an already confirmed event overlapping within 1 hour.
    """
    client = TestClient(app)
    
    # Inject signed session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    
    deadline = datetime.now(timezone.utc) + timedelta(days=1)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    conflicting_event = Event(
        id=99,
        user_id=1,
        title="Сдача ИИ",
        deadline=deadline,
        status=EventStatus.CONFIRMED
    )
    
    with patch("repositories.EventRepository.get_conflicting_event", new_callable=AsyncMock) as mock_get_conflicting:
        mock_get_conflicting.return_value = conflicting_event
        
        payload = {
            "title": "Новый дедлайн",
            "description": "Сдать лабораторную работу",
            "deadline": deadline.isoformat()
        }
        
        try:
            response = client.post("/api/events", json=payload)
            assert response.status_code == 400
            data = response.json()
            assert "пересекается с уже забронированной задачей" in data["detail"]
            assert "Сдача ИИ" in data["detail"]
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_delete_event_success():
    """
    Tests that DELETE /api/events/{event_id} successfully deletes an event,
    commits the database session, and returns success.
    """
    client = TestClient(app)
    
    mock_event = Event(id=42, title="Тестовая задача для удаления")
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_event
    mock_session.execute = AsyncMock(return_value=mock_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.delete("/api/events/42")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Тестовая задача для удаления" in data["message"]
        
        mock_session.delete.assert_called_once_with(mock_event)
        mock_session.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_delete_event_not_found():
    """
    Tests that DELETE /api/events/{event_id} returns 404
    if the event does not exist.
    """
    client = TestClient(app)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.delete("/api/events/999")
        assert response.status_code == 404
        assert "не найдено" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_toggle_event_complete_success():
    """
    Tests that POST /events/{event_id}/toggle-complete successfully
    toggles the completion state in the DB.
    """
    client = TestClient(app)
    
    mock_event = Event(id=42, title="Тестовая задача", is_completed=False)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_event
    mock_session.execute = AsyncMock(return_value=mock_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.post("/events/42/toggle-complete")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["is_completed"] is True
        assert mock_event.is_completed is True
        mock_session.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()
