import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import account_id_for
from schedule_models import (
    ScheduleException,
    ScheduleImportDraft,
    ScheduleImportSource,
    ScheduleSeries,
)


ACTIVE_DRAFT_STATUSES = {"awaiting_range", "ready"}
ACTIVE_SOURCE_STATUSES = {"awaiting_input", "processing"}
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS = {
    "января": 1, "январь": 1, "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3, "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5, "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7, "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9, "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11, "декабря": 12, "декабрь": 12,
}
DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?:[./-](\d{1,2})(?:[./-](\d{2,4}))?"
    r"|\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?)(?!\d)",
    re.IGNORECASE,
)


def parse_date_range(text: str, today: date | None = None) -> tuple[date, date] | None:
    """Parse two Russian/numeric dates without involving the LLM."""
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    matches = list(DATE_TOKEN_RE.finditer((text or "").casefold().replace("ё", "е")))
    if len(matches) < 2:
        return None

    def parts(match):
        day = int(match.group(1))
        month = int(match.group(2)) if match.group(2) else MONTHS[match.group(4)]
        raw_year = match.group(3) or match.group(5)
        year = int(raw_year) if raw_year else None
        if year is not None and year < 100:
            year += 2000
        return day, month, year

    start_day, start_month, start_year = parts(matches[0])
    end_day, end_month, end_year = parts(matches[1])
    if start_year is None:
        start_year = today.year
        candidate = date(start_year, start_month, start_day)
        if candidate < today - timedelta(days=31):
            start_year += 1
    start = date(start_year, start_month, start_day)
    end_year = end_year or start.year
    end = date(end_year, end_month, end_day)
    if end < start and matches[1].group(3) is None and matches[1].group(5) is None:
        end = date(end.year + 1, end.month, end.day)
    if end < start:
        raise ValueError("Конец периода должен быть не раньше начала.")
    if end - start > timedelta(days=370):
        raise ValueError("Период расписания не может превышать 370 дней.")
    return start, end


def parse_occurrence_date(text: str, today: date | None = None) -> date | None:
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    normalized = (text or "").casefold().replace("ё", "е")
    if re.search(r"\bпослезавтра\b", normalized):
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", normalized):
        return today + timedelta(days=1)
    if re.search(r"\bсегодня\b", normalized):
        return today
    match = DATE_TOKEN_RE.search(normalized)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else MONTHS[match.group(4)]
    raw_year = match.group(3) or match.group(5)
    year = int(raw_year) if raw_year else today.year
    if year < 100:
        year += 2000
    result = date(year, month, day)
    if raw_year is None and result < today - timedelta(days=1):
        result = date(year + 1, month, day)
    return result


async def pending_draft(
    db: AsyncSession, platform: str, external_id: int
) -> ScheduleImportDraft | None:
    account_id = await account_id_for(db, platform, external_id)
    result = await db.execute(
        select(ScheduleImportDraft)
        .filter(
            ScheduleImportDraft.account_id == account_id,
            ScheduleImportDraft.status.in_(ACTIVE_DRAFT_STATUSES),
        )
        .order_by(ScheduleImportDraft.id.desc())
    )
    return result.scalars().first()


