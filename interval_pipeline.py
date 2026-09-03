import logging
from datetime import datetime, timedelta, timezone
from typing import List, Union
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from anonymizer import DataAnonymizer
from conversation_service import recent_dialogue_context, remember_dialogue_turn
from reminder_service import build_user_reminders
from interval_ai_service import extract_intervals
from interval_models import EventTiming
from models import Event, EventStatus, ReminderStatus
from repositories import EventRepository, ReminderRepository, UserRepository
from services import SpeechKitService


logger = logging.getLogger("IntervalPipeline")
REMINDER_OFFSETS = (
    timedelta(hours=24),
    timedelta(hours=12),
    timedelta(hours=1),
    timedelta(minutes=30),
    timedelta(minutes=15),
)


def normalize_datetime(value: Union[str, datetime], timezone_name: str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.astimezone(timezone.utc)


class IntervalActionPipelineService:
    def __init__(self):
        self.anonymizer = DataAnonymizer()
        self.speechkit_service = SpeechKitService()

    async def _persist(
        self,
        user_tg_id: int,
        extracted: List[dict],
        db_session: AsyncSession,
    ) -> List[Event]:
        user_repo = UserRepository(db_session)
        event_repo = EventRepository(db_session)
        reminder_repo = ReminderRepository(db_session)
        user = await user_repo.get_by_tg_id(user_tg_id)
        if not user:
            user = await user_repo.create(user_tg_id, "Europe/Moscow")
            await db_session.flush()
        created = []
        now = datetime.now(timezone.utc)
        for item in extracted:
            start_at = normalize_datetime(item["start_at"], user.timezone)
            end_at = normalize_datetime(item["end_at"], user.timezone)
            if end_at <= start_at:
                raise ValueError("Окончание мероприятия должно быть позже начала.")

            event = await event_repo.create(
                user_id=user.id,
                title=self.anonymizer.clean_event_title(item.get("title", "")),
                description=self.anonymizer.clean_display_text(
                    item.get("description", "")
                ),
                deadline=end_at,
                status=EventStatus.DRAFT,
            )
            await db_session.flush()
            db_session.add(EventTiming(event_id=event.id, start_at=start_at, end_at=end_at, all_day=bool(item.get("all_day"))))

            reminder_times = await build_user_reminders(
                db_session, "telegram", user_tg_id,
                event.title, event.description or "",
                start_at, end_at, user.timezone, now, all_day=bool(item.get("all_day")),
            )
            for remind_at in reminder_times:
                await reminder_repo.create(
                    event_id=event.id, remind_at=remind_at,
                    status=ReminderStatus.PENDING,
                )
            created.append(event)

        await db_session.commit()
        return [await event_repo.get_event_with_reminders(event.id) for event in created]

    async def process_text_input(
        self,
        user_tg_id: int,
        raw_text: str,
        db_session: AsyncSession,
    ) -> List[Event]:
        if not raw_text.strip():
            raise ValueError("Сообщение пустое.")
        try:
            extracted = await self.extract_text_input(
                user_tg_id, raw_text, db_session
            )
            events = await self._persist(user_tg_id, extracted, db_session)
            await remember_dialogue_turn(
                db_session, "telegram", user_tg_id,
                self.anonymizer.anonymize_text(raw_text), commit=True,
            )
            return events
        except Exception:
            await db_session.rollback()
            raise

    async def extract_text_input(
        self,
        user_tg_id: int,
        raw_text: str,
        db_session: AsyncSession,
    ) -> List[dict]:
        """Parse one text turn with the compact history of this account."""
        history = await recent_dialogue_context(
            db_session, "telegram", user_tg_id
        )
        anonymized = self.anonymizer.anonymize_text(raw_text)
        return await extract_intervals(anonymized, context=history)

    async def process_voice_input(
        self,
        user_tg_id: int,
        audio_bytes: bytes,
        db_session: AsyncSession,
    ) -> List[Event]:
        transcript = await self.speechkit_service.transcribe_voice(audio_bytes)
        return await self.process_text_input(user_tg_id, transcript, db_session)

    async def process_image_input(
        self,
        user_tg_id: int,
        image_bytes: bytes,
        db_session: AsyncSession,
    ) -> List[Event]:
        try:
            extracted = await extract_intervals(image_bytes)
            for item in extracted:
                item["title"] = self.anonymizer.anonymize_text(item.get("title", ""))
                item["description"] = self.anonymizer.anonymize_text(item.get("description", ""))
            return await self._persist(user_tg_id, extracted, db_session)
        except Exception:
            await db_session.rollback()
            raise
