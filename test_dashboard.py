import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from models import Event, User, EventStatus
from database import get_db


def test_get_dashboard_success():
    """
    Tests that GET /dashboard/{user_tg_id} returns a 200 HTML response
    displaying the student's confirmed schedule tasks.
    """
    client = TestClient(app)
    
    mock_user = User(
        id=1,
        tg_id=12345,
        timezone="Europe/Moscow"
    )
    
    mock_event1 = Event(
        id=10,
        user_id=1,
        title="Сдача лабораторной по ИИ",
        description="Аудитория 302",
        deadline=datetime(2026, 6, 1, 15, 0, 0),
        status=EventStatus.CONFIRMED,
        is_completed=False
    )
    mock_event2 = Event(
        id=11,
        user_id=1,
        title="Зачет по дискретной математике",
        description="Аудитория 504",
        deadline=datetime(2026, 6, 2, 10, 0, 0),
        status=EventStatus.CONFIRMED,
        is_completed=True
    )
    
    # Mock Database Session responses
    mock_session = MagicMock(spec=AsyncSession)
    
    # Define execute sequence
    # 1st call: User retrieval
    # 2nd call: Confirmed events retrieval
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    
    mock_events_result = MagicMock()
    mock_events_result.scalars.return_value.all.return_value = [mock_event1, mock_event2]
    
    mock_session.execute = AsyncMock(side_effect=[mock_user_result, mock_events_result])
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.get("/dashboard/12345")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "планиИруй!" in response.text
        assert "ID: 12345" in response.text
        assert "Сдача лабораторной по ИИ" in response.text
        assert "Зачет по дискретной математике" in response.text
        assert "Аудитория 302" in response.text
        assert "Аудитория 504" in response.text
        assert "Выполнено" in response.text
        assert "Активно" in response.text
    finally:
        app.dependency_overrides.clear()


def test_get_dashboard_user_not_found():
    """
    Tests that GET /dashboard/{user_tg_id} renders the template with a 404 status
    code and an informative error message if the student is not registered.
    """
    client = TestClient(app)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.get("/dashboard/99999")
        
        assert response.status_code == 404
        assert "Студент с Telegram ID 99999 не зарегистрирован в системе" in response.text
        assert "Доступ ограничен" in response.text
    finally:
        app.dependency_overrides.clear()


def test_toggle_event_complete_success():
    """
    Tests that POST /events/{event_id}/toggle-complete toggles the is_completed
    field in the database and returns a 200 JSON success response.
    """
    client = TestClient(app)
    
    mock_event = Event(
        id=42,
        user_id=1,
        title="Сдача лабораторной по ИИ",
        description="Аудитория 302",
        deadline=datetime(2026, 6, 1, 15, 0, 0),
        status=EventStatus.CONFIRMED,
        is_completed=False
    )
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_event_result = MagicMock()
    mock_event_result.scalar_one_or_none.return_value = mock_event
    mock_session.execute = AsyncMock(return_value=mock_event_result)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.post("/events/42/toggle-complete")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["event_id"] == 42
        assert data["is_completed"] is True
        
        # Verify db logic
        assert mock_event.is_completed is True
        mock_session.commit.assert_called_once()
        mock_session.refresh.assert_called_once_with(mock_event)
    finally:
        app.dependency_overrides.clear()


def test_toggle_event_complete_not_found():
    """
    Tests that POST /events/{event_id}/toggle-complete returns a 404 error
    if the event does not exist.
    """
    client = TestClient(app)
    
    mock_session = MagicMock(spec=AsyncSession)
    mock_event_result = MagicMock()
    mock_event_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_event_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.post("/events/9999/toggle-complete")
        
        assert response.status_code == 404
        data = response.json()
        assert "не найдено" in data["detail"]
    finally:
        app.dependency_overrides.clear()
