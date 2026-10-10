import logging
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


logger = logging.getLogger("ScheduleService")


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
# «КМБО-03-19» and «БИ-26-1» are group codes, not the dates 03-19 and 26-1.
DATE_TOKEN_RE = re.compile(
    r"(?<![\w-])(?<![\w]-)(\d{1,2})(?:[./-](\d{1,2})(?:[./-](\d{2,4}))?"
    r"|\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?)(?![\d])(?!-\w)",
    re.IGNORECASE,
)


# «кр. 1 н.» — every week except the first; «1,5,9,13 н.», «2-16 н.» — only
# those weeks of the semester. Written before a class title in many timetables.
WEEK_MARK_RE = re.compile(
    r"^\s*\(?\s*(кр\.?\s*)?((?:\d{1,2}\s*[-–,]\s*)*\d{1,2})\s*(?:нед|н)\.?(?![а-яё])\s*\)?\s*[,;:—-]?\s*",
    re.IGNORECASE,
)


# The same mark written after the title: «Программирование (кр. 1 н.)».
WEEK_MARK_TAIL_RE = re.compile(
    r"\s*[(\[,;—-]?\s*(кр\.?\s*)?((?:\d{1,2}\s*[-–,]\s*)*\d{1,2})\s*(?:нед|н)\.?(?![а-яё])\s*[)\]]?\s*$",
    re.IGNORECASE,
)


def _week_numbers(text: str) -> list[int]:
    weeks: set[int] = set()
    for part in re.split(r"\s*,\s*", text):
        bounds = [int(value) for value in re.split(r"\s*[-–]\s*", part) if value]
        if len(bounds) == 2 and bounds[0] <= bounds[1]:
            weeks.update(range(bounds[0], bounds[1] + 1))
        else:
            weeks.update(bounds)
    return sorted(week for week in weeks if 1 <= week <= 30)


def split_week_marks(slot: dict) -> dict:
    """Move a week mark from the title into `weeks` or `except_weeks`."""
    raw = slot.get("title") or ""
    match = WEEK_MARK_RE.match(raw)
    title = raw[match.end():].strip() if match else ""
    if not match:
        match = WEEK_MARK_TAIL_RE.search(raw)
        title = raw[:match.start()].strip() if match else ""
    if not match or not title:
        return slot
    slot = {**slot, "title": title[:1].upper() + title[1:]}
    weeks = _week_numbers(match[2])
    if weeks and not slot.get("occurrence_date"):
        slot["except_weeks" if match[1] else "weeks"] = weeks
    return slot


def week_marks_label(slot: dict) -> str:
    if slot.get("except_weeks"):
        return " · кроме нед. " + ", ".join(map(str, slot["except_weeks"]))
    if slot.get("weeks"):
        return " · нед. " + ", ".join(map(str, slot["weeks"]))
    return ""


def semester_start(valid_from: date) -> date:
    """Week 1 of a timetable: 1 September for an autumn term, else its first day.

    Universities number weeks (I/II, числитель/знаменатель) from the start of
    the semester, not by the calendar week of the year.
    """
    if valid_from.month >= 9 or valid_from.month == 1:
        start = date(valid_from.year - (valid_from.month == 1), 9, 1)
    else:
        start = valid_from
    if start.weekday() == 6:  # 1 September on a Sunday: classes start on Monday
        start += timedelta(days=1)
    return start


def academic_week(valid_from: date, day: date) -> int:
    start = semester_start(valid_from)
    return (day - (start - timedelta(days=start.weekday()))).days // 7 + 1


def _date_matches(text: str) -> list[re.Match]:
    """Date tokens; «1-2» or «3-4» without a year are class numbers, not dates."""
    return [
        match for match in DATE_TOKEN_RE.finditer(text)
        if not (match.group(2) and not match.group(3) and "-" in match.group(0))
    ]


def parse_date_range(text: str, today: date | None = None) -> tuple[date, date] | None:
    """Parse two Russian/numeric dates without involving the LLM."""
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    matches = _date_matches((text or "").casefold().replace("ё", "е"))
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
    years_given = start_year is not None or end_year is not None
    try:
        start = date(start_year or today.year, start_month, start_day)
        end = date(end_year or start.year, end_month, end_day)
    except ValueError:
        return None  # «31.02» or a number that is not a date
    if end < start and end_year is None:
        end = date(end.year + 1, end.month, end.day)
    if not years_given and end < today:
        # A period that has fully passed this year means the next one; a
        # current semester («с 1 сентября по 31 декабря» in October) stays.
        start, end = date(start.year + 1, start.month, start.day), date(end.year + 1, end.month, end.day)
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
    match = next(iter(_date_matches(normalized)), None)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else MONTHS[match.group(4)]
    raw_year = match.group(3) or match.group(5)
    year = int(raw_year) if raw_year else today.year
    if year < 100:
        year += 2000
    try:
        result = date(year, month, day)
        if raw_year is None and result < today - timedelta(days=1):
            result = date(year + 1, month, day)
    except ValueError:
        return None
    return result


