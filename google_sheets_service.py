import asyncio
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from config import settings
from models import Event, User

logger = logging.getLogger("GoogleSheetsService")

class GoogleSheetsService:
    """
    Coordinates interactions with the Google Sheets API.
    All synchronous API calls are executed in worker threads to prevent blocking.
    """
    def __init__(self, user_credentials: Credentials):
        self.credentials = user_credentials

    async def append_event_to_sheet(
        self,
        user_spreadsheet_id: str,
        title: str,
        deadline: datetime,
        description: Optional[str] = None
    ) -> dict:
        """
        Appends event details as a row in the specified Google Spreadsheet.
        Row structure: [Дата добавления] | [Название задачи] | [Дата и время дедлайна] | [Описание / Контекст] | ["Активно"]
        Uses asyncio.to_thread to execute Sheets v4 append.
        """
        # Get local time representation
        added_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        deadline_str = deadline.strftime("%d.%m.%Y %H:%M")
        
        row_values = [
            [
                added_at_str,
                title,
                deadline_str,
                description or '',
                "Активно"
            ]
        ]
        
        body = {
            'values': row_values
        }
        
        def run_append():
            # Build sheets client inside thread
            service = build('sheets', 'v4', credentials=self.credentials)
            
            # Using A:E (or Sheet1!A:E fallback) to robustly write to the active sheet
            # regardless of whether the student's sheet is in Russian (Лист1) or English (Sheet1).
            # To be absolutely sure, we use A:E which targets the first sheet.
            try:
                return service.spreadsheets().values().append(
                    spreadsheetId=user_spreadsheet_id,
                    range='A:E',
                    valueInputOption='USER_ENTERED',
                    insertDataOption='INSERT_ROWS',
                    body=body
                ).execute()
            except Exception as e:
                logger.warning(f"Failed to append to active range 'A:E': {e}. Attempting fallback range 'Sheet1!A:E'.")
                # Try fallback Sheet1
                return service.spreadsheets().values().append(
                    spreadsheetId=user_spreadsheet_id,
                    range='Sheet1!A:E',
                    valueInputOption='USER_ENTERED',
                    insertDataOption='INSERT_ROWS',
                    body=body
                ).execute()
                
        logger.info(f"Scheduling Google Sheets append row for spreadsheetId: {user_spreadsheet_id}")
        return await asyncio.to_thread(run_append)


async def sync_event_to_google_sheets(event_id: int, db: AsyncSession) -> None:
    """
    Service helper function checking if the user has a spreadsheet configured
    (either personal or fallback global master). If so, appends event to the sheet.
    """
    try:
        # Load event eagerly with user relation
        result = await db.execute(
            select(Event)
            .filter(Event.id == event_id)
            .options(selectinload(Event.user))
        )
        event = result.scalar_one_or_none()
        if not event:
            logger.warning(f"Event ID {event_id} not found. Skipping Google Sheets sync.")
            return
            
        user = event.user
        
        # Decide which spreadsheet ID to use (personal vs global fallback)
        spreadsheet_id = user.google_spreadsheet_id or settings.GOOGLE_SPREADSHEET_ID
        if not spreadsheet_id:
            logger.info(f"No Google Spreadsheet ID configured for user tg_id={user.tg_id} or fallback settings. Skipping Sheets sync.")
            return
            
        # Check OAuth credentials
        if not user.google_access_token or not user.google_refresh_token:
            logger.info(f"User tg_id={user.tg_id} has not linked Google OAuth. Skipping Sheets sync.")
            return
            
        # Construct Google OAuth2 Credentials
        credentials = Credentials(
            token=user.google_access_token,
            refresh_token=user.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID or "dummy-client-id",
            client_secret=settings.GOOGLE_CLIENT_SECRET or "dummy-client-secret"
        )
        
        sheets_service = GoogleSheetsService(credentials)
        result_payload = await sheets_service.append_event_to_sheet(
            user_spreadsheet_id=spreadsheet_id,
            title=event.title,
            deadline=event.deadline,
            description=event.description
        )
        logger.info(f"Successfully appended row to Google Spreadsheet ID {spreadsheet_id} for event_id={event.id}. Response: {result_payload}")
    except Exception as e:
        logger.error(f"Failed to synchronize event_id={event_id} to Google Sheets: {e}", exc_info=True)
