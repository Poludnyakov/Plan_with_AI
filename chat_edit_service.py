"""Safe natural-language editing of confirmed calendar events."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from conversation_service import latest_event_ref
from interval_pipeline import normalize_datetime
from unified_calendar import (
    CalendarEntry,
    entry_is_past,
    entry_is_upcoming,
    get_owned_entry,
    list_linked_events,
    update_linked_event,
)


EDIT_RE = re.compile(
    r"^\s*(?:измени(?:ть)?|поменяй|перенеси(?:ть)?|сдвинь|сдвинуть|"
    r"передвинь|передвинуть|переименуй|назови|исправь|редактируй)\b",
    re.IGNORECASE,
)
PRONOUN_RE = re.compile(r"\b(?:его|её|ее|это|мероприятие)\b", re.IGNORECASE)
RENAME_RE = re.compile(
    r"\b(?:переименуй|назови|назвать|название|имя мероприятия)\b", re.IGNORECASE
)
DETAIL_RE = re.compile(r"\b(?:описание|детали|место|аудитория)\b", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:сегодня|завтра|послезавтра|понедельник\w*|вторник\w*|сред\w*|"
    r"четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b|"
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
    r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b(?:в|к|с|до)\s+(?:[01]?\d|2[0-3])(?:[:.]\d{2})?\b|"
    r"\b(?:утром|дн[её]м|вечером|ночью|полдень|полночь)\b",
    re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(
    r"(?:\bс\s+\d{1,2}(?::\d{2})?\s+до\s+\d{1,2}(?::\d{2})?\b|"
    r"\b\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2}\b)",
    re.IGNORECASE,
)
ALL_DAY_RE = re.compile(r"\b(?:весь|целый)\s+день\b", re.IGNORECASE)
ALIASES = {"кр": "контрольная", "к/р": "контрольная", "контр": "контрольная"}


@dataclass
class ChatEditResult:
    status: str
    entry: CalendarEntry | None = None
    candidates: list[CalendarEntry] = field(default_factory=list)


def is_edit_request(text: str) -> bool:
    return bool(EDIT_RE.match(text or ""))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _normalise_title(value: str) -> str:
    tokens = re.findall(r"[a-zа-я0-9/]+", (value or "").casefold().replace("ё", "е"))
    return " ".join(ALIASES.get(token, token) for token in tokens)


def _similarity(left: str, right: str) -> float:
    left, right = _normalise_title(left), _normalise_title(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.96
    overlap = len(set(left.split()) & set(right.split())) / max(len(set(left.split())), 1)
    return max(SequenceMatcher(None, left, right).ratio(), overlap * 0.9)


def _is_confirmed(entry: CalendarEntry) -> bool:
    status = getattr(entry.event, "status", "")
    return str(getattr(status, "value", status)).lower() == "confirmed"


async def _resolve_target(
    db: AsyncSession,
    platform: str,
    external_id: int,
    raw_text: str,
    desired: dict,
) -> ChatEditResult:
    last_ref = await latest_event_ref(db, platform, external_id)
    if last_ref and PRONOUN_RE.search(raw_text):
        recent = await get_owned_entry(db, platform, external_id, last_ref)
        if recent:
            if not _is_confirmed(recent):
                return ChatEditResult("draft")
            if entry_is_past(recent):
                return ChatEditResult("past")
            if not entry_is_upcoming(recent):
                return ChatEditResult("missing")
            return ChatEditResult("target", recent)

    entries = await list_linked_events(db, platform, external_id)
    confirmed = [entry for entry in entries if _is_confirmed(entry)]
    upcoming = [entry for entry in confirmed if entry_is_upcoming(entry)]
    title = str(desired.get("title") or "")

    def score(entry: CalendarEntry) -> float:
        return max(
            _similarity(title, entry.event.title),
            _similarity(entry.event.title, raw_text),
        )

    candidates = [entry for entry in upcoming if score(entry) >= 0.45]
    candidates.sort(key=lambda entry: _aware(entry.timing.start_at))
    if len(candidates) == 1:
        return ChatEditResult("target", candidates[0])
    if len(candidates) > 1:
        return ChatEditResult("ambiguous", candidates=candidates[:5])
    if len(upcoming) == 1:
        return ChatEditResult("target", upcoming[0])

    archived_matches = [
        entry for entry in confirmed
        if entry_is_past(entry) and score(entry) >= 0.45
    ]
    if archived_matches:
        return ChatEditResult("past", candidates=archived_matches[:5])
    return ChatEditResult("missing")


def _merged_interval(entry: CalendarEntry, desired: dict, raw_text: str) -> tuple[datetime, datetime, bool]:
    zone = ZoneInfo(entry.timezone_name or "Europe/Moscow")
    old_start = _aware(entry.timing.start_at).astimezone(zone)
    old_end = _aware(entry.timing.end_at).astimezone(zone)
    parsed_start = normalize_datetime(desired["start_at"], entry.timezone_name).astimezone(zone)
    parsed_end = normalize_datetime(desired["end_at"], entry.timezone_name).astimezone(zone)
    has_date, has_time = bool(DATE_RE.search(raw_text)), bool(TIME_RE.search(raw_text))
    explicit_all_day = bool(ALL_DAY_RE.search(raw_text))
    old_all_day = bool(getattr(entry.timing, "all_day", False))

    if explicit_all_day:
        all_day = True
    elif has_time:
        all_day = False
    else:
        all_day = old_all_day

    if all_day:
        start_day = parsed_start.date() if has_date else old_start.date()
        if explicit_all_day and has_date:
            end_day = parsed_end.date()
            if end_day <= start_day:
                end_day = start_day + timedelta(days=1)
        else:
            old_days = max(1, (old_end.date() - old_start.date()).days)
            end_day = start_day + timedelta(days=old_days)
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(end_day, datetime.min.time(), tzinfo=zone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc), True

    date_value = parsed_start.date() if has_date else old_start.date()
    time_value = parsed_start.timetz().replace(tzinfo=None) if has_time else old_start.timetz().replace(tzinfo=None)
    start = datetime.combine(date_value, time_value, tzinfo=zone)
    duration = parsed_end - parsed_start if TIME_RANGE_RE.search(raw_text) else old_end - old_start
    if duration <= timedelta(0):
        duration = old_end - old_start
    return start.astimezone(timezone.utc), (start + duration).astimezone(timezone.utc), False


async def apply_chat_edit(
    db: AsyncSession,
    platform: str,
    external_id: int,
    raw_text: str,
    extracted: list[dict],
) -> ChatEditResult:
    """Update exactly one confirmed event; never guess when several match."""
    if not is_edit_request(raw_text):
        return ChatEditResult("not_edit")
    if not extracted:
        return ChatEditResult("invalid")
    target = await _resolve_target(db, platform, external_id, raw_text, extracted[0])
    if target.status != "target" or target.entry is None:
        return target
    entry = target.entry
    start, end, all_day = _merged_interval(entry, extracted[0], raw_text)
    title = str(extracted[0].get("title") or "").strip() if RENAME_RE.search(raw_text) else None
    description = str(extracted[0].get("description") or "") if DETAIL_RE.search(raw_text) else None
    updated = await update_linked_event(
        db, platform, external_id, entry.ref, start, end,
        title=title, description=description, all_day=all_day,
    )
    return ChatEditResult("updated", updated)
