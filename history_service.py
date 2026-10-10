"""History of additions: imported schedules and added events, newest first.

Shared by Telegram and MAX. Deleting an import removes every class it created
(and nothing else); deleting an event removes that event only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import account_id_for
from calendar_import_models import CalendarImport, CalendarImportItem
from calendar_import_service import KIND_LABELS, events_word, undo_calendar_import
from schedule_models import ScheduleImportDraft, ScheduleSeries
from schedule_service import undo_import
from unified_calendar import aware, delete_linked_event, get_owned_entry, list_linked_events


PAGE_SIZE = 8
ZONE = ZoneInfo("Europe/Moscow")


@dataclass
class HistoryItem:
    ref: str  # "imp:12", "cal:3" or "ev:t:45"
    kind: str  # "import" (a schedule), "calendar" (another calendar) or "event"
    title: str
    detail: str
    added_at: datetime
    deletable: bool


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _when(entry) -> str:
    start = aware(entry.timing.start_at).astimezone(ZONE)
    end = aware(entry.timing.end_at).astimezone(ZONE)
    if getattr(entry.timing, "all_day", False):
        last = (end - timedelta(days=1)).date()
        return f"{start:%d.%m.%Y}" + (f"–{last:%d.%m.%Y}" if last > start.date() else "") + " · весь день"
    return f"{start:%d.%m.%Y %H:%M}–{end:%H:%M}"


async def _import_items(db: AsyncSession, account_id: int) -> list[HistoryItem]:
    drafts = (await db.execute(
        select(ScheduleImportDraft).filter(
            ScheduleImportDraft.account_id == account_id,
            ScheduleImportDraft.status.in_(("imported", "undone")),
        )
    )).scalars().all()
    if not drafts:
        return []
    counts = dict((await db.execute(
        select(ScheduleSeries.source, func.count())
        .filter(ScheduleSeries.source.in_([f"schedule_import:{draft.id}" for draft in drafts]))
        .group_by(ScheduleSeries.source)
    )).all())
    items = []
    for draft in drafts:
        count = counts.get(f"schedule_import:{draft.id}", 0)
        if draft.status == "imported" and not count:
            continue  # a repeated file: its classes already belong to an earlier import
        exact = bool(draft.slots) and all(slot.get("occurrence_date") for slot in draft.slots)
        span = (
            f"{draft.valid_from:%d.%m.%Y}–{draft.valid_until:%d.%m.%Y}"
            if draft.valid_from and draft.valid_until else ""
        )
        if draft.status == "undone":
            detail = "импорт отменён"
        else:
            detail = f"{count} {_plural(count, 'занятие', 'занятия', 'занятий')}"
            if span:
                detail += (" · даты " if exact else " · период ") + span
        items.append(HistoryItem(
            ref=f"imp:{draft.id}", kind="import",
            title=draft.source_name or "Импорт расписания", detail=detail,
            added_at=aware(draft.updated_at or draft.created_at),
            deletable=draft.status == "imported",
        ))
    return items


async def _calendar_items(db: AsyncSession, account_id: int) -> list[HistoryItem]:
    records = (await db.execute(
        select(CalendarImport).filter(CalendarImport.account_id == account_id)
    )).scalars().all()
    if not records:
        return []
    counts = dict((await db.execute(
        select(CalendarImportItem.import_id, func.count())
        .filter(CalendarImportItem.import_id.in_([record.id for record in records]))
        .group_by(CalendarImportItem.import_id)
    )).all())
    return [HistoryItem(
        ref=f"cal:{record.id}", kind="calendar", title=record.source_name,
        detail=(
            "импорт отменён" if record.status != "imported"
            else f"{events_word(counts.get(record.id, 0))} · {KIND_LABELS.get(record.kind, 'импорт')}"
        ),
        added_at=aware(record.created_at or datetime.now(timezone.utc)),
        deletable=record.status == "imported",
    ) for record in records]


async def addition_history(
    db: AsyncSession, platform: str, external_id: int
) -> list[HistoryItem]:
    """Imports of the account and its confirmed events, newest first."""
    account_id = await account_id_for(db, platform, external_id)
    items = await _import_items(db, account_id) + await _calendar_items(db, account_id)
    # Events of a calendar import are listed as that one import.
    imported = set((await db.execute(
        select(CalendarImportItem.source, CalendarImportItem.event_id)
        .join(CalendarImport, CalendarImport.id == CalendarImportItem.import_id)
        .filter(CalendarImport.account_id == account_id, CalendarImport.status == "imported")
    )).all())
    for entry in await list_linked_events(db, platform, external_id):
        if (entry.source, entry.event.id) in imported:
            continue
        created = getattr(entry.event, "created_at", None)
        items.append(HistoryItem(
            ref=f"ev:{entry.ref}", kind="event", title=entry.event.title,
            detail=_when(entry),
            added_at=aware(created) if created else datetime.min.replace(tzinfo=timezone.utc),
            deletable=True,
        ))
    return sorted(items, key=lambda item: item.added_at, reverse=True)


def history_page(items: list[HistoryItem], offset: int = 0) -> tuple[str, list[HistoryItem], bool]:
    """Text of one page, its items (for buttons) and whether more remain."""
    page = items[offset:offset + PAGE_SIZE]
    if not items:
        return (
            "📜 История добавлений пока пуста. Здесь появятся импортированные "
            "расписания и добавленные события.", [], False,
        )
    lines = ["📜 История добавлений (сначала новые):", ""]
    for number, item in enumerate(page, offset + 1):
        icon = {"import": "📚", "calendar": "📆"}.get(item.kind, "📌")
        added = item.added_at.astimezone(ZONE)
        lines.append(f"{number}. {icon} {item.title}")
        lines.append(f"    {item.detail} · добавлено {added:%d.%m %H:%M}")
    lines += ["", "Нажмите 🗑 с номером, чтобы удалить. Импорт удаляется целиком — "
              "со всеми своими занятиями; другие события не затрагиваются."]
    return "\n".join(lines), page, offset + PAGE_SIZE < len(items)


async def find_history_item(
    db: AsyncSession, platform: str, external_id: int, ref: str
) -> HistoryItem | None:
    for item in await addition_history(db, platform, external_id):
        if item.ref == ref:
            return item
    return None


def deletion_question(item: HistoryItem) -> str:
    if item.kind == "calendar":
        return (
            f"Удалить импорт календаря «{item.title}» ({item.detail})?\n"
            "Все события этого импорта исчезнут. Остальные события останутся."
        )
    if item.kind == "import":
        return (
            f"Удалить импорт «{item.title}» ({item.detail})?\n"
            "Все занятия этого импорта исчезнут из календаря. Личные события и "
            "другие расписания останутся."
        )
    return f"Удалить событие «{item.title}» ({item.detail})?"


async def delete_history_item(
    db: AsyncSession, platform: str, external_id: int, ref: str
) -> str:
    """Delete an import (all its classes) or one event; return the reply."""
    if ref.startswith("imp:"):
        try:
            draft_id = int(ref.split(":", 1)[1])
        except ValueError:
            return "Запись не найдена."
        state, removed = await undo_import(db, platform, external_id, draft_id)
        if state == "undone":
            return (
                f"🗑 Импорт удалён: из календаря убрано {removed} "
                f"{_plural(removed, 'занятие', 'занятия', 'занятий')}."
            )
        if state == "already_undone":
            return "Этот импорт уже удалён."
        return "Импорт не найден."
    if ref.startswith("cal:"):
        try:
            import_id = int(ref.split(":", 1)[1])
        except ValueError:
            return "Запись не найдена."
        state, removed = await undo_calendar_import(db, platform, external_id, import_id)
        if state == "undone":
            return f"🗑 Импорт календаря удалён: убрано {events_word(removed)}."
        if state == "already_undone":
            return "Этот импорт уже удалён."
        return "Импорт не найден."
    if ref.startswith("ev:"):
        event_ref = ref.split(":", 1)[1]
        if not await get_owned_entry(db, platform, external_id, event_ref):
            return "Событие не найдено — возможно, оно уже удалено."
        entry = await delete_linked_event(db, platform, external_id, event_ref)
        return f"🗑 Событие удалено: «{entry.event.title}»."
    return "Запись не найдена."


def history_payload(item: HistoryItem) -> dict:
    """One row of the Mini App history tab."""
    return {
        "ref": item.ref,
        "kind": item.kind,
        "title": item.title,
        "detail": item.detail,
        "added_at": item.added_at.isoformat(),
        "deletable": item.deletable,
        "question": deletion_question(item),
    }
