import logging
import re
from datetime import datetime
from typing import List, Optional
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import User, Event, Reminder, ReminderStatus, EventStatus
from schemas import UserCreate, EventCreate, EventUpdate
from repositories import UserRepository, EventRepository, ReminderRepository
from anonymizer import DataAnonymizer
from ai_service import YandexGPTService
from openrouter_service import NO_SPEECH_TEXT, chat_completion, transcribe_audio


class UserService:
    """Service layer coordinating business logic for User operations."""
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserRepository(db)

    async def get_user_by_id(self, user_id: int) -> Optional[User]:
        """Retrieve a user by their internal database primary key ID."""
        return await self.user_repo.get_by_id(user_id)

    async def get_user_by_tg_id(self, tg_id: int) -> Optional[User]:
        """Retrieve a user by their Telegram ID."""
        return await self.user_repo.get_by_tg_id(tg_id)

    async def create_user(self, user_in: UserCreate) -> User:
        """
        Register a new student user in the system.
        Raises a ValueError if a user with the same Telegram ID already exists.
        """
        existing_user = await self.user_repo.get_by_tg_id(user_in.tg_id)
        if existing_user:
            raise ValueError(f"Student with Telegram ID {user_in.tg_id} already exists.")
        
        db_user = await self.user_repo.create(
            tg_id=user_in.tg_id, 
            timezone=user_in.timezone
        )
        await self.db.commit()
        await self.db.refresh(db_user)
        return db_user


