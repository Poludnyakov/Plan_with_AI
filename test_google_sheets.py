import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession

from models import Event, User
from google.oauth2.credentials import Credentials
from google_sheets_service import GoogleSheetsService, sync_event_to_google_sheets


@pytest.mark.anyio
async def test_google_sheets_service_append():
    """
    Tests that GoogleSheetsService.append_event_to_sheet correctly builds discovery
    client and appends the event row to the spreadsheet inside a worker thread.
    """
    # 1. Setup mock credentials and service
    mock_credentials = MagicMock(spec=Credentials)
    service = GoogleSheetsService(user_credentials=mock_credentials)
    
    deadline_dt = datetime(2026, 6, 1, 15, 30, 0)
    
    # 2. Mock discovery sheets service
    mock_values_resource = MagicMock()
    mock_values_resource.append.return_value.execute.return_value = {"spreadsheetId": "mock_sheet_123"}
    
    mock_spreadsheets_resource = MagicMock()
    mock_spreadsheets_resource.values.return_value = mock_values_resource
    
    mock_client = MagicMock()
    mock_client.spreadsheets.return_value = mock_spreadsheets_resource
    
    with patch("google_sheets_service.build", return_value=mock_client) as mock_build:
        result = await service.append_event_to_sheet(
            user_spreadsheet_id="mock_sheet_123",
            title="Сдача лабораторной работы по ИИ",
            deadline=deadline_dt,
            description="Аудитория 302"
        )
        
        # Verify client build
        mock_build.assert_called_once_with("sheets", "v4", credentials=mock_credentials)
        
        # Verify append parameters
        mock_values_resource.append.assert_called_once()
        args, kwargs = mock_values_resource.append.call_args
        assert kwargs["spreadsheetId"] == "mock_sheet_123"
        assert kwargs["range"] == "A:E"
        assert kwargs["valueInputOption"] == "USER_ENTERED"
        assert kwargs["insertDataOption"] == "INSERT_ROWS"
        
        body = kwargs["body"]
        row = body["values"][0]
        # Check title, deadline format, description and "Активно" status
        assert row[1] == "Сдача лабораторной работы по ИИ"
        assert row[2] == "01.06.2026 15:30"
        assert row[3] == "Аудитория 302"
        assert row[4] == "Активно"
        
        # Verify result spreadsheetId
        assert result["spreadsheetId"] == "mock_sheet_123"


@pytest.mark.anyio
async def test_sync_event_to_google_sheets_skips_if_unlinked():
    """
    Tests that sync_event_to_google_sheets does not attempt Sheets row logging
    if no Spreadsheet ID or Google tokens are present.
    """
    db_session = MagicMock(spec=AsyncSession)
    
    # User has no tokens or spreadsheet ID
    mock_user = User(id=1, tg_id=12345, google_access_token=None, google_spreadsheet_id=None)
    mock_event = Event(id=10, title="Draft Event", deadline=datetime.now(), description="Context", user=mock_user)
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(GoogleSheetsService, "append_event_to_sheet", new_callable=AsyncMock) as mock_append:
        await sync_event_to_google_sheets(event_id=10, db=db_session)
        
        # Ensure Sheets append is skipped
        mock_append.assert_not_called()


@pytest.mark.anyio
async def test_sync_event_to_google_sheets_triggers_on_linked():
    """
    Tests that sync_event_to_google_sheets binds the correct OAuth credentials,
    resolves the Spreadsheet ID, and calls append_event_to_sheet.
    """
    db_session = MagicMock(spec=AsyncSession)
    
    # User has credentials and spreadsheet ID
    mock_user = User(
        id=1,
        tg_id=12345,
        google_access_token="fake_access_123",
        google_refresh_token="fake_refresh_456",
        google_spreadsheet_id="student_sheet_abc"
    )
    mock_event = Event(
        id=10,
        title="Сдача лабораторной по ИИ",
        deadline=datetime(2026, 6, 1, 15, 30, 0),
        description="Сдать отчет в ЛК",
        user=mock_user
    )
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch("google_sheets_service.GoogleSheetsService") as mock_service_class:
        mock_instance = MagicMock()
        mock_instance.append_event_to_sheet = AsyncMock(return_value={"spreadsheetId": "student_sheet_abc"})
        mock_service_class.return_value = mock_instance
        
        await sync_event_to_google_sheets(event_id=10, db=db_session)
        
        # Verify constructor call and credentials
        mock_service_class.assert_called_once()
        called_credentials = mock_service_class.call_args[0][0]
        assert called_credentials.token == "fake_access_123"
        assert called_credentials.refresh_token == "fake_refresh_456"
        
        # Verify append_event_to_sheet parameters
        mock_instance.append_event_to_sheet.assert_called_once()
        args, kwargs = mock_instance.append_event_to_sheet.call_args
        
        assert kwargs["user_spreadsheet_id"] == "student_sheet_abc"
        assert kwargs["title"] == "Сдача лабораторной по ИИ"
        assert kwargs["deadline"] == datetime(2026, 6, 1, 15, 30, 0)
        assert kwargs["description"] == "Сдать отчет в ЛК"
