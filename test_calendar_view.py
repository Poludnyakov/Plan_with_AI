import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from models import Event, User, EventStatus
from database import get_db
from dashboard_router import sign_tg_id, get_cookie_secret


def test_get_events_json_success():
    """
    Tests that GET /api/events with a valid session cookie returns a list of events
    formatted specifically for FullCalendar integration.
    """
    client = TestClient(app)
    
    # Sign and inject session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
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
        response = client.get("/api/events")
        
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


def test_get_events_json_unauthorized():
    """
    Tests that GET /api/events without a valid session cookie returns a 401 Unauthorized status.
    """
    client = TestClient(app)
    
    response = client.get("/api/events")
    assert response.status_code == 401
    assert "Сессия не найдена" in response.json()["detail"]


def test_get_calendar_page_success():
    """
    Tests that GET /calendar with a valid session cookie returns a 200 HTML response
    rendering the FullCalendar interactive schedule view.
    """
    client = TestClient(app)
    
    # Sign and inject session cookie
    signed_cookie = sign_tg_id(12345, get_cookie_secret())
    client.cookies.set("planiruy_session", signed_cookie)
    
    mock_user = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    mock_session = MagicMock(spec=AsyncSession)
    
    mock_user_result = MagicMock()
    mock_user_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_user_result)
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = client.get("/calendar")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "планиИруй!" in response.text
        assert 'id="timeline"' in response.text
        assert 'id="weeks"' in response.text
        assert "/api/calendar/events" in response.text
        assert "fullcalendar" not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_get_calendar_page_unauthorized_redirect():
    """
    Tests that GET /calendar without a session cookie redirects (HTTP 303) to /login.
    """
    client = TestClient(app)
    
    response = client.get("/calendar", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
