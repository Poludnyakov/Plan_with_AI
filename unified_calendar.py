import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import linked_identities
from reminder_service import build_user_reminders
from interval_calendar_sync import delete_yandex_interval, sync_yandex_interval
from interval_models import EventTiming
from interval_pipeline import normalize_datetime
from max_bot.calendar import delete_max_yandex, sync_max_yandex
from max_bot.models import MaxEvent, MaxEventTiming, MaxReminder, MaxUser
from models import Event, EventStatus, Reminder, ReminderStatus, User


@dataclass
class CalendarEntry:
    source: str
    event: Event | MaxEvent
    timing: EventTiming | MaxEventTiming
    timezone_name: str
    reminders: list[datetime] = field(default_factory=list)

    @property
    def ref(self) -> str:
        prefix = "t" if self.source == "telegram" else "m"
        return f"{prefix}:{self.event.id}"


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def entry_is_past(entry: CalendarEntry, now: datetime | None = None) -> bool:
    """Return true when an event belongs only to calendar history."""
    current = aware(now or datetime.now(timezone.utc))
    return aware(entry.timing.end_at) <= current


def entry_is_upcoming(entry: CalendarEntry, now: datetime | None = None) -> bool:
    """Return true only for uncompleted events that have not started yet."""
    current = aware(now or datetime.now(timezone.utc))
    return (
        not bool(getattr(entry.event, "is_completed", False))
        and aware(entry.timing.start_at) > current
    )


def parse_ref(value: str | int, default_source: str) -> tuple[str, int]:
    text = str(value)
    if ":" not in text:
        return default_source, int(text)
    prefix, raw_id = text.split(":", 1)
    source = {"t": "telegram", "telegram": "telegram", "m": "max", "max": "max"}.get(prefix)
    if not source:
        raise ValueError("Некорректный идентификатор мероприятия")
    return source, int(raw_id)


async def _telegram_entries(db: AsyncSession, tg_id: int) -> list[CalendarEntry]:
    result = await db.execute(
        select(Event, EventTiming, User.timezone)
        .join(User, User.id == Event.user_id)
        .outerjoin(EventTiming, EventTiming.event_id == Event.id)
        .filter(User.tg_id == tg_id, Event.status == EventStatus.CONFIRMED)
    )
    entries = []
    for event, timing, zone in result.all():
        timing = timing or EventTiming(
            event_id=event.id,
            start_at=aware(event.deadline) - timedelta(minutes=30),
            end_at=aware(event.deadline),
        )
        reminder_times = (await db.execute(
            select(Reminder.remind_at)
            .filter(Reminder.event_id == event.id, Reminder.status == ReminderStatus.PENDING)
            .order_by(Reminder.remind_at)
        )).scalars().all()
        entries.append(CalendarEntry(
            "telegram", event, timing, zone or "Europe/Moscow",
            [aware(value) for value in reminder_times],
        ))
    return entries


async def _max_entries(db: AsyncSession, max_user_id: int) -> list[CalendarEntry]:
    result = await db.execute(
        select(MaxEvent, MaxEventTiming, MaxUser.timezone)
        .join(MaxUser, MaxUser.id == MaxEvent.user_id)
        .outerjoin(MaxEventTiming, MaxEventTiming.event_id == MaxEvent.id)
        .filter(MaxUser.max_user_id == max_user_id, MaxEvent.status == "confirmed")
    )
    entries = []
    for event, timing, zone in result.all():
        timing = timing or MaxEventTiming(
            event_id=event.id,
            start_at=aware(event.deadline) - timedelta(minutes=30),
            end_at=aware(event.deadline),
        )
        reminder_times = (await db.execute(
            select(MaxReminder.remind_at)
            .filter(MaxReminder.event_id == event.id, MaxReminder.status == "pending")
            .order_by(MaxReminder.remind_at)
        )).scalars().all()
        entries.append(CalendarEntry(
            "max", event, timing, zone or "Europe/Moscow",
            [aware(value) for value in reminder_times],
        ))
    return entries


