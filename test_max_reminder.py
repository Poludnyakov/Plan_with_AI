from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from max_bot.handler_full import FullMaxUpdateHandler
from max_bot.models import MaxReminder
from max_bot.service import MaxEventService


@pytest.mark.anyio
async def test_max_manual_reminder_conversation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        service = MaxEventService()
        start = datetime.now(timezone.utc) + timedelta(days=10)
        event = (await service.create_drafts(db, 202, [{
            "title": "Экзамен", "description": "", "start_at": start,
            "end_at": start + timedelta(hours=2),
        }]))[0]
        before = len(event.reminders)
        client = MagicMock()
        client.answer_callback = AsyncMock(return_value={})
        client.send_message = AsyncMock(return_value={})
        handler = FullMaxUpdateHandler(
            client, service, SimpleNamespace(miniapp_name="", miniapp_url="")
        )
        await handler.handle_callback({"callback": {
            "callback_id": "cb-reminder", "payload": f"reminder:{event.id}",
            "user": {"user_id": 202},
        }}, db)
        reminder_local = (start - timedelta(hours=1)).astimezone(timezone(timedelta(hours=3)))
        await handler.handle_message({
            "sender": {"user_id": 202},
            "body": {"text": reminder_local.strftime("%d.%m.%Y %H:%M")},
        }, db)
        reminders = (await db.execute(
            select(MaxReminder).filter_by(event_id=event.id)
        )).scalars().all()
        assert len(reminders) == before + 1
        assert client.answer_callback.await_count == 1
        assert any(
            "Напоминание добавлено" in call.args[0]
            for call in client.send_message.await_args_list
        )
    await engine.dispose()
