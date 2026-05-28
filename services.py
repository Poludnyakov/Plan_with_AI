import logging
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
    """
    SpeechKitService coordinates integration with Yandex SpeechKit.
    Recognizes short audio fragments (up to 30 seconds / 1MB limit for REST API)
    using the official Yandex SpeechKit REST API.
    """
    def __init__(self):
        self.api_key = settings.API_KEY
        self.folder_id = settings.FOLDER_ID
        self.url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    async def transcribe_voice(self, audio_bytes: bytes) -> str:
        """
        Transcribes a voice message (Ogg Opus format) using Yandex SpeechKit.
        
        :param audio_bytes: Raw binary data of the voice message.
        :return: Transcribed text in Russian.
        :raises ValueError: If credentials are not set, the audio is empty,
                            or SpeechKit returns an empty text / error.
        :raises httpx.HTTPStatusError: For non-success HTTP status codes.
        """
        if not self.api_key:
            raise ValueError("Yandex SpeechKit API_KEY is not configured.")
        if not self.folder_id:
            raise ValueError("Yandex SpeechKit FOLDER_ID is not configured.")
        if not audio_bytes:
            raise ValueError("Audio data is empty.")

        headers = {
            "Authorization": f"Api-Key {self.api_key}"
        }
        params = {
            "folderId": self.folder_id,
            "lang": "ru-RU",
            "format": "oggopus"
        }

        logger.info("Sending voice message to Yandex SpeechKit STT endpoint...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    self.url,
                    headers=headers,
                    params=params,
                    content=audio_bytes
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Yandex SpeechKit HTTP error: {e.response.status_code} - {e.response.text}")
                raise
            except httpx.RequestError as e:
                logger.error(f"Yandex SpeechKit connection error: {e}")
                raise ValueError(f"Yandex SpeechKit is currently unavailable: {e}")

        try:
            result_data = response.json()
        except Exception as e:
            logger.error(f"Failed to parse Yandex SpeechKit response JSON: {e}")
            raise ValueError(f"Failed to parse Yandex SpeechKit response JSON: {e}")

        text = result_data.get("result", "").strip()

        if not text:
            logger.warning("Yandex SpeechKit returned empty transcription result.")
            raise ValueError("Yandex SpeechKit returned an empty transcription.")

        logger.info("Successfully transcribed voice message.")
        return text


class YandexOCRService:
    """
    YandexOCRService coordinates integration with Yandex Vision OCR API.
    Recognizes text from images using the official Yandex Cloud OCR REST API.
    """
    def __init__(self):
        self.api_key = settings.API_KEY
        self.folder_id = settings.FOLDER_ID
        self.url = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"

    async def recognize_text(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """
        Recognizes text from an image using Yandex Vision OCR API.
        
        :param image_bytes: Raw binary data of the image.
        :param mime_type: MIME type of the image (e.g. image/jpeg, image/png).
        :return: Recognized text.
        """
        import base64
        if not self.api_key:
            raise ValueError("Yandex OCR API_KEY is not configured.")
        if not self.folder_id:
            raise ValueError("Yandex OCR FOLDER_ID is not configured.")
        if not image_bytes:
            raise ValueError("Image data is empty.")

        # Encode image to Base64 string
        base64_content = base64.b64encode(image_bytes).decode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {self.api_key}",
            "x-folder-id": self.folder_id
        }

        payload = {
            "mimeType": mime_type,
            "content": base64_content,
            "languageCodes": ["ru", "en"]
        }

        logger.info("Sending image to Yandex OCR endpoint...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    self.url,
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Yandex OCR HTTP error: {e.response.status_code} - {e.response.text}")
                raise
            except httpx.RequestError as e:
                logger.error(f"Yandex OCR connection error: {e}")
                raise ValueError(f"Yandex OCR is currently unavailable: {e}")

        try:
            result_data = response.json()
        except Exception as e:
            logger.error(f"Failed to parse Yandex OCR response JSON: {e}")
            raise ValueError(f"Failed to parse Yandex OCR response JSON: {e}")

        # Extract text lines
        text_lines = []
        result = result_data.get("result", {})
        text_page = result.get("textPage", {})
        blocks = text_page.get("blocks", [])
        for block in blocks:
            lines = block.get("lines", [])
            for line in lines:
                line_text = line.get("text", "").strip()
                if line_text:
                    text_lines.append(line_text)
                    
        text = "\n".join(text_lines).strip()

        if not text:
            logger.warning("Yandex OCR returned empty text page.")
            raise ValueError("На этом изображении не удалось распознать печатный текст расписания.")

        logger.info(f"Successfully recognized text via Yandex OCR: {len(text)} chars.")
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
                title = event_data.get("title", "Без названия")
                description = event_data.get("description", "")
                
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
                
                # Anonymize event text fields to ensure data protection
                title = self.anonymizer.anonymize_text(title)
                description = self.anonymizer.anonymize_text(description)

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