TIME_ONLY_RE = re.compile(
    r"\b(?:в|к|с)\s+(?:[01]?\d|2[0-3])(?:[:.]\d{2})?\b", re.IGNORECASE
)
IMPORT_SELECTION_RE = re.compile(
    r"\b(?:группа|раздел|поток|курс|факультет)\b|\b[а-яё]{2,}-\d{2,4}\b",
    re.IGNORECASE,
)


BARE_PERIOD_TEXT = (
    "Это похоже на период для расписания, но файла или фото в ожидании нет. "
    "Пришлите расписание ещё раз и укажите период в подписи или следующим "
    "сообщением. Если это событие, добавьте его название, например: "
    "«отпуск с 1 по 7 ноября»."
)


def is_bare_period(text: str) -> bool:
    """Only two dates, as answered to the import question: not an event."""
    value = (text or "").casefold().replace("ё", "е")
    try:
        if parse_date_range(value) is None:
            return False
    except ValueError:
        return False
    rest = DATE_TOKEN_RE.sub(" ", value)
    words = re.findall(r"[а-яa-z]+", rest)
    filler = {"с", "по", "до", "от", "и", "период", "учебный", "семестр", "года", "год", "г"}
    return all(word in filler for word in words)


def supersedes_pending_import(text: str) -> bool:
    """Whether text is a new calendar request, not an import-file answer.

    A two-date period is a common answer to an import question and must be
    preserved. A single date such as "завтра" is a new event in normal chat.
    """
    value = (text or "").strip()
    if not value or parse_date_range(value) is not None:
        return False
    if parse_occurrence_date(value) is not None:
        return True
    return bool(TIME_ONLY_RE.search(value)) and not bool(IMPORT_SELECTION_RE.search(value))


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


async def discard_pending_imports(
    db: AsyncSession, platform: str, external_id: int
) -> tuple[int, int]:
    """Cancel an unfinished import and erase its retained source file."""
    account_id = await account_id_for(db, platform, external_id)
    sources = (await db.execute(
        select(ScheduleImportSource).filter(
            ScheduleImportSource.account_id == account_id,
            ScheduleImportSource.status.in_(ACTIVE_SOURCE_STATUSES),
        )
    )).scalars().all()
    drafts = (await db.execute(
        select(ScheduleImportDraft).filter(
            ScheduleImportDraft.account_id == account_id,
            ScheduleImportDraft.status.in_(ACTIVE_DRAFT_STATUSES),
        )
    )).scalars().all()
    for source in sources:
        source.status = "cancelled"
        source.content = b""
        source.prompt = ""
    for draft in drafts:
        draft.status = "cancelled"
        draft.slots = []
    if sources or drafts:
        await db.commit()
    return len(sources), len(drafts)


IMAGE_SOURCE_RE = re.compile(r"\.(?:jpe?g|png|webp|heic|gif|bmp)$", re.IGNORECASE)


def import_source_label(filename: str | None) -> str:
    """How an import is named in the history: the file or «Фото расписания»."""
    name = (filename or "").strip()
    if not name or IMAGE_SOURCE_RE.search(name):
        return "Фото расписания"
    return name[:255]


async def create_import_draft(
    db: AsyncSession, platform: str, external_id: int, extraction: dict,
    source_name: str | None = None,
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
    slots = [split_week_marks(slot) for slot in extraction["slots"]]
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
        source_name=source_name,
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
            f"{slot['title']}{parity + week_marks_label(slot) if not occurrence_date else ''}"
        )
    if len(draft.slots) > limit:
        lines.append(f"…и ещё {len(draft.slots) - limit}")
    lines.extend(["", "Добавить это расписание фоновым слоем календаря?"])
    return "\n".join(lines)


