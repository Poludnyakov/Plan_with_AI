import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message, User as TGUser, Chat, CallbackQuery, PhotoSize
from handlers.user import cmd_start, cmd_list, cmd_calendar
from handlers.pipeline_handlers import handle_confirm_callback, handle_cancel_callback, handle_voice_input, handle_complete_event_callback, handle_photo_input
from models import User, Event, EventStatus, ReminderStatus
from repositories import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

def datetime_mock():
    from datetime import datetime
    return datetime.now()


@pytest.mark.anyio
async def test_cmd_start_new_user():
    """
    Tests /start command when a new Telegram user registers.
    Verifies that they are persisted to the database and get a welcome message.
    """
    # Mock message
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир", username="vladimir")
    mock_chat = Chat(id=999222111, type="private")
    message = Message(message_id=1, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="/start")
    
    # Mock DB Session
    db_session = MagicMock(spec=AsyncSession)
    
    # Mock UserRepository methods and Message.answer method at class level
    with patch("repositories.UserRepository.get_by_tg_id", new_callable=AsyncMock) as mock_get, \
         patch("repositories.UserRepository.create", new_callable=AsyncMock) as mock_create, \
         patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
        
        mock_get.return_value = None  # New user
        mock_created_user = User(id=1, tg_id=999222111, timezone="Europe/Moscow")
        mock_create.return_value = mock_created_user
        
        await cmd_start(message, db_session)
        
        # Verify user repository interactions
        mock_get.assert_called_once_with(999222111)
        mock_create.assert_called_once_with(tg_id=999222111, timezone="Europe/Moscow")
        db_session.commit.assert_called_once()
        
        # Verify message response
        assert mock_answer.call_count == 2
        
        # Verify first call (greeting + persistent reply markup keyboard)
        first_call_args = mock_answer.call_args_list[0][0]
        assert "Привет, Владимир!" in first_call_args[0]
        assert "планиИруй!" in first_call_args[0]


@pytest.mark.anyio
async def test_handle_confirm_callback_success():
    """
    Tests callback handler for Event confirmation.
    Verifies that the status changes to CONFIRMED, database commits, and card text updates.
    """
    # Mock callback query
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=10, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Draft event card")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="confirm:42"
    )
    
    db_session = MagicMock(spec=AsyncSession)
    
    # Mock database execute result for Event fetch
    mock_event = Event(id=42, title="Лекция по физике", status=EventStatus.DRAFT)
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "edit_text", new_callable=AsyncMock) as mock_edit_text, \
         patch("yandex_calendar_service.YandexCalendarService") as MockYandexCalendarService:
         
        mock_service_instance = MockYandexCalendarService.return_value
        mock_service_instance.add_deadline_to_yandex = AsyncMock()
         
        await handle_confirm_callback(callback, db_session)
        
        # Assert event status successfully updated
        assert mock_event.status == EventStatus.CONFIRMED
        db_session.commit.assert_called_once()
        
        # Assert callback answered and message updated
        mock_answer.assert_called_once_with("✅ Подтверждено!")
        mock_edit_text.assert_called_once()
        args, kwargs = mock_edit_text.call_args
        assert "Лекция по физике" in args[0]
        assert "добавлена в календарь" in args[0]


@pytest.mark.anyio
async def test_handle_cancel_callback_success():
    """
    Tests callback handler for Event cancellation.
    Verifies that the draft event is deleted from the DB and message text is updated to cancelled.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=10, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Draft event card")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="cancel:42"
    )
    
    db_session = MagicMock(spec=AsyncSession)
    
    mock_event = Event(id=42, title="Лекция по физике", status=EventStatus.DRAFT)
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "edit_text", new_callable=AsyncMock) as mock_edit_text:
         
        await handle_cancel_callback(callback, db_session)
        
        # Assert event got deleted and committed
        db_session.delete.assert_called_once_with(mock_event)
        db_session.commit.assert_called_once()
        
        mock_answer.assert_called_once_with("❌ Отменено")
        mock_edit_text.assert_called_once_with("❌ *Добавление отменено.*", parse_mode="Markdown")


@pytest.mark.anyio
async def test_handle_voice_input_pipeline_failure():
    """
    Tests that if handle_voice_input transcribes voice successfully,
    but process_text_input fails, the bot replies with the custom error message.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    
    mock_voice = {
        "file_id": "mock_file_id_123",
        "file_unique_id": "mock_unique_id_123",
        "duration": 5
    }
    mock_bot = MagicMock()
    mock_bot.download = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    
    message = Message(
        message_id=10, 
        date=datetime_mock(), 
        chat=mock_chat, 
        from_user=mock_user, 
        voice=mock_voice
    ).as_(mock_bot)
    
    db_session = MagicMock(spec=AsyncSession)
    
    with patch("handlers.pipeline_handlers.pipeline_service.speechkit_service.transcribe_voice", new_callable=AsyncMock) as mock_transcribe, \
         patch("handlers.pipeline_handlers.pipeline_service.process_text_input", new_callable=AsyncMock) as mock_process_text, \
         patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
         
        mock_transcribe.return_value = "лекция в пятницу"
        mock_process_text.side_effect = ValueError("Simulated pipeline error")
        
        await handle_voice_input(message, db_session)
        
        mock_transcribe.assert_called_once()
        mock_process_text.assert_called_once_with(
            user_tg_id=999222111,
            raw_text="лекция в пятницу",
            db_session=db_session
        )
        
        mock_answer.assert_called_once()
        error_msg = mock_answer.call_args[0][0]
        assert "❌ Произошла ошибка при обработке этого запроса." in error_msg
        assert "Текст вашего сообщения: 'лекция в пятницу'" in error_msg
        assert "Попробуйте перефразировать или добавить задачу текстом." in error_msg


