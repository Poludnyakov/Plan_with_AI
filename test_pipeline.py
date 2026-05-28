import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

from database import Base
from models import User, Event, Reminder, EventStatus, ReminderStatus
from services import ActionPipelineService

@pytest.fixture(scope="function")
async def test_db():
    """
    Function-scoped fixture that sets up an in-memory SQLite database
    for testing pipeline database operations isolation.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with async_session() as session:
        yield session
        
    await engine.dispose()


@pytest.mark.anyio
async def test_pipeline_text_input_success(test_db):
    """
    Tests successful text input pipeline execution:
    Anonymization -> GPT Extraction -> Database insertion of User, Event and Reminders.
    """
    mock_raw_text = "Я Алексей, тел 89991112233. Лек по матану в пятницу в 14:00"
    mock_anon_text = "Я [ФИО], тел [ТЕЛЕФОН]. Лек по матану в пятницу в 14:00"
    mock_gpt_response = [
        {
            "title": "Лекция по матанализу",
            "deadline": "2026-10-09T14:00:00Z",
            "description": "Аудитория 402",
            "suggested_reminders": [
                "2026-10-08T14:00:00Z",
                "2026-10-09T02:00:00Z",
                "2026-10-09T13:00:00Z",
                "2026-10-09T13:30:00Z",
                "2026-10-09T13:45:00Z"
            ]
        }
    ]

    pipeline = ActionPipelineService()

    with patch.object(pipeline.anonymizer, "anonymize_text", return_value=mock_anon_text) as mock_anon, \
         patch.object(pipeline.gpt_service, "extract_schedule", new_callable=AsyncMock, return_value=mock_gpt_response) as mock_gpt:
        
        user_tg_id = 123456789
        events = await pipeline.process_text_input(
            user_tg_id=user_tg_id,
            raw_text=mock_raw_text,
            db_session=test_db
        )

        # Assertions
        assert len(events) == 1
        event = events[0]
        assert event.title == "Лекция по матанализу"
        assert event.description == "Аудитория 402"
        assert event.status == EventStatus.DRAFT
        assert len(event.reminders) == 5
        
        # Verify reminders in DB
        reminders = sorted(event.reminders, key=lambda r: r.remind_at)
        assert reminders[0].status == ReminderStatus.PENDING
        assert reminders[0].remind_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-08T14:00:00"
        assert reminders[1].remind_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-09T02:00:00"
        assert reminders[2].remind_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-09T13:00:00"
        assert reminders[3].remind_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-09T13:30:00"
        assert reminders[4].remind_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-09T13:45:00"

        # Verify User was created
        result = await test_db.execute(select(User).filter(User.tg_id == user_tg_id))
        db_user = result.scalar_one_or_none()
        assert db_user is not None
        assert db_user.timezone == "Europe/Moscow"

        mock_anon.assert_called_once_with(mock_raw_text)
        mock_gpt.assert_called_once_with(mock_anon_text)


@pytest.mark.anyio
async def test_pipeline_voice_input_success(test_db):
    """
    Tests successful voice input pipeline execution:
    Audio Transcription -> Anonymization -> GPT Extraction -> Database persistence.
    """
    mock_audio_bytes = b"fake_ogg_opus_voice_recording"
    mock_transcript = "лекция по матану завтра"
    mock_anon_text = "лекция по матану завтра"
    mock_gpt_response = [
        {
            "title": "Лекция по матанализу",
            "deadline": "2026-10-09T14:00:00Z",
            "description": "",
            "suggested_reminders": [
                "2026-10-08T14:00:00Z",
                "2026-10-09T02:00:00Z",
                "2026-10-09T13:00:00Z",
                "2026-10-09T13:30:00Z",
                "2026-10-09T13:45:00Z"
            ]
        }
    ]

    pipeline = ActionPipelineService()

    with patch.object(pipeline.speechkit_service, "transcribe_voice", new_callable=AsyncMock, return_value=mock_transcript) as mock_transcribe, \
         patch.object(pipeline.anonymizer, "anonymize_text", return_value=mock_anon_text) as mock_anon, \
         patch.object(pipeline.gpt_service, "extract_schedule", new_callable=AsyncMock, return_value=mock_gpt_response) as mock_gpt:
        
        user_tg_id = 987654321
        events = await pipeline.process_voice_input(
            user_tg_id=user_tg_id,
            audio_bytes=mock_audio_bytes,
            db_session=test_db
        )

        assert len(events) == 1
        assert events[0].title == "Лекция по матанализу"
        
        mock_transcribe.assert_called_once_with(mock_audio_bytes)
        mock_anon.assert_called_once_with(mock_transcript)
        mock_gpt.assert_called_once_with(mock_anon_text)


@pytest.mark.anyio
async def test_pipeline_rollback_on_gpt_failure(test_db):
    """
    Tests that if YandexGPTService fails during pipeline execution,
    the transaction is correctly rolled back and no entries (User or Event) are committed.
    """
    pipeline = ActionPipelineService()

    # Pre-populate the user to check that no new data gets committed on rollback
    user_tg_id = 555555555
    
    with patch.object(pipeline.anonymizer, "anonymize_text", return_value="anonymized text"), \
         patch.object(pipeline.gpt_service, "extract_schedule", new_callable=AsyncMock, side_effect=ValueError("GPT extraction failed")):
        
        with pytest.raises(ValueError) as excinfo:
            await pipeline.process_text_input(
                user_tg_id=user_tg_id,
                raw_text="Я Алексей, запиши лекцию",
                db_session=test_db
            )
        
        assert "GPT extraction failed" in str(excinfo.value)

        # Check DB that no User got committed
        result = await test_db.execute(select(User).filter(User.tg_id == user_tg_id))
        db_user = result.scalar_one_or_none()
        assert db_user is None

        # Check DB that no Event got committed
        result = await test_db.execute(select(Event))
        db_events = result.scalars().all()
        assert len(db_events) == 0
