from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.event_cancellation import (
    find_cancellation_candidates,
    strip_cancel_prefix,
    title_similarity,
)
from handlers.pipeline_handlers_intervals import handle_confirm_callback
from interval_models import EventTiming
from models import Event, EventStatus


def make_event(event_id: int, title: str, start: datetime) -> tuple[Event, EventTiming, str]:
    event = Event(
        id=event_id,
        user_id=1,
        title=title,
        deadline=start + timedelta(hours=1),
        status=EventStatus.CONFIRMED,
    )
    timing = EventTiming(
        event_id=event_id,
        start_at=start,
        end_at=start + timedelta(hours=1),
    )
    return event, timing, "Europe/Moscow"


def test_cancel_prefix_and_title_abbreviation():
    assert strip_cancel_prefix("отмена кр по русскому") == "кр по русскому"
    assert title_similarity("кр по русскому", "Контрольная по русскому") > 0.9


@pytest.mark.anyio
async def test_plain_cancel_returns_all_matching_user_events():
    first = make_event(
        1,
        "Контрольная по русскому",
        datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
    )
    second = make_event(
        2,
        "Контрольная по русскому",
        datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
    )

    with patch(
        "handlers.event_cancellation.extract_intervals", new_callable=AsyncMock
    ) as extract:
        candidates = await find_cancellation_candidates(
            "кр по русскому", [first, second]
        )

    extract.assert_not_awaited()
    assert [item[1].id for item in candidates] == [1, 2]


@pytest.mark.anyio
async def test_detailed_cancel_uses_ai_date_and_time_to_choose_one():
    first = make_event(
        1,
        "Контрольная по русскому",
        datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
    )
    second = make_event(
        2,
        "Контрольная по русскому",
        datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
    )
    extracted = [{
        "title": "Контрольная по русскому",
        "start_at": datetime(2026, 8, 20, 10, 0, tzinfo=timezone(timedelta(hours=3))),
        "end_at": datetime(2026, 8, 20, 11, 0, tzinfo=timezone(timedelta(hours=3))),
        "description": "",
    }]

    with patch(
        "handlers.event_cancellation.extract_intervals",
        new=AsyncMock(return_value=extracted),
    ) as extract, patch(
        "handlers.pipeline_handlers_intervals.pipeline.anonymizer.anonymize_text",
        return_value="контрольная по русскому завтра в 10",
    ):
        candidates = await find_cancellation_candidates(
            "контрольная по русскому завтра в 10", [first, second]
        )

    extract.assert_awaited_once()
    assert len(candidates) == 1
    assert candidates[0][1].id == 1


@pytest.mark.anyio
async def test_conflicting_draft_is_deleted_instead_of_confirmed():
    telegram_user = TelegramUser(id=999222111, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=telegram_user.id, type="private"),
        from_user=telegram_user,
        text="draft",
    )
    callback = CallbackQuery(
        id="callback",
        from_user=telegram_user,
        chat_instance="chat",
        message=message,
        data="confirm:42",
    )
    start = datetime.now(timezone.utc) + timedelta(days=1)
    event = Event(
        id=42,
        user_id=5,
        title="Новая контрольная",
        deadline=start + timedelta(hours=2),
        status=EventStatus.DRAFT,
    )
    timing = EventTiming(event_id=42, start_at=start, end_at=start + timedelta(hours=2))
    existing = Event(
        id=99,
        user_id=5,
        title="Существующая пара",
        deadline=start + timedelta(hours=1),
        status=EventStatus.CONFIRMED,
    )
    existing_timing = EventTiming(
        event_id=99,
        start_at=start + timedelta(minutes=30),
        end_at=start + timedelta(hours=1),
    )
    session = MagicMock(spec=AsyncSession)
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = event
    timing_result = MagicMock()
    timing_result.scalar_one_or_none.return_value = timing
    conflict_result = MagicMock()
    conflict_result.first.return_value = (existing, existing_timing)
    session.execute = AsyncMock(
        side_effect=[event_result, timing_result, conflict_result]
    )
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with patch.object(
        CallbackQuery, "answer", new_callable=AsyncMock
    ) as answer, patch.object(
        Message, "edit_text", new_callable=AsyncMock
    ) as edit:
        await handle_confirm_callback(callback, session)

    session.delete.assert_awaited_once_with(event)
    session.commit.assert_awaited_once()
    assert event.status == EventStatus.DRAFT
    answer.assert_awaited_once_with("Мероприятия перекрываются", show_alert=True)
    assert "не добавлено" in edit.await_args.args[0]
