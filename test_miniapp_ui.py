from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app_miniapp_ui import app
from dashboard_router import get_cookie_secret, sign_tg_id
from database import get_db
from models import User


def test_mini_calendar_is_lightweight_and_responsive():
    mock_session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = User(id=1, tg_id=12345, timezone="Europe/Moscow")
    mock_session.execute = AsyncMock(return_value=result)

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("planiruy_session", sign_tg_id(12345, get_cookie_secret()))
    try:
        response = client.get("/mini-calendar")
        assert response.status_code == 200
        assert 'id="month-grid"' in response.text
        assert 'id="agenda-list"' in response.text
        assert 'viewport-fit=cover' in response.text
        assert "fullcalendar" not in response.text.lower()
        assert "bootstrap" not in response.text.lower()
        assert "tippy" not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_mini_calendar_requires_session():
    client = TestClient(app)
    response = client.get("/mini-calendar")
    assert response.status_code == 401


def test_miniapp_entry_loads_telegram_bridge():
    client = TestClient(app)
    response = client.get("/miniapp")
    assert response.status_code == 200
    assert "telegram-web-app.js" in response.text
