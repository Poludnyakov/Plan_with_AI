import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from aiogram.types import Message, User as TGUser, Chat, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import Event, Reminder, EventStatus, ReminderStatus
from handlers.pipeline_handlers import (
    AddReminderStates,
    handle_add_reminder_init,
    handle_cancel_add_reminder_callback,
    handle_cancel_add_reminder_message,
    handle_reminder_datetime_input
)

def datetime_mock():
    from datetime import datetime
    return datetime.now()


@pytest.mark.anyio
async def test_handle_add_reminder_init_success():
    """
    Tests that handle_add_reminder_init correctly transitions user state
    to AddReminderStates.waiting_for_reminder_datetime, saves event details,
    and prompts the user with the input format instruction.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=10, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Draft event card")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="add_reminder_init:42"
    )
    
    # Mock database session & return mock event
    db_session = MagicMock(spec=AsyncSession)
    mock_event = Event(id=42, title="Лекция по физике", status=EventStatus.DRAFT)
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    # Mock state
    state = MagicMock(spec=FSMContext)
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    
    mock_prompt_msg = Message(message_id=15, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Prompt message")
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "answer", new_callable=AsyncMock, return_value=mock_prompt_msg) as mock_msg_answer:
         
        await handle_add_reminder_init(callback, state, db_session)
        
        # Verify FSM transitions & saved data
        state.set_state.assert_called_once_with(AddReminderStates.waiting_for_reminder_datetime)
        state.update_data.assert_any_call(event_id=42, card_message_id=10)
        state.update_data.assert_any_call(prompt_message_id=15)
        
        # Verify prompt presentation
        mock_answer.assert_called_once()
        mock_msg_answer.assert_called_once()
        args, kwargs = mock_msg_answer.call_args
        assert "Добавление напоминания" in args[0]
        assert "ДД.ММ.ГГГГ ЧЧ:ММ" in args[0]


@pytest.mark.anyio
async def test_handle_cancel_add_reminder_callback():
    """
    Tests that handle_cancel_add_reminder_callback correctly clears FSM state
    and deletes the bot's prompt message.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    mock_message = Message(message_id=15, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="Bot prompt")
    
    callback = CallbackQuery(
        id="query_123",
        from_user=mock_user,
        chat_instance="chat_inst",
        message=mock_message,
        data="cancel_add_reminder"
    )
    
    state = MagicMock(spec=FSMContext)
    state.get_state = AsyncMock(return_value="AddReminderStates:waiting_for_reminder_datetime")
    state.clear = AsyncMock()
    
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as mock_answer, \
         patch.object(Message, "delete", new_callable=AsyncMock) as mock_delete:
         
        await handle_cancel_add_reminder_callback(callback, state)
        
        state.clear.assert_called_once()
        mock_delete.assert_called_once()
        mock_answer.assert_called_once_with("❌ Добавление отменено")


@pytest.mark.anyio
async def test_handle_reminder_datetime_input_success():
    """
    Tests that a valid datetime input successfully creates a Reminder in DB,
    clears FSM state, deletes intermediate messages, and updates the event card text.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    message = Message(message_id=20, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="29.05.2026 14:15")
    
    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={
        "event_id": 42,
        "card_message_id": 10,
        "prompt_message_id": 15
    })
    state.clear = AsyncMock()
    
    db_session = MagicMock(spec=AsyncSession)
    
    # Target Event
    deadline_dt = datetime(2026, 5, 29, 15, 0, 0)
    mock_reminder = Reminder(id=1, event_id=42, remind_at=datetime(2026, 5, 29, 14, 15, 0), status=ReminderStatus.PENDING)
    mock_event = Event(id=42, title="Лекция по физике", deadline=deadline_dt, reminders=[mock_reminder])
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    mock_execute_result.scalar_one.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    mock_bot = MagicMock()
    mock_bot.delete_message = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()
    message = message.as_(mock_bot)
    
    with patch.object(Message, "delete", new_callable=AsyncMock) as mock_msg_delete:
        await handle_reminder_datetime_input(message, state, db_session)
        
        # Verify DB insert and commit
        db_session.add.assert_called_once()
        db_session.commit.assert_called_once()
        
        # Verify message cleanups
        mock_msg_delete.assert_called_once()
        mock_bot.delete_message.assert_called_once_with(chat_id=999222111, message_id=15)
        
        # Verify state is cleared
        state.clear.assert_called_once()
        
        # Verify card re-rendering
        mock_bot.edit_message_text.assert_called_once()
        args, kwargs = mock_bot.edit_message_text.call_args
        assert kwargs["chat_id"] == 999222111
        assert kwargs["message_id"] == 10
        assert "Лекция по физике" in kwargs["text"]
        assert "2026-05-29 в 14:15" in kwargs["text"]


@pytest.mark.anyio
async def test_handle_reminder_datetime_input_invalid_format():
    """
    Tests that an invalid date/time format triggers an error warning message
    and keeps the FSM state waiting (does not clear state or save anything).
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    message = Message(message_id=20, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="invalid-date-format")
    
    state = MagicMock(spec=FSMContext)
    db_session = MagicMock(spec=AsyncSession)
    
    with patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
        await handle_reminder_datetime_input(message, state, db_session)
        
        # Verify error answer
        mock_answer.assert_called_once()
        assert "Неверный формат" in mock_answer.call_args[0][0]
        
        # Verify DB and state were untouched
        db_session.add.assert_not_called()
        state.clear.assert_not_called()


@pytest.mark.anyio
async def test_handle_reminder_datetime_input_past_deadline():
    """
    Tests that a reminder scheduled after the event's deadline triggers an error
    warning and keeps the FSM state waiting.
    """
    mock_user = TGUser(id=999222111, is_bot=False, first_name="Владимир")
    mock_chat = Chat(id=999222111, type="private")
    # Setting datetime to 29.05.2026 16:00 (which is after deadline 15:00)
    message = Message(message_id=20, date=datetime_mock(), chat=mock_chat, from_user=mock_user, text="29.05.2026 16:00")
    
    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={
        "event_id": 42,
        "card_message_id": 10,
        "prompt_message_id": 15
    })
    
    db_session = MagicMock(spec=AsyncSession)
    deadline_dt = datetime(2026, 5, 29, 15, 0, 0)
    mock_event = Event(id=42, title="Лекция по физике", deadline=deadline_dt, reminders=[])
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_event
    db_session.execute = AsyncMock(return_value=mock_execute_result)
    
    with patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer:
        await handle_reminder_datetime_input(message, state, db_session)
        
        # Verify deadline violation message
        mock_answer.assert_called_once()
        assert "Время напоминания не может быть позже или равным дедлайну" in mock_answer.call_args[0][0]
        
        # Verify DB and state untouched
        db_session.add.assert_not_called()
        state.clear.assert_not_called()
