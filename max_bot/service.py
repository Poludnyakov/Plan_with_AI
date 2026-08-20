import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from anonymizer import DataAnonymizer
from account_service import intelligent_reminders_enabled
from intelligent_reminders import build_reminders
from interval_ai_service import extract_intervals
from .calendar import delete_max_yandex, sync_max_yandex
from interval_pipeline import REMINDER_OFFSETS, normalize_datetime
from services import SpeechKitService

from .models import MaxEvent, MaxEventTiming, MaxReminder, MaxUser
from unified_calendar import list_linked_events


logger = logging.getLogger("MaxEventService")
CANCEL_RE = re.compile(r"^\s*(?:отмена|отмени|отменить|удали|удалить)\b[\s:,.!—-]*(.*)$", re.I)
DATE_RE = re.compile(r"\b(?:сегодня|завтра|послезавтра|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b|\b\d{1,2}[./-]\d{1,2}", re.I)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\b(?:в|к|с|до)\s+(?:[01]?\d|2[0-3])(?:[:.]\d{2})?\b", re.I)
ALIASES = {"кр": "контрольная", "к/р": "контрольная", "контр": "контрольная"}


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def format_interval(start_at: datetime, end_at: datetime, timezone_name="Europe/Moscow") -> str:
    zone = ZoneInfo(timezone_name)
    start, end = aware(start_at).astimezone(zone), aware(end_at).astimezone(zone)
    if start.date() == end.date():
        return f"{start:%d.%m.%Y}, {start:%H:%M}–{end:%H:%M}"
    return f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}"


def normalize_title(value: str) -> str:
    tokens = re.findall(r"[a-zа-я0-9/]+", (value or "").casefold().replace("ё", "е"))
    return " ".join(ALIASES.get(token, token) for token in tokens)