async def list_linked_events(
    db: AsyncSession, platform: str, external_id: int
) -> list[CalendarEntry]:
    identities = await linked_identities(db, platform, external_id)
    entries: list[CalendarEntry] = []
    if "telegram" in identities:
        entries.extend(await _telegram_entries(db, identities["telegram"]))
    if "max" in identities:
        entries.extend(await _max_entries(db, identities["max"]))
    return sorted(entries, key=lambda item: aware(item.timing.start_at))


async def get_owned_entry(
    db: AsyncSession,
    platform: str,
    external_id: int,
    event_ref: str | int,
) -> CalendarEntry | None:
    source, event_id = parse_ref(event_ref, platform)
    identities = await linked_identities(db, platform, external_id)
    if source not in identities:
        return None
    if source == "telegram":
        result = await db.execute(
            select(Event, EventTiming, User.timezone)
            .join(User, User.id == Event.user_id)
            .outerjoin(EventTiming, EventTiming.event_id == Event.id)
            .filter(Event.id == event_id, User.tg_id == identities[source])
        )
        row = result.first()
        if not row:
            return None
        event, timing, zone = row
        timing = timing or EventTiming(
            event_id=event.id,
            start_at=aware(event.deadline) - timedelta(minutes=30),
            end_at=aware(event.deadline),
        )
    else:
        result = await db.execute(
            select(MaxEvent, MaxEventTiming, MaxUser.timezone)
            .join(MaxUser, MaxUser.id == MaxEvent.user_id)
            .outerjoin(MaxEventTiming, MaxEventTiming.event_id == MaxEvent.id)
            .filter(MaxEvent.id == event_id, MaxUser.max_user_id == identities[source])
        )
        row = result.first()
        if not row:
            return None
        event, timing, zone = row
        timing = timing or MaxEventTiming(
            event_id=event.id,
            start_at=aware(event.deadline) - timedelta(minutes=30),
            end_at=aware(event.deadline),
        )
    reminder_model = Reminder if source == "telegram" else MaxReminder
    pending_status = ReminderStatus.PENDING if source == "telegram" else "pending"
    reminder_times = (await db.execute(
        select(reminder_model.remind_at)
        .filter(reminder_model.event_id == event.id, reminder_model.status == pending_status)
        .order_by(reminder_model.remind_at)
    )).scalars().all()
    return CalendarEntry(
        source, event, timing, zone or "Europe/Moscow",
        [aware(value) for value in reminder_times],
    )


async def find_linked_conflict(
    db: AsyncSession,
    platform: str,
    external_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_ref: str | None = None,
    all_day: bool = False,
) -> CalendarEntry | None:
    start, end = aware(start_at), aware(end_at)
    if all_day:
        return None
    for entry in await list_linked_events(db, platform, external_id):
        if exclude_ref and entry.ref == exclude_ref:
            continue
        if bool(getattr(entry.timing, "all_day", False)):
            continue
        if aware(entry.timing.start_at) < end and aware(entry.timing.end_at) > start:
            return entry
    return None


async def find_linked_all_day_overlaps(
    db: AsyncSession,
    platform: str,
    external_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_ref: str | None = None,
) -> list[CalendarEntry]:
    """Return existing all-day events intersecting a candidate interval.

    All-day and long-running events are allowed to coexist with timed events,
    so this is a non-blocking warning rather than a calendar conflict.
    """
    start, end = aware(start_at), aware(end_at)
    overlaps: list[CalendarEntry] = []
    for entry in await list_linked_events(db, platform, external_id):
        if exclude_ref and entry.ref == exclude_ref:
            continue
        timing = entry.timing
        is_all_day_or_long = (
            bool(getattr(timing, "all_day", False))
            or aware(timing.end_at) - aware(timing.start_at) >= timedelta(days=1)
        )
        if not is_all_day_or_long:
            continue
        if aware(timing.start_at) < end and aware(timing.end_at) > start:
            overlaps.append(entry)
    return overlaps