class EventService:
    """Service layer coordinating business logic for Event and Reminder operations."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserRepository(db)
        self.event_repo = EventRepository(db)
        self.reminder_repo = ReminderRepository(db)

    async def get_event(self, event_id: int) -> Optional[Event]:
        """Retrieve an event by ID along with its reminders."""
        return await self.event_repo.get_event_with_reminders(event_id)

    async def get_user_events(self, user_id: int) -> List[Event]:
        """Retrieve all events (with reminders) for a specific user."""
        return await self.event_repo.get_all_by_user_id(user_id)

    async def create_event(self, event_in: EventCreate) -> Event:
        """
        Create a new academic/personal event for a user.
        If a list of reminder datetimes is provided, automatically generates
        and associates pending reminders.
        Raises a ValueError if the corresponding user does not exist.
        """
        # Ensure the user exists before creating an event
        user = await self.user_repo.get_by_id(event_in.user_id)
        if not user:
            raise ValueError(f"User with ID {event_in.user_id} does not exist.")

        # Create event
        db_event = await self.event_repo.create(
            user_id=event_in.user_id,
            title=event_in.title,
            description=event_in.description,
            deadline=event_in.deadline,
            status=event_in.status
        )
        
        # Flush to generate event database ID for reminder mapping
        await self.db.flush()

        # Handle optional reminder generation
        if event_in.reminders:
            for remind_time in event_in.reminders:
                await self.reminder_repo.create(
                    event_id=db_event.id,
                    remind_at=remind_time,
                    status=ReminderStatus.PENDING
                )

        await self.db.commit()
        
        # Reload the event with loaded relationship to return complete event response
        complete_event = await self.event_repo.get_event_with_reminders(db_event.id)
        return complete_event

    async def update_event(self, event_id: int, event_in: EventUpdate) -> Optional[Event]:
        """
        Update fields of an existing event.
        Returns the updated Event model, or None if the event is not found.
        """
        db_event = await self.event_repo.get_event_with_reminders(event_id)
        if not db_event:
            return None

        # Convert update schema to a dict of values that were set
        update_data = event_in.model_dump(exclude_unset=True)
        
        await self.event_repo.update(db_event, update_data)
        await self.db.commit()
        await self.db.refresh(db_event)
        
        if db_event.status == EventStatus.CONFIRMED:
            from yandex_calendar_service import YandexCalendarService
            await YandexCalendarService().add_deadline_to_yandex(
                title=db_event.title,
                deadline=db_event.deadline,
                description=db_event.description
            )
        
        return db_event

    async def delete_event(self, event_id: int) -> bool:
        """
        Delete an existing event by ID. Cascading rules handle the deletion
        of associated reminders automatically on the database level.
        Returns True if successful, False if the event was not found.
        """
        db_event = await self.event_repo.get_by_id(event_id)
        if not db_event:
            return False

        await self.event_repo.delete(db_event)
        await self.db.commit()
        return True


logger = logging.getLogger("SpeechKitService")

class SpeechKitService:
    """Compatibility name for the voice parser backed by OpenRouter STT."""

    async def transcribe_voice(self, audio_bytes: bytes) -> str:
        # Telegram and MAX deliver Ogg/Opus voice messages. OpenRouter accepts
        # the same bytes as a base64 ``ogg`` transcription input.
        text = normalize_transcript(await transcribe_audio(audio_bytes, audio_format="ogg"))
        if not text:
            raise ValueError(NO_SPEECH_TEXT)
        return text


# Whisper "hears" these closing captions of videos in silence or noise.
WHISPER_HALLUCINATION_RE = re.compile(
    r"продолжение следует|субтитр|спасибо за просмотр|подписывайтесь|"
    r"ставьте лайк|dimatorzok|редактор|корректор|музыка|аплодисменты|смех",
    re.IGNORECASE,
)


def no_event_in_voice_text(transcript: str) -> str:
    """Reply to a voice message without an event: show what was heard."""
    return (
        f"🎙 Распознано: «{transcript}»\n"
        "Не нашёл здесь мероприятия. Назовите дату и время, например: "
        "«завтра в 15:00 встреча»."
    )


def normalize_transcript(text: str) -> str:
    """Speech-to-text output as a user would have typed it."""
    text = re.sub(r"\s+", " ", text or "").strip()
    # «Перенеси спортзал на восемь вечера». — quotes around the whole phrase.
    text = re.sub(r"^[«\"'„“]+|[»\"'“”]+(?=[.!?…]*$)", "", text).strip()
    words = re.findall(r"[^\W\d_]+", text)
    if not words or (len(text) <= 60 and WHISPER_HALLUCINATION_RE.search(text)
                     and len(WHISPER_HALLUCINATION_RE.sub("", text).split()) <= 3):
        return ""
    # «без пятнадцати шесть» is transcribed as «без 15.06» or «без 15:06».
    text = re.sub(
        r"\bбез\s+(\d{1,2})[.:](\d{1,2})\b",
        lambda match: f"без {int(match[1])} минут {int(match[2])}",
        text, flags=re.IGNORECASE,
    )
    return text


class YandexOCRService:
    """Legacy adapter name; image OCR is served by OpenRouter vision."""

    async def recognize_text(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        if not image_bytes:
            raise ValueError("Image data is empty.")
        import base64
        encoded = base64.b64encode(image_bytes).decode("ascii")
        response = await chat_completion(
            model=settings.OPENROUTER_MODEL_FOR_OCR,
            messages=[
                {"role": "system", "content": "Перепиши текст с изображения без пояснений."},
                {"role": "user", "content": [
                    {"type": "text", "text": "Распознай текст на изображении."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
                ]},
            ],
        )
        text = str(response.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("OpenRouter OCR returned an empty result.")
        return text


class ActionPipelineService:
    """
    ActionPipelineService coordinates the complete student scheduling input pipeline:
    SpeechKit Transcription -> Data Anonymization -> YandexGPT JSON Extraction -> Database Persistence.
    """
    def __init__(self):
        self.speechkit_service = SpeechKitService()
        self.ocr_service = YandexOCRService()
        self.anonymizer = DataAnonymizer()
        self.gpt_service = YandexGPTService()

    async def process_text_input(
        self, 
        user_tg_id: int, 
        raw_text: str, 
        db_session: AsyncSession
    ) -> List[Event]:
        """
        Processes a raw text query from a student, anonymizes sensitive data,
        extracts event items, registers/fetches the user, and persists draft events/reminders.
        """
        import time
        start_time = time.perf_counter()
        logger.info(f"Starting text pipeline for user tg_id={user_tg_id}...")

        if not raw_text.strip():
            raise ValueError("Input text is empty.")

        try:
            # 1. Anonymization Step
            anon_start = time.perf_counter()
            anonymized_text = self.anonymizer.anonymize_text(raw_text)
            logger.info(f"[Pipeline Step 1/3] Anonymized text in {time.perf_counter() - anon_start:.4f}s.")
            logger.info(f"Anonymized output: {anonymized_text}")

            # 2. YandexGPT JSON Event Extraction Step
            gpt_start = time.perf_counter()
            extracted_events_json = await self.gpt_service.extract_schedule(anonymized_text)
            logger.info(f"[Pipeline Step 2/3] Extracted {len(extracted_events_json)} events in {time.perf_counter() - gpt_start:.4f}s.")

            # 3. Database Persistence Step
            db_start = time.perf_counter()
            user_repo = UserRepository(db_session)
            event_repo = EventRepository(db_session)
            reminder_repo = ReminderRepository(db_session)

            # Retrieve or register the user
            db_user = await user_repo.get_by_tg_id(user_tg_id)
            if not db_user:
                logger.info(f"User tg_id={user_tg_id} not found in DB. Creating new user...")
                db_user = await user_repo.create(tg_id=user_tg_id, timezone="Europe/Moscow")
                await db_session.flush()

            created_events = []
            for event_data in extracted_events_json:
                title = self.anonymizer.clean_event_title(
                    event_data.get("title", "")
                )
                description = self.anonymizer.clean_display_text(
                    event_data.get("description", "")
                )
                
                # Parse deadline ISO string
                deadline_str = event_data.get("deadline")
                if deadline_str.endswith("Z"):
                    deadline_str = deadline_str[:-1] + "+00:00"
                deadline = datetime.fromisoformat(deadline_str)

                # Create the draft event
                db_event = await event_repo.create(
                    user_id=db_user.id,
                    title=title,
                    description=description,
                    deadline=deadline,
                    status=EventStatus.DRAFT
                )
                await db_session.flush()

                # Create reminders
                reminders_data = event_data.get("suggested_reminders", [])
                for remind_str in reminders_data:
                    if remind_str.endswith("Z"):
                        remind_str = remind_str[:-1] + "+00:00"
                    remind_at = datetime.fromisoformat(remind_str)
                    
                    await reminder_repo.create(
                        event_id=db_event.id,
                        remind_at=remind_at,
                        status=ReminderStatus.PENDING
                    )

                created_events.append(db_event)

            # Commit transaction
            await db_session.commit()
            logger.info(f"[Pipeline Step 3/3] Persisted data and committed to DB in {time.perf_counter() - db_start:.4f}s.")
            
            # Eager load relationships so they are available after session commit
            loaded_events = []
            for ev in created_events:
                loaded_ev = await event_repo.get_event_with_reminders(ev.id)
                loaded_events.append(loaded_ev)

            total_duration = time.perf_counter() - start_time
            logger.info(f"Pipeline text processing completed successfully in {total_duration:.4f}s.")
            return loaded_events

        except Exception as e:
            logger.error(f"Pipeline error during text processing: {e}. Rolling back database session...", exc_info=True)
            await db_session.rollback()
            raise

    async def process_voice_input(
        self, 
        user_tg_id: int, 
        audio_bytes: bytes, 
        db_session: AsyncSession
    ) -> List[Event]:
        """
        Processes a raw audio clip (Ogg Opus) from a student, transcribes it,
        and pipes the resulting text through process_text_input.
        """
        import time
        start_time = time.perf_counter()
        logger.info(f"Starting voice pipeline for user tg_id={user_tg_id}...")

        if not audio_bytes:
            raise ValueError("Audio bytes are empty.")

        try:
            # 1. Transcribe Voice Step
            transcribe_start = time.perf_counter()
            transcribed_text = await self.speechkit_service.transcribe_voice(audio_bytes)
            logger.info(f"[Voice Pipeline] Transcribed audio in {time.perf_counter() - transcribe_start:.4f}s.")
            logger.info(f"Transcript: {transcribed_text}")

            if not transcribed_text.strip():
                raise ValueError("SpeechKit transcribed empty text from voice message.")

            # 2. Pipe to text processing
            events = await self.process_text_input(
                user_tg_id=user_tg_id,
                raw_text=transcribed_text,
                db_session=db_session
            )
            
            logger.info(f"Pipeline voice processing completed successfully in {time.perf_counter() - start_time:.4f}s.")
            return events

        except Exception as e:
            logger.error(f"Pipeline error during voice processing: {e}. Rolling back database session...", exc_info=True)
            await db_session.rollback()
            raise

    async def process_image_input(
        self,
        user_tg_id: int,
        image_bytes: bytes,
        db_session: AsyncSession
    ) -> List[Event]:
        """
        Processes a raw schedule image (bytes) from a student, sends it directly
        to the Yandex AI Studio multimodal model, parses it, anonymizes extracted events,
        and persists draft events and reminders to the DB.
        """
        import time
        start_time = time.perf_counter()
        logger.info(f"Starting multimodal image pipeline for user tg_id={user_tg_id}...")

        if not image_bytes:
            raise ValueError("Image bytes are empty.")

        try:
            # 1. Yandex AI Studio structured completions step
            ai_start = time.perf_counter()
            extracted_events_json = await self.gpt_service.extract_schedule(image_bytes)
            logger.info(f"[Image Pipeline Step 1/2] Extracted {len(extracted_events_json)} events in {time.perf_counter() - ai_start:.4f}s.")

            # 2. Database Persistence & Anonymization Step
            db_start = time.perf_counter()
            user_repo = UserRepository(db_session)
            event_repo = EventRepository(db_session)
            reminder_repo = ReminderRepository(db_session)

            # Retrieve or register the user
            db_user = await user_repo.get_by_tg_id(user_tg_id)
            if not db_user:
                logger.info(f"User tg_id={user_tg_id} not found in DB. Creating new user...")
                db_user = await user_repo.create(tg_id=user_tg_id, timezone="Europe/Moscow")
                await db_session.flush()

            created_events = []
            for event_data in extracted_events_json:
                title = event_data.get("title", "Без названия")
                description = event_data.get("description", "")
                
                # Anonymize PII, then remove internal mask labels from the UI.
                title = self.anonymizer.clean_event_title(
                    self.anonymizer.anonymize_text(title)
                )
                description = self.anonymizer.clean_display_text(
                    self.anonymizer.anonymize_text(description)
                )

                # Parse deadline ISO string
                deadline_str = event_data.get("deadline")
                if deadline_str.endswith("Z"):
                    deadline_str = deadline_str[:-1] + "+00:00"
                deadline = datetime.fromisoformat(deadline_str)

                # Create the draft event
                db_event = await event_repo.create(
                    user_id=db_user.id,
                    title=title,
                    description=description,
                    deadline=deadline,
                    status=EventStatus.DRAFT
                )
                await db_session.flush()

                # Create reminders
                reminders_data = event_data.get("suggested_reminders", [])
                for remind_str in reminders_data:
                    if remind_str.endswith("Z"):
                        remind_str = remind_str[:-1] + "+00:00"
                    remind_at = datetime.fromisoformat(remind_str)
                    
                    await reminder_repo.create(
                        event_id=db_event.id,
                        remind_at=remind_at,
                        status=ReminderStatus.PENDING
                    )

                created_events.append(db_event)

            # Commit transaction
            await db_session.commit()
            logger.info(f"[Image Pipeline Step 2/2] Persisted data and committed to DB in {time.perf_counter() - db_start:.4f}s.")
            
            # Eager load relationships
            loaded_events = []
            for ev in created_events:
                loaded_ev = await event_repo.get_event_with_reminders(ev.id)
                loaded_events.append(loaded_ev)

            total_duration = time.perf_counter() - start_time
            logger.info(f"Pipeline image processing completed successfully in {total_duration:.4f}s.")
            return loaded_events

        except Exception as e:
            logger.error(f"Pipeline error during image processing: {e}. Rolling back database session...", exc_info=True)
            await db_session.rollback()
            raise