@pytest.mark.anyio
async def test_handle_voice_input_transcribe_failure():
    """
    Tests that if handle_voice_input fails during voice transcription,
    the bot replies with the generic error message.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    
    mock_voice = {
        "file_id": "mock_file_id_123",
        "file_unique_id": "mock_unique_id_123",
        "duration": 5
    }
    mock_bot = MagicMock()
    mock_bot.download = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    
    message = Message(
        message_id=10, 
        date=datetime_mock(), 
        chat=mock_chat, 
        from_user=mock_user, 
        voice=mock_voice
    ).as_(mock_bot)
    
    db_session = MagicMock(spec=AsyncSession)
    
    with patch("handlers.pipeline_handlers.pipeline_service.speechkit_service.transcribe_voice", new_callable=AsyncMock) as mock_transcribe, \
         patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
         
        mock_transcribe.return_value = ""
        
        await handle_voice_input(message, db_session)
        
        mock_transcribe.assert_called_once()
        mock_answer.assert_called_once_with("❌ Произошла ошибка при обработке голосового сообщения. Попробуй еще раз!")


@pytest.mark.anyio
async def test_handle_complete_event_callback_success():
    """
    Tests callback handler for Event completion (is_completed = True).
    Verifies that the is_completed attribute becomes True, database commits,
    and push card text is updated to reflect the event is marked completed.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=10, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Reminder push card")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="complete_event:42"
    )
    
    db_session = MagicMock(spec=AsyncSession)
    
    # Mock event
    mock_event = Event(id=42, title="Подготовка к экзамену", status=EventStatus.CONFIRMED, is_completed=False)
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "edit_text", new_callable=AsyncMock) as mock_edit_text:
         
        await handle_complete_event_callback(callback, db_session)
        
        # Assert database updates
        assert mock_event.is_completed is True
        db_session.commit.assert_called_once()
        
        # Assert callback is answered and text is edited
        mock_answer.assert_called_once_with("✅ Задача выполнена!")
        mock_edit_text.assert_called_once()
        args, kwargs = mock_edit_text.call_args
        assert "Подготовка к экзамену" in args[0]
        assert "отмечена как выполненная" in args[0]


@pytest.mark.anyio
async def test_cmd_list():
    """
    Tests /list command.
    Verifies that the bot replies with the beautiful dashboard URL link and the correct markup button.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    message = Message(message_id=1, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="/list")
    
    with patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
        await cmd_list(message)
        
        mock_answer.assert_called_once()
        args, kwargs = mock_answer.call_args
        
        # Verify text containing HTML link
        assert "Ваша персональная таблица дедлайнов готова!" in args[0]
        assert "http://localhost:8000/dashboard" in args[0]
        assert "<a href=" in args[0]
        
        # Verify message parameters
        assert kwargs.get("parse_mode") == "HTML"
        assert kwargs.get("disable_web_page_preview") is True


@pytest.mark.anyio
async def test_cmd_calendar():
    """
    Tests /calendar command.
    Verifies that the bot replies with the active calendar grid text and direct Markdown links.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    message = Message(message_id=1, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="/calendar")
    
    with patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
        await cmd_calendar(message)
        
        mock_answer.assert_called_once()
        args, kwargs = mock_answer.call_args
        
        # Verify text containing styled links
        assert "календарь готов" in args[0]
        assert "http://localhost:8000/calendar" in args[0]
        
        # Verify message parameters
        assert kwargs.get("parse_mode") == "Markdown"
        assert kwargs.get("disable_web_page_preview") is True


