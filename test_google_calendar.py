import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from models import Event, User, EventStatus
from google.oauth2.credentials import Credentials
from google_calendar_service import GoogleCalendarService, sync_event_to_google_calendar
from database import get_db


@pytest.mark.anyio
async def test_google_calendar_service_insert():
    """
    Tests that GoogleCalendarService.Calendar correctly builds discovery client
    and inserts the event with a 30-minute duration block in a worker thread.
    """
    # 1. Setup service and mock credentials
    service = GoogleCalendarService()
    mock_credentials = MagicMock(spec=Credentials)
    
    deadline_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    
    # 2. Mock googleapiclient build client
    mock_events_resource = MagicMock()
    mock_events_resource.insert.return_value.execute.return_value = {"id": "mock_gcal_id_123"}
    
    mock_service_client = MagicMock()
    mock_service_client.events.return_value = mock_events_resource
    
    with patch("google_calendar_service.build", return_value=mock_service_client) as mock_build:
        result = await service.Calendar(
            user_credentials=mock_credentials,
            event_title="Лабораторная по физике",
            deadline=deadline_dt,
            description="Защита отчетов"
        )
        
        # Verify build client arguments
        mock_build.assert_called_once_with("calendar", "v3", credentials=mock_credentials)
        
        # Verify insert payload
        mock_events_resource.insert.assert_called_once()
        args, kwargs = mock_events_resource.insert.call_args
        assert kwargs["calendarId"] == "primary"
        
        body = kwargs["body"]
        assert body["summary"] == "Лабораторная по физике"
        assert body["description"] == "Защита отчетов"
        assert body["start"]["dateTime"] == "2026-06-01T12:00:00+00:00"
        # 30-minute block logic
        assert body["end"]["dateTime"] == "2026-06-01T12:30:00+00:00"
        
        # Verify result output
        assert result["id"] == "mock_gcal_id_123"


@pytest.mark.anyio
async def test_sync_event_to_google_calendar_skips_unauthenticated():
    """
    Tests that sync_event_to_google_calendar does not attempt calendar sync
    if the student has not authorized their Google account (missing tokens).
    """
    db_session = MagicMock(spec=AsyncSession)
    
    # Mock user has no access_token/refresh_token
    mock_user = User(id=1, tg_id=12345, google_access_token=None, google_refresh_token=None)
    mock_event = Event(id=10, title="Draft Event", deadline=datetime.now(), description="Context", user=mock_user)
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(GoogleCalendarService, "Calendar", new_callable=AsyncMock) as mock_calendar_call:
        await sync_event_to_google_calendar(event_id=10, db=db_session)
        
        # Verify GoogleCalendarService is NOT triggered
        mock_calendar_call.assert_not_called()


@pytest.mark.anyio
async def test_sync_event_to_google_calendar_triggers_on_authenticated():
    """
    Tests that sync_event_to_google_calendar constructs correct Credentials object
    and triggers the Calendar sync for authenticated students.
    """
    db_session = MagicMock(spec=AsyncSession)
    
    mock_user = User(
        id=1,
        tg_id=12345,
        google_access_token="fake_access_123",
        google_refresh_token="fake_refresh_456"
    )
    mock_event = Event(
        id=10,
        title="Сдача лабы",
        deadline=datetime(2026, 6, 1, 15, 0, 0),
        description="В учебный корпус",
        user=mock_user
    )
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(GoogleCalendarService, "Calendar", new_callable=AsyncMock, return_value={"id": "gcal_event_99"}) as mock_calendar_call:
        await sync_event_to_google_calendar(event_id=10, db=db_session)
        
        # Verify Calendar call arguments
        mock_calendar_call.assert_called_once()
        args, kwargs = mock_calendar_call.call_args
        
        assert kwargs["event_title"] == "Сдача лабы"
        assert kwargs["deadline"] == datetime(2026, 6, 1, 15, 0, 0)
        assert kwargs["description"] == "В учебный корпус"
        
        # Verify Credentials setup
        creds = kwargs["user_credentials"]
        assert isinstance(creds, Credentials)
        assert creds.token == "fake_access_123"
        assert creds.refresh_token == "fake_refresh_456"


def test_google_login_endpoint():
    """
    Tests that GET /google/login generates redirection link successfully
    with custom state embedding Telegram ID.
    """
    client = TestClient(app)
    
    mock_flow = MagicMock()
    mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?state=12345", "state")
    
    with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=mock_flow) as mock_from_config:
        response = client.get("/google/login?tg_id=12345", follow_redirects=False)
        
        # Assert RedirectResponse
        assert response.status_code == 307  # FastAPI Temporary Redirect
        assert "accounts.google.com" in response.headers["location"]
        assert "state=12345" in response.headers["location"]
        mock_from_config.assert_called_once()


def test_google_callback_endpoint_success():
    """
    Tests that GET /google/callback exchanges the code, binds access/refresh tokens
    to the correct database User model, and outputs the premium confirmation card HTML page.
    """
    client = TestClient(app)
    
    mock_credentials = MagicMock()
    mock_credentials.token = "token_secret_abc"
    mock_credentials.refresh_token = "refresh_secret_xyz"
    mock_credentials.expiry = datetime.now()
    
    mock_flow = MagicMock()
    mock_flow.credentials = mock_credentials
    
    mock_user = User(id=1, tg_id=12345, google_access_token=None, google_refresh_token=None)
    
    # Mock database session
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    
    async def override_get_db():
        yield mock_session
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        # Mocking UserService.get_user_by_tg_id
        with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=mock_flow), \
             patch("main.UserService.get_user_by_tg_id", new_callable=AsyncMock, return_value=mock_user):
             
            response = client.get("/google/callback?code=oauth_auth_code_999&state=12345")
            
            # Verify tokens got saved and committed in DB
            assert mock_user.google_access_token == "token_secret_abc"
            assert mock_user.google_refresh_token == "refresh_secret_xyz"
            mock_session.commit.assert_called_once()
            
            # Verify premium styled HTML response card
            assert response.status_code == 200
            assert "Google Календарь подключен!" in response.text
            assert "планиИруй!" in response.text
    finally:
        app.dependency_overrides.clear()
