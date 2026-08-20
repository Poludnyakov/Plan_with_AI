import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from interval_ai_service import extract_intervals
from interval_calendar_sync import delete_yandex_interval
from interval_models import EventTiming
from models import Event, EventStatus, User
from unified_calendar import delete_linked_event, get_owned_entry, list_linked_events


logger = logging.getLogger("EventCancellationHandlers")
router = Router(name="event_cancellation")

CANCEL_COMMAND_PATTERN = r"(?i)^\s*(?:отмена|отмени|отменить|удали|удалить)\b"
CANCEL_PREFIX_RE = re.compile(
    r"^\s*(?:отмена|отмени|отменить|удали|удалить)\b[\s:,.!—-]*(.*)$",
    re.IGNORECASE,
)
DATE_HINT_RE = re.compile(
    r"\b(?:сегодня|завтра|послезавтра|"
    r"понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b"
    r"|\b\d{1,2}[.\/-]\d{1,2}(?:[.\/-]\d{2,4})?\b"
    r"|\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b",
    re.IGNORECASE,
)
TIME_HINT_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b(?:в|к|с|до)\s+(?:[01]?\d|2[0-3])(?:[:.]\d{2})?\b"
    r"|\b(?:в|к|с|до)\s+(?:час(?:а|у|ов)?|полдень|полночь|один|два|двух|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать)\b",
    re.IGNORECASE,
)
TOKEN_ALIASES = {
    "кр": "контрольная",
    "к/р": "контрольная",
    "контр": "контрольная",
}


def strip_cancel_prefix(text: str) -> str:
    match = CANCEL_PREFIX_RE.match(text or "")
    return match.group(1).strip() if match else ""


def normalize_title(text: str) -> str:
    value = (text or "").casefold().replace("ё", "е")
    tokens = re.findall(r"[a-zа-я0-9/]+", value)
    return " ".join(TOKEN_ALIASES.get(token, token) for token in tokens)


def title_similarity(query: str, title: str) -> float:
    normalized_query = normalize_title(query)
    normalized_title = normalize_title(title)
    if not normalized_query or not normalized_title:
        return 0.0
    if normalized_query == normalized_title:
        return 1.0
    if normalized_query in normalized_title or normalized_title in normalized_query:
        return 0.96
    query_tokens = set(normalized_query.split())
    title_tokens = set(normalized_title.split())
    coverage = len(query_tokens & title_tokens) / max(len(query_tokens), 1)
    sequence = SequenceMatcher(None, normalized_query, normalized_title).ratio()
    return max(sequence, coverage * 0.9)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def candidate_start(event: Event, timing: EventTiming | None) -> datetime:
    if timing:
        return ensure_aware(timing.start_at)
    return ensure_aware(event.deadline) - timedelta(minutes=30)


def candidate_end(event: Event, timing: EventTiming | None) -> datetime:
    if timing:
        return ensure_aware(timing.end_at)
    return ensure_aware(event.deadline)


def format_candidate(event: Event, timing: EventTiming | None, timezone_name: str) -> str:
    local_timezone = ZoneInfo(timezone_name or "Europe/Moscow")
    start = candidate_start(event, timing).astimezone(local_timezone)
    end = candidate_end(event, timing).astimezone(local_timezone)
    return f"{event.title} — {start:%d.%m.%Y, %H:%M}–{end:%H:%M}"


async def load_user_events(db: AsyncSession, tg_id: int):
    entries = await list_linked_events(db, "telegram", tg_id)
    for entry in entries:
        entry.event._calendar_ref = entry.ref
    return [(entry.event, entry.timing, entry.timezone_name) for entry in entries]


