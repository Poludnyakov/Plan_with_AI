from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app_intervals import app
from database import get_db
from miniapp_interval_router import (
    MINIAPP_TOKEN_TTL_SECONDS,
    sign_miniapp_access_token,
    verify_miniapp_access_token,
)
from models import User


def test_miniapp_access_token_is_signed_and_expires():
    token = sign_miniapp_access_token(12345, issued_at=1_000_000)
    assert verify_miniapp_access_token(token, now=1_000_001) == 12345
    assert verify_miniapp_access_token(
        token, now=1_000_000 + MINIAPP_TOKEN_TTL_SECONDS + 1
    ) is None
    assert verify_miniapp_access_token(token + "tampered", now=1_000_001) is None


def test_miniapp_auth_sets_cross_site_cookie_and_returns_fallback_token():
    mock_session = MagicMock(spec=AsyncSession)
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = User(
        id=1, tg_id=12345, timezone="Europe/Moscow"
    )
    mock_session.execute = AsyncMock(return_value=user_result)

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with patch(
            "miniapp_interval_router.validate_telegram_init_data",
            return_value={"id": 12345},
        ):
            client = TestClient(app, base_url="https://planwithai.ru")
            response = client.post(
                "/api/auth/miniapp",
                json={"init_data": "valid", "destination": "calendar"},
            )
        assert response.status_code == 200
        assert response.json()["redirect"] == "/mini-timeline"
        token = response.json()["access_token"]
        assert verify_miniapp_access_token(token) == 12345
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=none" in cookie
        assert "path=/" in cookie
    finally:
        app.dependency_overrides.clear()


def test_timeline_contains_cookie_and_bearer_fallback():
    client = TestClient(app, base_url="https://planwithai.ru")
    response = client.get("/mini-timeline")
    assert response.status_code == 200
    assert "planiruy_access_token" in response.text
    assert "headers.Authorization" in response.text
    assert "credentials:" in response.text
    assert "include" in response.text
    assert "authenticatedFetch" in response.text
    assert "/api/v2/events" in response.text


def test_events_api_accepts_bearer_when_cookie_is_blocked():
    mock_session = MagicMock(spec=AsyncSession)
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = User(
        id=1, tg_id=12345, timezone="Europe/Moscow"
    )
    events_result = MagicMock()
    events_result.all.return_value = []
    mock_session.execute = AsyncMock(side_effect=[user_result, events_result])

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, base_url="https://planwithai.ru")
        token = sign_miniapp_access_token(12345)
        response = client.get(
            "/api/v2/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.dependency_overrides.clear()