def title_similarity(left: str, right: str) -> float:
    left, right = normalize_title(left), normalize_title(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.96
    coverage = len(set(left.split()) & set(right.split())) / max(len(set(left.split())), 1)
    return max(SequenceMatcher(None, left, right).ratio(), coverage * 0.9)


class MaxEventService:
    def __init__(self):
        self.anonymizer = DataAnonymizer()
        self.speechkit = SpeechKitService()

    async def user(self, db: AsyncSession, max_user_id: int) -> MaxUser:
        result = await db.execute(select(MaxUser).filter(MaxUser.max_user_id == max_user_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = MaxUser(max_user_id=max_user_id)
            db.add(user)
            await db.flush()
        return user

    async def create_drafts(self, db: AsyncSession, max_user_id: int, extracted: list[dict]) -> list[MaxEvent]:
        user = await self.user(db, max_user_id)
        smart_enabled = await intelligent_reminders_enabled(db, "max", max_user_id)
        now = datetime.now(timezone.utc)
        created: list[MaxEvent] = []
        for item in extracted:
            start = normalize_datetime(item["start_at"], user.timezone)
            end = normalize_datetime(item["end_at"], user.timezone)
            if end <= start:
                raise ValueError("Окончание мероприятия должно быть позже начала.")
            event = MaxEvent(
                user_id=user.id,
                title=(item.get("title") or "Без названия")[:255],
                description=item.get("description") or "",
                deadline=end,
                status="draft",
            )
            db.add(event)
            await db.flush()
            db.add(MaxEventTiming(event_id=event.id, start_at=start, end_at=end))
            reminder_times = await build_reminders(
                smart_enabled, event.title, event.description or "",
                start, end, user.timezone, now,
            )
            for remind_at in reminder_times:
                db.add(MaxReminder(event_id=event.id, remind_at=remind_at, status="pending"))
            created.append(event)
        await db.commit()
        for event in created:
            await db.refresh(event, attribute_names=["timing", "reminders"])
        return created

    async def from_text(self, db: AsyncSession, max_user_id: int, text: str) -> list[MaxEvent]:
        if not text.strip():
            raise ValueError("Сообщение пустое.")
        try:
            return await self.create_drafts(
                db, max_user_id, await extract_intervals(self.anonymizer.anonymize_text(text))
            )
        except Exception:
            await db.rollback()
            raise

    async def from_voice(self, db: AsyncSession, max_user_id: int, content: bytes) -> list[MaxEvent]:
        return await self.from_text(db, max_user_id, await self.speechkit.transcribe_voice(content))

    async def from_image(self, db: AsyncSession, max_user_id: int, content: bytes) -> list[MaxEvent]:
        try:
            items = await extract_intervals(content)
            for item in items:
                item["title"] = self.anonymizer.anonymize_text(item.get("title", ""))
                item["description"] = self.anonymizer.anonymize_text(item.get("description", ""))
            return await self.create_drafts(db, max_user_id, items)
        except Exception:
            await db.rollback()
            raise

    async def event_for_user(self, db: AsyncSession, max_user_id: int, event_id: int) -> MaxEvent | None:
        result = await db.execute(
            select(MaxEvent)
            .join(MaxUser)
            .filter(MaxEvent.id == event_id, MaxUser.max_user_id == max_user_id)
            .options(selectinload(MaxEvent.timing), selectinload(MaxEvent.reminders))
        )
        return result.scalar_one_or_none()

    async def confirm(self, db: AsyncSession, max_user_id: int, event_id: int):
        event = await self.event_for_user(db, max_user_id, event_id)
        if not event:
            return "missing", None
        if event.status == "confirmed":
            return "confirmed", event
        timing = event.timing
        result = await db.execute(
            select(MaxEvent, MaxEventTiming)
            .join(MaxEventTiming)
            .filter(
                MaxEvent.user_id == event.user_id,
                MaxEvent.id != event.id,
                MaxEvent.status == "confirmed",
                MaxEventTiming.start_at < timing.end_at,
                MaxEventTiming.end_at > timing.start_at,
            )
        )
        conflict = result.first()
        if conflict:
            await db.delete(event)
            await db.commit()
            return "conflict", conflict
        event.status = "confirmed"
        await db.commit()
        asyncio.create_task(sync_max_yandex(
            event.title, timing.start_at, timing.end_at, event.description or "",
            event_id=event.id,
        ))
        return "confirmed", event

    async def delete(self, db: AsyncSession, max_user_id: int, event_id: int) -> MaxEvent | None:
        event = await self.event_for_user(db, max_user_id, event_id)
        if not event:
            return None
        confirmed = event.status == "confirmed"
        await db.delete(event)
        await db.commit()
        if confirmed:
            asyncio.create_task(delete_max_yandex(event_id))
        return event

    async def list_confirmed(self, db: AsyncSession, max_user_id: int, limit: int = 12):
        result = await db.execute(
            select(MaxEvent, MaxEventTiming, MaxUser.timezone)
            .join(MaxUser).join(MaxEventTiming)
            .filter(MaxUser.max_user_id == max_user_id, MaxEvent.status == "confirmed")
            .order_by(MaxEventTiming.start_at).limit(limit)
        )
        return list(result.all())

    async def cancellation_candidates(self, db: AsyncSession, max_user_id: int, raw: str):
        match = CANCEL_RE.match(raw)
        query = match.group(1).strip() if match else raw.strip()
        if not query:
            return []
        title, target = query, None
        date_hint, time_hint = bool(DATE_RE.search(query)), bool(TIME_RE.search(query))
        if date_hint or time_hint:
            try:
                items = await extract_intervals(self.anonymizer.anonymize_text(query))
                if items:
                    title, target = items[0].get("title") or query, aware(items[0]["start_at"])
            except Exception as error:
                logger.warning("MAX cancellation detail parsing failed: %s", error)
        candidates = []
        for entry in await list_linked_events(db, "max", max_user_id):
            event, timing, timezone_name = entry.event, entry.timing, entry.timezone_name
            event._calendar_ref = entry.ref
            score = title_similarity(title, event.title)
            if score < 0.45:
                continue
            start = aware(timing.start_at if timing else event.deadline - timedelta(minutes=30))
            if target:
                zone = ZoneInfo(timezone_name)
                local, wanted = start.astimezone(zone), target.astimezone(zone)
                if date_hint and local.date() != wanted.date():
                    continue
                if time_hint and abs((local.hour * 60 + local.minute) - (wanted.hour * 60 + wanted.minute)) > 10:
                    continue
            candidates.append((score, event, timing, timezone_name))
        return sorted(candidates, key=lambda row: (-row[0], row[2].start_at if row[2] else row[1].deadline))