@pytest.mark.anyio
async def test_handle_confirm_callback_conflict():
    """
    Tests callback handler for Event confirmation when there is a scheduling conflict.
    Verifies that it answers the callback with a conflict alert and updates the message text to describe the conflict,
    and does not set status to confirmed or commit the transaction.
    """
    from datetime import datetime, timezone
    
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=10, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Draft event card")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="confirm:42"
    )
    
    db_session = MagicMock(spec=AsyncSession)
    
    deadline = datetime.now(timezone.utc)
    mock_event = Event(id=42, user_id=1, title="Лекция по физике", deadline=deadline, status=EventStatus.DRAFT)
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    conflicting_event = Event(id=99, user_id=1, title="Сдача ИИ", deadline=deadline, status=EventStatus.CONFIRMED)
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "edit_text", new_callable=AsyncMock) as mock_edit_text, \
         patch("repositories.EventRepository.get_conflicting_event", new_callable=AsyncMock) as mock_get_conflicting:
         
        mock_get_conflicting.return_value = conflicting_event
         
        await handle_confirm_callback(callback, db_session)
        
        # Assert event status NOT changed to confirmed and NOT committed
        assert mock_event.status == EventStatus.DRAFT
        db_session.commit.assert_not_called()
        
        # Assert callback answered with alert and message updated with conflict text
        mock_answer.assert_called_once_with("⚠️ Конфликт расписания!", show_alert=True)
        mock_edit_text.assert_called_once()
        args, kwargs = mock_edit_text.call_args
        assert "Конфликт расписания" in args[0]
        assert "Сдача ИИ" in args[0]
        assert "выберите другое время" in args[0]


@pytest.mark.anyio
async def test_handle_photo_input_success():
    """
    Tests that handle_photo_input successfully downloads the photo,
    calls Yandex OCR text recognition, parses via the pipeline,
    and returns styled confirmation draft cards.
    """
    from datetime import datetime, timezone
    
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    
    mock_photo_size = PhotoSize(file_id="mock_photo_id_123", file_unique_id="mock_unique_id", width=800, height=600)
    
    mock_bot = MagicMock()
    mock_bot.download = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    
    message = Message(
        message_id=10,
        date=datetime_mock(),
        chat=mock_chat,
        from_user=mock_user,
        photo=[mock_photo_size]
    ).as_(mock_bot)
    
    db_session = MagicMock(spec=AsyncSession)
    
    # Mock draft event
    deadline = datetime.now(timezone.utc)
    mock_event = Event(
        id=42, 
        user_id=1, 
        title="Сдача лабораторной работы по ИИ", 
        deadline=deadline, 
        status=EventStatus.DRAFT,
        reminders=[]
    )
    
    with patch("handlers.pipeline_handlers.pipeline_service.process_image_input", new_callable=AsyncMock) as mock_process_image, \
         patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
         
        mock_process_image.return_value = [mock_event]
        
        await handle_photo_input(message, db_session)
        
        mock_process_image.assert_called_once_with(
            user_tg_id=999222111,
            image_bytes=b"",
            db_session=db_session
        )
        
        mock_answer.assert_called_once()
        args, kwargs = mock_answer.call_args
        assert "Найдено новое событие из расписания!" in args[0]
        assert "Сдача лабораторной работы по ИИ" in args[0]
        assert "Всё верно? Подтвердите добавление в план." in args[0]



@pytest.mark.anyio
async def test_handle_photo_input_ocr_failure():
    """
    Tests that if handle_photo_input fails to extract text from photo,
    it responds with a helpful image clarification warning.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    
    mock_photo_size = PhotoSize(file_id="mock_photo_id_123", file_unique_id="mock_unique_id", width=800, height=600)
    
    mock_bot = MagicMock()
    mock_bot.download = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    
    message = Message(
        message_id=10,
        date=datetime_mock(),
        chat=mock_chat,
        from_user=mock_user,
        photo=[mock_photo_size]
    ).as_(mock_bot)
    
    db_session = MagicMock(spec=AsyncSession)
    
    with patch("handlers.pipeline_handlers.pipeline_service.process_image_input", new_callable=AsyncMock) as mock_process_image, \
         patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
         
        mock_process_image.side_effect = ValueError("Multimodal parsing mock error")
        
        await handle_photo_input(message, db_session)
        
        mock_process_image.assert_called_once()
        mock_answer.assert_called_once()
        args, kwargs = mock_answer.call_args
        assert "Мне не удалось распознать расписание на этом изображении" in args[0]
        assert "убедитесь, что текст на картинке четкий" in args[0]