async def ensure_platform_user(db: AsyncSession, platform: str, external_id: int):
    if platform == "telegram":
        user = (await db.execute(select(User).filter(User.tg_id == external_id))).scalar_one_or_none()
        if not user:
            user = User(tg_id=external_id, timezone="Europe/Moscow")
            db.add(user)
    else:
        user = (await db.execute(
            select(MaxUser).filter(MaxUser.max_user_id == external_id)
        )).scalar_one_or_none()
        if not user:
            user = MaxUser(max_user_id=external_id, timezone="Europe/Moscow")
            db.add(user)
    await db.flush()
    return user


async def _replace_reminders(
    db: AsyncSession,
    entry: CalendarEntry,
    platform: str,
    external_id: int,
    explicit_times: list[datetime] | None = None,
) -> None:
    if explicit_times is None:
        times = await build_user_reminders(
            db, platform, external_id,
            entry.event.title,
            entry.event.description or "",
            aware(entry.timing.start_at),
            aware(entry.timing.end_at),
            entry.timezone_name,
            all_day=bool(getattr(entry.timing, "all_day", False)),
        )
    else:
        times = sorted({aware(value) for value in explicit_times})
    times = sorted({aware(value) for value in times})
    if any(value > aware(entry.timing.start_at) for value in times):
        raise ValueError("Уведомление должно быть не позже начала мероприятия")
    if entry.source == "telegram":
        reminders = (await db.execute(
            select(Reminder).filter(
                Reminder.event_id == entry.event.id,
                Reminder.status == ReminderStatus.PENDING,
            )
        )).scalars().all()
        for reminder in reminders:
            await db.delete(reminder)
        for remind_at in times:
            db.add(Reminder(
                event_id=entry.event.id,
                remind_at=remind_at,
                status=ReminderStatus.PENDING,
            ))
    else:
        reminders = (await db.execute(
            select(MaxReminder).filter(
                MaxReminder.event_id == entry.event.id,
                MaxReminder.status == "pending",
            )
        )).scalars().all()
        for reminder in reminders:
            await db.delete(reminder)
        for remind_at in times:
            db.add(MaxReminder(
                event_id=entry.event.id, remind_at=remind_at, status="pending"
            ))
    entry.reminders = list(times)


async def create_linked_event(
    db: AsyncSession,
    platform: str,
    external_id: int,
    title: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    reminder_times: list[datetime] | None = None,
    all_day: bool = False,
) -> CalendarEntry:
    user = await ensure_platform_user(db, platform, external_id)
    start = normalize_datetime(start_at, user.timezone)
    end = normalize_datetime(end_at, user.timezone)
    overlap = await find_linked_conflict(db, platform, external_id, start, end, all_day=all_day)
    if overlap:
        raise ValueError(f"Время пересекается с мероприятием «{overlap.event.title}»")
    if platform == "telegram":
        event = Event(
            user_id=user.id, title=title.strip(), description=description.strip(),
            deadline=end, status=EventStatus.CONFIRMED,
        )
        db.add(event)
        await db.flush()
        timing = EventTiming(event_id=event.id, start_at=start, end_at=end, all_day=all_day)
    else:
        event = MaxEvent(
            user_id=user.id, title=title.strip(), description=description.strip(),
            deadline=end, status="confirmed",
        )
        db.add(event)
        await db.flush()
        timing = MaxEventTiming(event_id=event.id, start_at=start, end_at=end, all_day=all_day)
    db.add(timing)
    entry = CalendarEntry(platform, event, timing, user.timezone)
    await _replace_reminders(
        db, entry, platform, external_id, explicit_times=reminder_times
    )
    await db.commit()
    _sync(entry)
    return entry


