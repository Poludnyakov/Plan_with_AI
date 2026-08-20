from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app_intervals import app
from dashboard_router import get_cookie_secret, sign_tg_id
from database import get_db
from models import User


def test_timeline_template_is_dependency_free():
    mock_session = MagicMock(spec=AsyncSession)
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = User(
        id=1, tg_id=12345, timezone="Europe/Moscow"
    )
    mock_session.execute = AsyncMock(return_value=user_result)

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set(
        "planiruy_session", sign_tg_id(12345, get_cookie_secret())
    )
    try:
        response = client.get("/mini-timeline")
        assert response.status_code == 200
        assert 'id="timeline"' in response.text
        assert '<div class="calendar-brand"><span id="dateLabel"></span></div>' in response.text
        assert '<div class="calendar-brand"><strong>' not in response.text
        assert 'id="start-at"' in response.text
        assert 'id="end-at"' in response.text
        assert "/api/v2/events" in response.text
        assert "fullcalendar" not in response.text.lower()
        assert 'id="addEventButton"' in response.text
        assert 'id="editTitle"' in response.text
        assert 'id="editStart"' in response.text
        assert 'id="editEnd"' in response.text
        assert 'data-resize="start"' not in response.text
        assert 'data-resize="end"' not in response.text
        assert "state.selectedEventId!==eventId(event)" in response.text
        assert "Math.abs(distance)<10" in response.text
        assert "drag-origin" in response.text
        assert "emptySlotClick" in response.text
        assert "openCreate(atMinute(state.date,hour*60))" in response.text
        assert 'placeholder="Новое событие"' in response.text
        assert 'placeholder="Детали"' in response.text
        assert 'id="createReminders"' in response.text
        assert 'id="editReminders"' in response.text
        assert "collectReminderTimes" in response.text
        assert "Событие выделено" not in response.text
        assert "Нажмите ещё раз" not in response.text
    finally:
        app.dependency_overrides.clear()
