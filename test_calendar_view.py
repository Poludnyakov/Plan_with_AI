import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from models import Event, User, EventStatus
from database import get_db


def test_get_events_json_success():
    """
    Tests that GET /api/events/{user_tg_id} returns a list of events
    formatted specifically for FullCalendar integration.
    """
    client = TestClient(app)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    mock_event = Event(
        id=10,
        user_id=1,
        title="Сдача лабораторной по ИИ",
        description="Аудитория 302",
        deadline=datetime(2026, 6, 1, 15, 0, 0),
        status=EventStatus.CONFIRMED,
        is_completed=False
    )
    
    mock_session = MagicMock(spec=AsyncSession)
    
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    
    mock_events_result = MagicMock()
    mock_events_result.scalars.return_value.all.return_value = [mock_event]
    
    mock_session.execute = AsyncMock(side_effect=[mock_user_result, mock_events_result])
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.get("/api/events/12345")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == 10
        assert data[0]["title"] == "Сдача лабораторной по ИИ"
        assert "2026-06-01T15:00:00" in data[0]["start"]
        assert data[0]["description"] == "Аудитория 302"
        assert data[0]["color"] == "#7B2CBF"
    finally:
        app.dependency_overrides.clear()


def test_get_events_json_user_not_found():
    """
    Tests that GET /api/events/{user_tg_id} returns a 404 error
    if the student is not registered.
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
        response = client.get("/api/events/99999")
        assert response.status_code == 404
        assert "не найден" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_calendar_page_success():
    """
    Tests that GET /calendar/{user_tg_id} returns a 200 HTML response
    rendering the FullCalendar interactive schedule view.
    """
    client = TestClient(app)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    mock_session = MagicMock(spec=AsyncSession)
    
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.get("/calendar/12345")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "планиИруй!" in response.text
        assert "Календарь дедлайнов" in response.text
        assert "12345" in response.text
        assert "fullcalendar" in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_get_calendar_page_user_not_found():
    """
    Tests that GET /calendar/{user_tg_id} renders the templates with a 404 status
    if the student is not registered.
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
        response = client.get("/calendar/99999")
        
        assert response.status_code == 404
        assert "Студент с Telegram ID 99999 не зарегистрирован в системе" in response.text
        assert "Доступ ограничен" in response.text
    finally:
        app.dependency_overrides.clear()