async def update_linked_event(
    db: AsyncSession,
    platform: str,
    external_id: int,
    event_ref: str | int,
    start_at: datetime,
    end_at: datetime,
    title: str | None = None,
    description: str | None = None,
    reminder_times: list[datetime] | None = None,
    all_day: bool | None = None,
) -> CalendarEntry:
    entry = await get_owned_entry(db, platform, external_id, event_ref)
    if not entry:
        raise LookupError("Мероприятие не найдено")
    start = normalize_datetime(start_at, entry.timezone_name)
    end = normalize_datetime(end_at, entry.timezone_name)
    is_all_day = bool(getattr(entry.timing, "all_day", False)) if all_day is None else all_day
    if end <= start:
        raise ValueError("Окончание должно быть позже начала")
    overlap = await find_linked_conflict(
        db, platform, external_id, start, end, exclude_ref=entry.ref, all_day=is_all_day
    )
    if overlap:
        raise ValueError(f"Время пересекается с мероприятием «{overlap.event.title}»")
    if title is not None:
        entry.event.title = title.strip()
    if description is not None:
        entry.event.description = description.strip()
    entry.event.deadline = end
    persisted_timing = await db.get(
        EventTiming if entry.source == "telegram" else MaxEventTiming,
        entry.event.id,
    )
    if persisted_timing:
        persisted_timing.start_at, persisted_timing.end_at = start, end
        persisted_timing.all_day = is_all_day
        entry.timing = persisted_timing
    else:
        entry.timing.start_at, entry.timing.end_at = start, end
        entry.timing.all_day = is_all_day
        db.add(entry.timing)
    await _replace_reminders(
        db, entry, platform, external_id, explicit_times=reminder_times
    )
    await db.commit()
    _sync(entry)
    return entry


async def toggle_linked_event(
    db: AsyncSession, platform: str, external_id: int, event_ref: str | int
) -> CalendarEntry:
    entry = await get_owned_entry(db, platform, external_id, event_ref)
    if not entry:
        raise LookupError("Мероприятие не найдено")
    entry.event.is_completed = not entry.event.is_completed
    await db.commit()
    return entry


async def delete_linked_event(
    db: AsyncSession, platform: str, external_id: int, event_ref: str | int
) -> CalendarEntry:
    entry = await get_owned_entry(db, platform, external_id, event_ref)
    if not entry:
        raise LookupError("Мероприятие не найдено")
    await db.delete(entry.event)
    await db.commit()
    if entry.source == "telegram":
        asyncio.create_task(delete_yandex_interval(entry.event.id))
    else:
        asyncio.create_task(delete_max_yandex(entry.event.id))
    return entry


def _sync(entry: CalendarEntry) -> None:
    if entry.source == "telegram":
        asyncio.create_task(sync_yandex_interval(
            entry.event.title, entry.timing.start_at, entry.timing.end_at,
            entry.event.description or "", event_id=entry.event.id,
            all_day=bool(getattr(entry.timing, "all_day", False)),
            timezone_name=entry.timezone_name,
        ))
    else:
        asyncio.create_task(sync_max_yandex(
            entry.event.title, entry.timing.start_at, entry.timing.end_at,
            entry.event.description or "", entry.event.id,
            all_day=bool(getattr(entry.timing, "all_day", False)),
            timezone_name=entry.timezone_name,
        ))


def payload(entry: CalendarEntry) -> dict:
    return {
        "id": entry.ref,
        "source": entry.source,
        "title": entry.event.title,
        "description": entry.event.description or "",
        "start": aware(entry.timing.start_at).isoformat(),
        "all_day": bool(getattr(entry.timing, "all_day", False)),
        "end": aware(entry.timing.end_at).isoformat(),
        "reminders": [aware(value).isoformat() for value in entry.reminders],
        "is_completed": entry.event.is_completed,
    }