async def find_cancellation_candidates(query: str, rows):
    date_hint = bool(DATE_HINT_RE.search(query))
    time_hint = bool(TIME_HINT_RE.search(query))
    search_title = query
    target_start = None
    if date_hint or time_hint:
        try:
            from handlers.pipeline_handlers_intervals import pipeline

            safe_query = pipeline.anonymizer.anonymize_text(query)
            extracted = await extract_intervals(safe_query)
            if extracted:
                search_title = extracted[0].get("title") or query
                target_start = ensure_aware(extracted[0]["start_at"])
        except Exception as error:
            logger.warning("Could not parse cancellation details: %s", error, exc_info=True)

    scored = []
    for event, timing, timezone_name in rows:
        score = title_similarity(search_title, event.title)
        if score < 0.45:
            continue
        if target_start is not None:
            local_timezone = ZoneInfo(timezone_name or "Europe/Moscow")
            event_local = candidate_start(event, timing).astimezone(local_timezone)
            target_local = target_start.astimezone(local_timezone)
            if date_hint and event_local.date() != target_local.date():
                continue
            if time_hint:
                event_minutes = event_local.hour * 60 + event_local.minute
                target_minutes = target_local.hour * 60 + target_local.minute
                if abs(event_minutes - target_minutes) > 10:
                    continue
        scored.append((score, event, timing, timezone_name or "Europe/Moscow"))

    scored.sort(key=lambda item: (-item[0], candidate_start(item[1], item[2])))
    return scored


async def delete_event_and_answer(
    message: Message,
    db: AsyncSession,
    event: Event,
    timing: EventTiming | None,
    timezone_name: str,
) -> None:
    description = format_candidate(event, timing, timezone_name)
    event_id = event.id
    event_ref = getattr(event, "_calendar_ref", None)
    if event_ref:
        await delete_linked_event(db, "telegram", message.from_user.id, event_ref)
    else:
        was_confirmed = event.status == EventStatus.CONFIRMED
        await db.delete(event)
        await db.commit()
        if was_confirmed:
            asyncio.create_task(delete_yandex_interval(event_id))
    await message.answer(f"🗑 Удалено: {description}")


@router.message(F.text.regexp(CANCEL_COMMAND_PATTERN))
async def handle_cancellation_message(message: Message, db_session: AsyncSession):
    query = strip_cancel_prefix(message.text or "")
    if not query:
        await message.answer(
            "Напишите, какое мероприятие удалить. Например: «отмена контрольная по русскому»."
        )
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        rows = await load_user_events(db_session, message.from_user.id)
        candidates = await find_cancellation_candidates(query, rows)
        if not candidates:
            await message.answer(
                "Не нашёл подходящее мероприятие. Уточните название, дату или время."
            )
            return
        if len(candidates) == 1:
            _, event, timing, timezone_name = candidates[0]
            await delete_event_and_answer(message, db_session, event, timing, timezone_name)
            return

        builder = InlineKeyboardBuilder()
        lines = ["Нашёл несколько мероприятий. Выберите, какое удалить:"]
        for index, (_, event, timing, timezone_name) in enumerate(candidates[:8], 1):
            description = format_candidate(event, timing, timezone_name)
            lines.append(f"{index}. {description}")
            button_title = event.title
            if len(button_title) > 32:
                button_title = button_title[:29] + "..."
            start = candidate_start(event, timing).astimezone(ZoneInfo(timezone_name))
            builder.button(
                text=f"🗑 {button_title} · {start:%d.%m %H:%M}",
                callback_data=f"delete_event:{getattr(event, '_calendar_ref', event.id)}",
            )
        builder.adjust(1)
        if len(candidates) > 8:
            lines.append("Показаны первые 8 вариантов. Уточните запрос датой и временем.")
        await message.answer("\n".join(lines), reply_markup=builder.as_markup())
    except Exception as error:
        await db_session.rollback()
        logger.error("Cancellation search failed: %s", error, exc_info=True)
        await message.answer("❌ Не удалось найти мероприятие для удаления.")


@router.callback_query(F.data.startswith("delete_event:"))
async def handle_delete_event_callback(callback: CallbackQuery, db_session: AsyncSession):
    try:
        event_ref = callback.data.split(":", 1)[1]
        entry = await get_owned_entry(
            db_session, "telegram", callback.from_user.id, event_ref
        )
        if not entry:
            await callback.answer("Событие уже удалено или не найдено.", show_alert=True)
            return
        event, timing, timezone_name = entry.event, entry.timing, entry.timezone_name
        description = format_candidate(event, timing, timezone_name)
        await delete_linked_event(
            db_session, "telegram", callback.from_user.id, event_ref
        )
        await callback.answer("🗑 Удалено")
        if callback.message:
            await callback.message.edit_text(f"🗑 Удалено: {description}")
    except Exception as error:
        await db_session.rollback()
        logger.error("Event deletion failed: %s", error, exc_info=True)
        try:
            await callback.answer("❌ Не удалось удалить мероприятие.", show_alert=True)
        except Exception:
            pass
