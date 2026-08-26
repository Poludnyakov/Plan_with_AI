import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, get_db
from max_bot.api import MaxApiClient, callback_button
from max_bot.app import app
from max_bot.auth import (
    sign_max_access_token,
    sign_max_session,
    validate_max_init_data,
    verify_max_access_token,
    verify_max_session,
)
from max_bot.calendar import max_yandex_uid
from max_bot.models import MaxEvent, MaxEventTiming, MaxUser
from max_bot.service import MaxEventService


def signed_init_data(token: str, now: int, user_key: str = "user_id") -> str:
    fields = {
        "auth_date": str(now),
        "query_id": "max-query",
        "user": json.dumps({user_key: 990011, "first_name": "Ирина"}, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_max_init_data_follows_official_hmac_and_rejects_tampering():
    token, now = "max-secret-token", 2_000_000_000
    value = signed_init_data(token, now)
    assert validate_max_init_data(value, token, now=now)["user_id"] == 990011
    assert validate_max_init_data(value.replace("max-query", "tampered-query"), token, now=now) is None
    assert validate_max_init_data(value, token, now=now + 3601) is None


def test_max_init_data_accepts_legacy_id_and_rejects_duplicate_keys():
    token, now = "token", 2_000_000_000
    value = signed_init_data(token, now, user_key="id")
    assert validate_max_init_data(value, token, now=now)["user_id"] == 990011
    assert validate_max_init_data(value + "&auth_date=1", token, now=now) is None


def test_max_session_and_fallback_tokens_are_signed_and_expire():
    session = sign_max_session(42, "token")
    assert verify_max_session(session, "token") == 42
    assert verify_max_session(session + "x", "token") is None
    access = sign_max_access_token(42, "token", issued_at=1_000_000)
    assert verify_max_access_token(access, "token", now=1_000_001) == 42
    assert verify_max_access_token(access, "token", now=1_086_401) is None


@pytest.mark.anyio
async def test_max_api_uses_authorization_header_and_official_message_shapes():
    seen = []

    def transport(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = MaxApiClient("secret", "https://platform-api2.max.ru")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://platform-api2.max.ru",
        headers={"Authorization": "secret"},
        transport=httpx.MockTransport(transport),
    )
    try:
        await client.send_message("Привет", user_id=77, buttons=[[callback_button("Да", "confirm:1")]])
        await client.answer_callback("cb-1", text="Готово", notification="OK")
        await client.set_commands([{"name": "start", "description": "Начать"}])
    finally:
        await client.close()
    first, second = seen[:2]
    assert first.url.path == "/messages" and first.url.params["user_id"] == "77"
    assert first.headers["Authorization"] == "secret"
    body = json.loads(first.content)
    assert body["attachments"][0]["type"] == "inline_keyboard"
    assert body["attachments"][0]["payload"]["buttons"][0][0]["payload"] == "confirm:1"
    assert second.url.path == "/answers" and second.url.params["callback_id"] == "cb-1"
    assert json.loads(seen[2].content) == {"commands": [{"name": "start", "description": "Начать"}]}


@pytest.fixture
async def max_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        yield session
    await engine.dispose()


@pytest.mark.anyio
async def test_max_events_use_separate_tables_and_conflicts_remove_new_draft(max_db):
    service = MaxEventService()
    start = datetime.now(timezone.utc) + timedelta(days=2)
    existing = (await service.create_drafts(max_db, 101, [{
        "title": "Лекция", "description": "", "start_at": start, "end_at": start + timedelta(hours=2)
    }]))[0]
    existing.status = "confirmed"
    await max_db.commit()
    draft = (await service.create_drafts(max_db, 101, [{
        "title": "Контрольная", "description": "", "start_at": start + timedelta(hours=1), "end_at": start + timedelta(hours=3)
    }]))[0]
    outcome, conflict = await service.confirm(max_db, 101, draft.id)
    assert outcome == "conflict" and conflict[0].id == existing.id
    assert await max_db.get(MaxEvent, draft.id) is None
    assert (await max_db.execute(select(MaxUser).filter_by(max_user_id=101))).scalar_one().max_user_id == 101


def test_max_calendar_uid_never_collides_with_telegram_uid():
    assert max_yandex_uid(12) == "planiruy-max-event-12@planwithai.ru"
    assert max_yandex_uid(12) != "planiruy-event-12@planwithai.ru"


def test_max_miniapp_auth_cookie_bearer_and_templates():
    token, now = "max-token", int(datetime.now(timezone.utc).timestamp())
    mock_session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = MaxUser(id=1, max_user_id=990011)
    mock_session.execute = AsyncMock(return_value=result)

    async def override_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_db
    fake_settings = SimpleNamespace(bot_token=token, webhook_secret="hook")
    try:
        with patch("max_bot.web.settings", fake_settings):
            client = TestClient(app, base_url="https://planwithai.ru")
            response = client.post("/max/api/auth", json={"init_data": signed_init_data(token, now)})
            assert response.status_code == 200
            assert verify_max_access_token(response.json()["access_token"], token) == 990011
            cookie = response.headers["set-cookie"].lower()
            assert "secure" in cookie and "httponly" in cookie and "samesite=none" in cookie
            page = client.get("/max/miniapp")
            timeline = client.get("/max/timeline")
            assert "https://st.max.ru/js/max-web-app.js" in page.text
            assert "window.WebApp" in page.text
            assert "/max/api/events" in timeline.text
            assert "planiruy_max_access_token" in timeline.text
    finally:
        app.dependency_overrides.clear()


def test_max_webhook_rejects_wrong_secret_before_processing():
    with patch("max_bot.web.settings", SimpleNamespace(webhook_secret="expected")):
        client = TestClient(app, base_url="https://planwithai.ru")
        response = client.post("/max/webhook", json={"update_type": "bot_started"}, headers={"X-Max-Bot-Api-Secret": "wrong"})
    assert response.status_code == 401