def import_summary(draft: ScheduleImportDraft, created: int, limit: int = 12) -> str:
    """A compact receipt for an import that was already committed."""
    exact_dates = bool(draft.slots) and all(
        slot.get("occurrence_date") for slot in draft.slots
    )
    span = (
        f"Даты: {draft.valid_from:%d.%m.%Y}–{draft.valid_until:%d.%m.%Y}"
        if exact_dates else f"Период: {draft.valid_from:%d.%m.%Y}–{draft.valid_until:%d.%m.%Y}"
    )
    lines = [
        "✅ Расписание добавлено в календарь.",
        f"Создано элементов: {created} из {len(draft.slots)} распознанных.",
        span,
        "",
        "Внесено:",
    ]
    for slot in draft.slots[:limit]:
        occurrence_date = slot.get("occurrence_date")
        day_label = (
            date.fromisoformat(occurrence_date).strftime("%d.%m.%Y")
            if occurrence_date else WEEKDAYS[int(slot["weekday"])]
        )
        lines.append(
            f"• {day_label} {slot["start_time"]}–{slot["end_time"]} {slot["title"]}"
            + ("" if occurrence_date else week_marks_label(slot))
        )
    if len(draft.slots) > limit:
        lines.append(f"…и ещё {len(draft.slots) - limit}")
    lines.extend([
        "",
        "Можно отменить только этот импорт или заменить расписание новым файлом. "
        "Позже это можно сделать в /history.",
    ])
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
            tuple(slot.get("weeks") or ()), tuple(slot.get("except_weeks") or ()),
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
        series = ScheduleSeries(
            account_id=account_id,
            title=slot["title"][:255],
            description=(slot.get("description") or "")[:1000],
            weekday=int(slot["weekday"]),
            start_time=start_clock,
            end_time=end_clock,
            valid_from=valid_from,
            valid_until=valid_until,
            week_pattern=slot.get("week_pattern", "every"),
            source=f"schedule_import:{draft.id}",
        )
        db.add(series)
        skipped = _weeks_outside_marks(series, slot)
        if skipped:
            await db.flush()
            db.add_all(
                ScheduleException(series_id=series.id, occurrence_date=day, status="skipped")
                for day in skipped
            )
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


async def undo_import(
    db: AsyncSession, platform: str, external_id: int, draft_id: int
) -> tuple[str, int]:
    """Remove only rows created by one completed import, never older schedules."""
    account_id = await account_id_for(db, platform, external_id)
    result = await db.execute(
        select(ScheduleImportDraft)
        .filter(
            ScheduleImportDraft.id == draft_id,
            ScheduleImportDraft.account_id == account_id,
        )
        .with_for_update()
    )
    draft = result.scalar_one_or_none()
    if not draft:
        return "missing", 0
    if draft.status == "undone":
        return "already_undone", 0
    if draft.status != "imported":
        return "not_imported", 0
    rows = (await db.execute(
        select(ScheduleSeries).filter(
            ScheduleSeries.account_id == account_id,
            ScheduleSeries.source == f"schedule_import:{draft.id}",
        )
    )).scalars().all()
    for series in rows:
        await db.delete(series)
    draft.status = "undone"
    await db.commit()
    return "undone", len(rows)


def _matches_week(series: ScheduleSeries, occurrence: date) -> bool:
    if series.week_pattern == "every":
        return True
    parity = academic_week(series.valid_from, occurrence) % 2
    return parity == (1 if series.week_pattern == "odd" else 0)


def _weeks_outside_marks(series: ScheduleSeries, slot: dict) -> list[date]:
    """Dates a «кр. 1 н.» or «1,5,9 н.» class does not take place."""
    weeks, excluded = set(slot.get("weeks") or ()), set(slot.get("except_weeks") or ())
    if not weeks and not excluded:
        return []
    skipped = []
    current = series.valid_from + timedelta(days=(series.weekday - series.valid_from.weekday()) % 7)
    while current <= series.valid_until:
        week = academic_week(series.valid_from, current)
        if _matches_week(series, current) and (week in excluded or (weeks and week not in weeks)):
            skipped.append(current)
        current += timedelta(days=7)
    return skipped


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


async def schedule_overlaps(
    db: AsyncSession, platform: str, external_id: int, start_at: datetime, end_at: datetime
) -> list[dict]:
    """Classes of imported timetables that run during a personal event."""
    zone = ZoneInfo("Europe/Moscow")
    items = await list_schedule_occurrences(
        db, platform, external_id,
        start_at.astimezone(zone).date(), end_at.astimezone(zone).date(),
    )
    return [
        item for item in items
        if datetime.fromisoformat(item["start"]) < end_at
        and datetime.fromisoformat(item["end"]) > start_at
    ]


async def schedule_overlap_note(db: AsyncSession, platform: str, external_id: int, timing) -> str:
    """Card text about classes during a timed event; never fails the card."""
    if bool(getattr(timing, "all_day", False)):
        return ""
    try:
        start, end = timing.start_at, timing.end_at
        if start.tzinfo is None:
            start, end = start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)
        return schedule_overlap_text(await schedule_overlaps(db, platform, external_id, start, end))
    except Exception as error:
        logger.warning("Schedule overlap check failed: %s", error)
        return ""


def schedule_overlap_text(items: list[dict]) -> str:
    """A non-blocking note: classes are a background layer and can be skipped."""
    if not items:
        return ""
    rows = "\n".join(
        f"• {item['start_local']}–{item['end_local']} {item['title']}" for item in items[:3]
    )
    more = "\n• …" if len(items) > 3 else ""
    return (
        f"\n📚 В это время по расписанию:\n{rows}{more}\n"
        "Событие всё равно можно добавить.\n"
    )


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
