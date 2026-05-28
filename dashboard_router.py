import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User, Event, EventStatus, ReminderStatus
from repositories import EventRepository, ReminderRepository

logger = logging.getLogger("DashboardRouter")

router = APIRouter(tags=["Dashboard"])

# Initialize templates inside the router module for high portability
templates = Jinja2Templates(directory="templates")


@router.get("/dashboard/{user_tg_id}", response_class=HTMLResponse, summary="Render student schedule dashboard")
async def get_dashboard(request: Request, user_tg_id: int, db: AsyncSession = Depends(get_db)):
    """
    Renders an interactive web dashboard for a student, displaying all confirmed tasks
    ordered by their deadline (nearest first).
    """
    try:
        # 1. Retrieve the student user by Telegram ID
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            # Render a premium glassmorphic error page if the student is not registered
            logger.warning(f"Dashboard requested for unregistered Telegram ID: {user_tg_id}")
            return templates.TemplateResponse(
                request=request,
                name="dashboard.html",
                context={
                    "request": request,
                    "error": f"Студент с Telegram ID {user_tg_id} не зарегистрирован в системе.",
                    "user_tg_id": user_tg_id,
                    "events": []
                },
                status_code=status.HTTP_404_NOT_FOUND
            )

        # 2. Query all confirmed events, sorted by upcoming deadline
        events_result = await db.execute(
            select(Event)
            .filter(Event.user_id == user.id, Event.status == EventStatus.CONFIRMED)
            .order_by(Event.deadline.asc())
        )
        events = events_result.scalars().all()
        
        logger.info(f"Loaded {len(events)} confirmed events for user tg_id={user_tg_id}")
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "request": request,
                "user": user,
                "user_tg_id": user_tg_id,
                "events": events
            }
        )
    except Exception as e:
        logger.error(f"Error rendering dashboard for tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка при загрузке дашборда."
        )


@router.post("/events/{event_id}/toggle-complete", summary="Toggle event completion state")
async def toggle_event_complete(event_id: int, db: AsyncSession = Depends(get_db)):
    """
    Asynchronously toggles the is_completed status of a specific event in the database.
    Returns the new state in a JSON response for AJAX integration.
    """
    try:
        event_result = await db.execute(select(Event).filter(Event.id == event_id))
        event = event_result.scalar_one_or_none()
        
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Событие с ID {event_id} не найдено."
            )
            
        # Toggle completion
        event.is_completed = not event.is_completed
        await db.commit()
        await db.refresh(event)
        
        logger.info(f"Toggled event ID {event_id} completion to {event.is_completed}")
        return {
            "status": "success",
            "event_id": event.id,
            "is_completed": event.is_completed
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling event ID {event_id} status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось обновить статус события."
        )


@router.delete("/api/events/{event_id}", summary="Delete an event manually")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    """
    Asynchronously deletes a specific event from the database.
    This automatically cascades to delete all linked pending reminders.
    """
    try:
        event_result = await db.execute(select(Event).filter(Event.id == event_id))
        event = event_result.scalar_one_or_none()
        
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Событие с ID {event_id} не найдено."
            )
            
        event_title = event.title
        await db.delete(event)
        await db.commit()
        
        logger.info(f"Successfully deleted event ID {event_id}")
        return {
            "status": "success",
            "message": f"Событие '{event_title}' успешно удалено."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting event ID {event_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось удалить событие."
        )


@router.get("/api/events/{user_tg_id}", summary="Get student events in FullCalendar format")
async def get_events_json(user_tg_id: int, db: AsyncSession = Depends(get_db)):
    """
    Returns a list of all confirmed events for a user, formatted specifically
    for direct integration with FullCalendar.
    """
    try:
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Студент с Telegram ID {user_tg_id} не найден."
            )
            
        events_result = await db.execute(
            select(Event)
            .filter(Event.user_id == user.id, Event.status == EventStatus.CONFIRMED)
        )
        events = events_result.scalars().all()
        
        payload = []
        for event in events:
            payload.append({
                "id": event.id,
                "title": event.title,
                "start": event.deadline.isoformat(),
                "description": event.description or "",
                "is_completed": event.is_completed,
                "color": "#4CAF50" if event.is_completed else "#7B2CBF"
            })
            
        logger.info(f"Delivered {len(payload)} calendar JSON events for tg_id={user_tg_id}")
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving calendar JSON events for tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось загрузить события для календаря."
        )


