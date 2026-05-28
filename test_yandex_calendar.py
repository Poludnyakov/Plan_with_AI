import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import CallbackQuery, Message

from config import settings
from models import Event, User, EventStatus
from yandex_calendar_service import YandexCalendarService
from handlers.pipeline_handlers import handle_confirm_callback
from services import EventService
from repositories import EventRepository

@pytest.mark.anyio
async def test_yandex_calendar_service_success():
    """
    Verifies that YandexCalendarService.add_deadline_to_yandex successfully
    connects to CalDAV via DAVClient, retrieves the first calendar, constructs
    the iCalendar object using vobject, and calls save_event.
    """
    # Override settings for the test
    with patch.object(settings, "YANDEX_EMAIL", "test-email@yandex.ru"), \
         patch.object(settings, "YANDEX_APP_PASSWORD", "test-app-pass"):
        
        service = YandexCalendarService()
        deadline_dt = datetime(2026, 6, 1, 15, 30, 0, tzinfo=timezone.utc)

        # Mock the caldav structure
        mock_client = MagicMock()
        mock_principal = MagicMock()
        mock_calendar = MagicMock()
        mock_calendar.name = "Основной календарь"
        mock_calendar.url = "https://caldav.yandex.ru/test-url"

        mock_client.principal.return_value = mock_principal
        mock_principal.calendars.return_value = [mock_calendar]

        with patch("caldav.DAVClient", return_value=mock_client) as mock_dav_client:
            await service.add_deadline_to_yandex(
                title="Сдача лабораторной",
                deadline=deadline_dt,
                description="В корпусе ГК"
            )

            # Assert DAVClient was correctly initialized
            mock_dav_client.assert_called_once_with(
                url="https://caldav.yandex.ru",
                username="test-email@yandex.ru",
                password="test-app-pass"
            )
            mock_principal.calendars.assert_called_once()
            
            # Assert save_event was called on the primary calendar
            mock_calendar.save_event.assert_called_once()
            
            # Retrieve the ics string sent to save_event
            kwargs = mock_calendar.save_event.call_args.kwargs
            ical_content = kwargs.get("ical") or mock_calendar.save_event.call_args.args[0]
            
            assert "📝 ДЕДЛАЙН: Сдача лабораторной" in ical_content
            assert "В корпусе ГК" in ical_content
            assert "VEVENT" in ical_content


@pytest.mark.anyio
async def test_yandex_calendar_service_skips_if_no_creds():
    """
    Verifies that YandexCalendarService skips synchronization if Yandex email
    or application password is not configured in settings.
    """
    with patch.object(settings, "YANDEX_EMAIL", None), \
         patch.object(settings, "YANDEX_APP_PASSWORD", "test-pass"):
        
        service = YandexCalendarService()
        with patch("caldav.DAVClient") as mock_dav_client:
            await service.add_deadline_to_yandex(
                title="Сдача лабораторной",
                deadline=datetime.now()
            )
            mock_dav_client.assert_not_called()

    with patch.object(settings, "YANDEX_EMAIL", "test@yandex.ru"), \
         patch.object(settings, "YANDEX_APP_PASSWORD", None):
        
        service = YandexCalendarService()
        with patch("caldav.DAVClient") as mock_dav_client:
            await service.add_deadline_to_yandex(
                title="Сдача лабораторной",
                deadline=datetime.now()
            )
            mock_dav_client.assert_not_called()


@pytest.mark.anyio
async def test_pipeline_confirm_calls_yandex_sync():
    """
    Tests that handle_confirm_callback properly sets the event status to confirmed
    and triggers YandexCalendarService.add_deadline_to_yandex with correct arguments.
    """
    db_session = MagicMock(spec=AsyncSession)
    
    mock_event = Event(
        id=42,
        title="Защита диплома",
        deadline=datetime(2026, 6, 15, 10, 0, 0),
        description="Конференц-зал",
        status=EventStatus.DRAFT,
        reminders=[]
    )
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    db_session.commit = AsyncMock()

    # Mock the callback query
    mock_message = MagicMock(spec=Message)
    mock_message.edit_text = AsyncMock()
    
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "confirm:42"
    callback.message = mock_message
    callback.answer = AsyncMock()

    with patch("yandex_calendar_service.YandexCalendarService") as MockYandexCalendarService:
        mock_service_instance = MockYandexCalendarService.return_value
        mock_service_instance.add_deadline_to_yandex = AsyncMock()

        await handle_confirm_callback(callback, db_session)
        
        # Verify status updated
        assert mock_event.status == EventStatus.CONFIRMED
        db_session.commit.assert_called_once()
        callback.answer.assert_called_once_with("✅ Подтверждено!")
        
        # Verify Yandex service trigger
        mock_service_instance.add_deadline_to_yandex.assert_called_once_with(
            title="Защита диплома",
            deadline=datetime(2026, 6, 15, 10, 0, 0),
            description="Конференц-зал"
        )


@pytest.mark.anyio
async def test_services_update_event_calls_yandex_sync():
    """
    Tests that EventService.update_event invokes YandexCalendarService.add_deadline_to_yandex
    when the event transitions to a CONFIRMED status.
    """
    mock_db = MagicMock(spec=AsyncSession)
    mock_event_repo = MagicMock(spec=EventRepository)
    
    mock_event = Event(
        id=77,
        title="Тест-кейсы",
        deadline=datetime(2026, 6, 2, 9, 0, 0),
        description="Валидация CalDAV",
        status=EventStatus.CONFIRMED
    )
    
    mock_event_repo.get_event_with_reminders = AsyncMock(return_value=mock_event)
    mock_event_repo.update = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    from schemas import EventUpdate
    event_in = EventUpdate(status=EventStatus.CONFIRMED)
    
    service = EventService(db=mock_db)
    service.event_repo = mock_event_repo
    
    with patch("yandex_calendar_service.YandexCalendarService") as MockYandexCalendarService:
        mock_service_instance = MockYandexCalendarService.return_value
        mock_service_instance.add_deadline_to_yandex = AsyncMock()

        await service.update_event(event_id=77, event_in=event_in)
        
        mock_event_repo.update.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once()
        
        mock_service_instance.add_deadline_to_yandex.assert_called_once_with(
            title="Тест-кейсы",
            deadline=datetime(2026, 6, 2, 9, 0, 0),
            description="Валидация CalDAV"
        )
