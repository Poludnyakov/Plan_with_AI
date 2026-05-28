import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from config import settings
from models import Event, User

logger = logging.getLogger("GoogleCalendarService")

class GoogleCalendarService:
    """
    Coordinates interactions with the Google Calendar API.
    All synchronous API calls are executed in worker threads to prevent blocking.
    """
    async def Calendar(
        self,
        user_credentials: Credentials,
        event_title: str,
        deadline: datetime,
        description: Optional[str] = None
    ) -> dict:
        """
        Creates an event in the user's primary Google Calendar.
        The event starts at the deadline and lasts for 30 minutes.
        Uses asyncio.to_thread to run build(...) and insert(...).execute() in a worker thread.
        """
        # Form start time and end time (30 minutes duration)
        start_time = deadline.isoformat()
        end_time = (deadline + timedelta(minutes=30)).isoformat()
        
        event_body = {
            'summary': event_title,
            'description': description or '',
            'start': {
                'dateTime': start_time,
                'timeZone': str(deadline.tzinfo) if deadline.tzinfo else 'Europe/Moscow',
            },
            'end': {
                'dateTime': end_time,
                'timeZone': str(deadline.tzinfo) if deadline.tzinfo else 'Europe/Moscow',
            },
        }
        
        def run_insert():
            # Build client inside thread
            service = build('calendar', 'v3', credentials=user_credentials)
            return service.events().insert(
                calendarId='primary',
                body=event_body
            ).execute()
            
        logger.info(f"Scheduling Google Calendar API call for event summary: {event_title}")
        return await asyncio.to_thread(run_insert)


async def sync_event_to_google_calendar(event_id: int, db: AsyncSession) -> None:
    """
    Service helper function checking if the user has authenticated with Google Calendar.
    If authenticated, constructs their credentials and inserts the event into their Google Calendar.
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
            logger.warning(f"Event ID {event_id} not found. Skipping Google Calendar sync.")
            return
            
        user = event.user
        if not user.google_access_token or not user.google_refresh_token:
            logger.info(f"User tg_id={user.tg_id} has not linked Google Calendar. Skipping calendar sync.")
            return
            
        # Construct Google OAuth2 Credentials
        credentials = Credentials(
            token=user.google_access_token,
            refresh_token=user.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID or "dummy-client-id",
            client_secret=settings.GOOGLE_CLIENT_SECRET or "dummy-client-secret"
        )
        
        calendar_service = GoogleCalendarService()
        result_payload = await calendar_service.Calendar(
            user_credentials=credentials,
            event_title=event.title,
            deadline=event.deadline,
            description=event.description
        )
        logger.info(f"Successfully created Google Calendar event for event_id={event.id}. Response: {result_payload}")
    except Exception as e:
        logger.error(f"Failed to synchronize event_id={event_id} to Google Calendar: {e}", exc_info=True)