@router.get("/calendar/{user_tg_id}", response_class=HTMLResponse, summary="Render student schedule visual calendar")
async def get_calendar_page(request: Request, user_tg_id: int, db: AsyncSession = Depends(get_db)):
    """
    Renders the visual calendar dashboard page templates/calendar.html.
    """
    try:
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            logger.warning(f"Calendar page requested for unregistered Telegram ID: {user_tg_id}")
            return templates.TemplateResponse(
                request=request,
                name="calendar.html",
                context={
                    "request": request,
                    "error": f"Студент с Telegram ID {user_tg_id} не зарегистрирован в системе.",
                    "user_tg_id": user_tg_id
                },
                status_code=status.HTTP_404_NOT_FOUND
            )

        logger.info(f"Rendering visual calendar page for user tg_id={user_tg_id}")
        return templates.TemplateResponse(
            request=request,
            name="calendar.html",
            context={
                "request": request,
                "user": user,
                "user_tg_id": user_tg_id
            }
        )
    except Exception as e:
        logger.error(f"Error rendering calendar page for tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка при загрузке календаря."
        )


class EventManualCreate(BaseModel):
    title: str = Field(..., max_length=255)
    description: Optional[str] = None
    deadline: datetime


@router.post("/api/events/{user_tg_id}", summary="Create a new event manually with automatic reminders and calendar sync")
async def create_event_manually(user_tg_id: int, event_in: EventManualCreate, db: AsyncSession = Depends(get_db)):
    """
    Creates a confirmed event manually, automatically calculates the 5 pre-emptive reminders
    (excluding any that are in the past relative to creation time), and triggers Yandex Calendar CalDAV sync.
    """
    try:
        # Retrieve user
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Студент с Telegram ID {user_tg_id} не найден."
            )
            
        # Create confirmed event
        event_repo = EventRepository(db)
        reminder_repo = ReminderRepository(db)
        
        # Check for scheduling conflicts
        conflicting_event = await event_repo.get_conflicting_event(
            user_id=user.id,
            deadline=event_in.deadline
        )
        if conflicting_event:
            display_time = conflicting_event.deadline.strftime('%d.%m.%Y в %H:%M')
            try:
                import pytz
                if user.timezone:
                    tz = pytz.timezone(user.timezone)
                    display_deadline = conflicting_event.deadline
                    if display_deadline.tzinfo is not None:
                        display_deadline = display_deadline.astimezone(tz)
                    else:
                        display_deadline = pytz.utc.localize(display_deadline).astimezone(tz)
                    display_time = display_deadline.strftime('%d.%m.%Y в %H:%M')
            except Exception:
                pass
                
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Этот временной интервал пересекается с уже забронированной задачей '{conflicting_event.title}' ({display_time})"
            )
        
        db_event = await event_repo.create(
            user_id=user.id,
            title=event_in.title,
            description=event_in.description,
            deadline=event_in.deadline,
            status=EventStatus.CONFIRMED
        )
        await db.flush()
        
        # Calculate pre-emptive reminders
        intervals = [
            timedelta(hours=24),
            timedelta(hours=12),
            timedelta(hours=1),
            timedelta(minutes=30),
            timedelta(minutes=15)
        ]
        now = datetime.now(timezone.utc)
        
        # Ensure deadline has tzinfo for timezone-safe comparison
        deadline_utc = event_in.deadline
        if deadline_utc.tzinfo is None:
            deadline_utc = deadline_utc.replace(tzinfo=timezone.utc)
        else:
            deadline_utc = deadline_utc.astimezone(timezone.utc)
            
        time_to_deadline = deadline_utc - now
        
        # If deadline is less than 24 hours away (and still in the future), schedule an immediate reminder
        if time_to_deadline < timedelta(hours=24) and time_to_deadline > timedelta(0):
            await reminder_repo.create(
                event_id=db_event.id,
                remind_at=now,
                status=ReminderStatus.PENDING
            )
            logger.info(f"Scheduled immediate reminder for near-term event {db_event.id} (time to deadline: {time_to_deadline})")
            
        for interval in intervals:
            remind_time = deadline_utc - interval
            if remind_time > now:
                await reminder_repo.create(
                    event_id=db_event.id,
                    remind_at=remind_time,
                    status=ReminderStatus.PENDING
                )
                
        await db.commit()
        await db.refresh(db_event)
        
        # Call Yandex CalDAV sync if credentials are set
        try:
            from yandex_calendar_service import YandexCalendarService
            await YandexCalendarService().add_deadline_to_yandex(
                title=db_event.title,
                deadline=db_event.deadline,
                description=db_event.description
            )
            logger.info(f"Successfully synced event {db_event.id} to Yandex Calendar")
        except Exception as sync_err:
            logger.warning(f"Failed to sync manual event to Yandex Calendar: {sync_err}", exc_info=True)
            
        logger.info(f"Manually created confirmed event {db_event.id} for user {user_tg_id}")
        return {
            "status": "success",
            "event_id": db_event.id,
            "title": db_event.title,
            "deadline": db_event.deadline.isoformat(),
            "description": db_event.description or ""
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating manual event for user {user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось создать событие."
        )