async def save_import_source(
    db: AsyncSession,
    platform: str,
    external_id: int,
    content: bytes,
    filename: str,
    prompt: str = "",
) -> ScheduleImportSource:
    account_id = await account_id_for(db, platform, external_id)
    old_sources = (await db.execute(
        select(ScheduleImportSource).filter(
            ScheduleImportSource.account_id == account_id,
            ScheduleImportSource.status.in_(ACTIVE_SOURCE_STATUSES),
        )
    )).scalars().all()
    for item in old_sources:
        item.status = "superseded"
        item.content = b""
    source = ScheduleImportSource(
        account_id=account_id,
        source_platform=platform,
        filename=(filename or "schedule")[:255],
        content=content,
        prompt=(prompt or "")[:4000],
        status="awaiting_input",
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


async def pending_import_source(
    db: AsyncSession, platform: str, external_id: int
) -> ScheduleImportSource | None:
    account_id = await account_id_for(db, platform, external_id)
    result = await db.execute(
        select(ScheduleImportSource)
        .filter(
            ScheduleImportSource.account_id == account_id,
            ScheduleImportSource.status.in_(ACTIVE_SOURCE_STATUSES),
        )
        .order_by(ScheduleImportSource.id.desc())
    )
    return result.scalars().first()


async def update_import_source_prompt(
    db: AsyncSession, source: ScheduleImportSource, prompt: str
) -> ScheduleImportSource:
    source.prompt = (prompt or "")[:4000]
    source.status = "awaiting_input"
    await db.commit()
    return source


async def finish_import_source(
    db: AsyncSession, source: ScheduleImportSource, status: str = "processed"
) -> None:
    source.status = status
    source.content = b""
    await db.commit()


async def create_import_draft(
    db: AsyncSession, platform: str, external_id: int, extraction: dict
) -> ScheduleImportDraft:
    account_id = await account_id_for(db, platform, external_id)
    old = (await db.execute(
        select(ScheduleImportDraft).filter(
            ScheduleImportDraft.account_id == account_id,
            ScheduleImportDraft.status.in_(ACTIVE_DRAFT_STATUSES),
        )
    )).scalars().all()
    for item in old:
        item.status = "superseded"
    slots = list(extraction["slots"])
    exact_dates = [
        date.fromisoformat(slot["occurrence_date"])
        for slot in slots if slot.get("occurrence_date")
    ]
    all_rows_are_exact = bool(slots) and len(exact_dates) == len(slots)
    draft = ScheduleImportDraft(
        account_id=account_id,
        source_platform=platform,
        slots=slots,
        confidence=float(extraction.get("confidence", 0.0)),
        status="ready" if all_rows_are_exact else "awaiting_range",
        valid_from=min(exact_dates) if all_rows_are_exact else None,
        valid_until=max(exact_dates) if all_rows_are_exact else None,
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return draft


async def set_draft_range(
    db: AsyncSession, draft: ScheduleImportDraft, valid_from: date, valid_until: date
) -> ScheduleImportDraft:
    draft.valid_from, draft.valid_until, draft.status = valid_from, valid_until, "ready"
    await db.commit()
    return draft


def import_preview(draft: ScheduleImportDraft, limit: int = 12) -> str:
    lines = [
        f"Распознано интервалов: {len(draft.slots)}",
        (
            f"Даты: {draft.valid_from:%d.%m.%Y}–{draft.valid_until:%d.%m.%Y}"
            if draft.slots and all(slot.get("occurrence_date") for slot in draft.slots)
            else f"Период: {draft.valid_from:%d.%m.%Y}–{draft.valid_until:%d.%m.%Y}"
        ),
        "",
    ]
    for slot in draft.slots[:limit]:
        occurrence_date = slot.get("occurrence_date")
        parity = {"odd": " · нечёт.", "even": " · чёт.", "every": ""}.get(
            slot.get("week_pattern"), ""
        )
        day_label = (
            date.fromisoformat(occurrence_date).strftime("%d.%m.%Y")
            if occurrence_date else WEEKDAYS[int(slot["weekday"])]
        )
        lines.append(
            f"• {day_label} {slot['start_time']}–{slot['end_time']} "
            f"{slot['title']}{parity if not occurrence_date else ''}"
        )
    if len(draft.slots) > limit:
        lines.append(f"…и ещё {len(draft.slots) - limit}")
    lines.extend(["", "Добавить это расписание фоновым слоем календаря?"])
    return "\n".join(lines)


async def confirm_import(
    db: AsyncSession, platform: str, external_id: int, draft_id: int
) -> tuple[str, int]:
    account_id = await account_id_for(db, platform, external_id)
    result = await db.execute(
        select(ScheduleImportDraft)
        .filter(ScheduleImportDraft.id == draft_id, ScheduleImportDraft.account_id == account_id)
        .with_for_update()
    )
    draft = result.scalar_one_or_none()
    if not draft:
        return "missing", 0
    if draft.status == "imported":
        return "imported", 0
    if draft.status != "ready" or not draft.valid_from or not draft.valid_until:
        return "not_ready", 0

    created = 0
    seen: set[tuple] = set()
    for slot in draft.slots:
        start_clock = time.fromisoformat(slot["start_time"])
        end_clock = time.fromisoformat(slot["end_time"])
        occurrence_date = (
            date.fromisoformat(slot["occurrence_date"])
            if slot.get("occurrence_date") else None
        )
        valid_from = occurrence_date or draft.valid_from
        valid_until = occurrence_date or draft.valid_until
        if occurrence_date and not (draft.valid_from <= occurrence_date <= draft.valid_until):
            continue
        signature = (
            slot["title"], int(slot["weekday"]), start_clock, end_clock,
            slot.get("week_pattern", "every"), valid_from, valid_until,
        )
        if signature in seen:
            continue
        seen.add(signature)
        filters = (
            ScheduleSeries.account_id == account_id,
            ScheduleSeries.title == slot["title"],
            ScheduleSeries.weekday == int(slot["weekday"]),
            ScheduleSeries.start_time == start_clock,
            ScheduleSeries.end_time == end_clock,
            ScheduleSeries.valid_from == valid_from,
            ScheduleSeries.valid_until == valid_until,
            ScheduleSeries.week_pattern == slot.get("week_pattern", "every"),
        )
        if (await db.execute(select(ScheduleSeries.id).filter(*filters))).scalar_one_or_none():
            continue
        db.add(ScheduleSeries(
            account_id=account_id,
            title=slot["title"][:255],
            description=(slot.get("description") or "")[:1000],
            weekday=int(slot["weekday"]),
            start_time=start_clock,
            end_time=end_clock,
            valid_from=valid_from,
            valid_until=valid_until,
            week_pattern=slot.get("week_pattern", "every"),
            source="schedule_import",
        ))
        created += 1
    draft.status = "imported"
    await db.commit()
    return "created", created


async def cancel_import(
    db: AsyncSession, platform: str, external_id: int, draft_id: int
) -> bool:
    account_id = await account_id_for(db, platform, external_id)
    draft = await db.get(ScheduleImportDraft, draft_id)
    if not draft or draft.account_id != account_id or draft.status not in ACTIVE_DRAFT_STATUSES:
        return False
    draft.status = "cancelled"
    await db.commit()
    return True


def _matches_week(series: ScheduleSeries, occurrence: date) -> bool:
    if series.week_pattern == "every":
        return True
    parity = occurrence.isocalendar().week % 2
    return parity == (1 if series.week_pattern == "odd" else 0)


async def list_schedule_occurrences(
    db: AsyncSession,
    platform: str,
    external_id: int,
    range_start: date | None = None,
    range_end: date | None = None,
) -> list[dict]:
    account_id = await account_id_for(db, platform, external_id)
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    range_start = range_start or today - timedelta(days=31)
    range_end = range_end or today + timedelta(days=370)
    series_rows = (await db.execute(
        select(ScheduleSeries).filter(
            ScheduleSeries.account_id == account_id,
            ScheduleSeries.valid_until >= range_start,
            ScheduleSeries.valid_from <= range_end,
        )
    )).scalars().all()
    if not series_rows:
        return []
    series_ids = [item.id for item in series_rows]
    skipped = set((await db.execute(
        select(ScheduleException.series_id, ScheduleException.occurrence_date).filter(
            ScheduleException.series_id.in_(series_ids),
            ScheduleException.status == "skipped",
        )
    )).all())
    payloads = []
    for series in series_rows:
        current = max(series.valid_from, range_start)
        current += timedelta(days=(series.weekday - current.weekday()) % 7)
        until = min(series.valid_until, range_end)
        zone = ZoneInfo(series.timezone)
        while current <= until:
            if (series.id, current) not in skipped and _matches_week(series, current):
                start = datetime.combine(current, series.start_time, zone).astimezone(timezone.utc)
                end = datetime.combine(current, series.end_time, zone).astimezone(timezone.utc)
                payloads.append({
                    "id": f"s:{series.id}:{current.isoformat()}",
                    "kind": "schedule",
                    "series_id": series.id,
                    "source": "schedule",
                    "title": series.title,
                    "description": series.description,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "start_local": series.start_time.strftime("%H:%M"),
                    "end_local": series.end_time.strftime("%H:%M"),
                    "reminders": [],
                    "is_completed": False,
                })
            current += timedelta(days=7)
    return sorted(payloads, key=lambda item: item["start"])


async def skip_candidates(
    db: AsyncSession, platform: str, external_id: int, target: date, query: str
) -> list[dict]:
    items = await list_schedule_occurrences(db, platform, external_id, target, target)
    words = set(re.findall(r"[a-zа-я0-9]+", query.casefold().replace("ё", "е")))
    noise = {"не", "иду", "пойду", "пропущу", "пропускаю", "пару", "занятие", "сегодня", "завтра"}
    wanted = words - noise
    if wanted:
        matched = [item for item in items if wanted & set(re.findall(r"[a-zа-я0-9]+", item["title"].casefold().replace("ё", "е")))]
        if matched:
            items = matched
    return items


async def skip_occurrence(
    db: AsyncSession, platform: str, external_id: int, occurrence_ref: str
) -> bool:
    match = re.fullmatch(r"s:(\d+):(\d{4}-\d{2}-\d{2})", occurrence_ref)
    if not match:
        return False
    account_id = await account_id_for(db, platform, external_id)
    series = await db.get(ScheduleSeries, int(match.group(1)))
    occurrence = date.fromisoformat(match.group(2))
    if not series or series.account_id != account_id:
        return False
    existing = (await db.execute(select(ScheduleException).filter(
        ScheduleException.series_id == series.id,
        ScheduleException.occurrence_date == occurrence,
    ))).scalar_one_or_none()
    if existing:
        return True
    db.add(ScheduleException(series_id=series.id, occurrence_date=occurrence, status="skipped"))
    await db.commit()
    return True
