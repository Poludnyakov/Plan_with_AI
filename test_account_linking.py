from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import pytest

from account_service import (
    consume_link_code,
    create_link_code,
    intelligent_reminders_enabled,
    linked_identities,
    set_intelligent_reminders,
)
from database import Base


@pytest.mark.anyio
async def test_telegram_and_max_accounts_can_be_linked_without_moving_events():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        code = await create_link_code(db, "telegram", 111)
        result = await consume_link_code(db, "max", 222, code)
        assert result == {"telegram": 111, "max": 222}
        assert await linked_identities(db, "telegram", 111) == result
        assert await linked_identities(db, "max", 222) == result
    await engine.dispose()


@pytest.mark.anyio
async def test_intelligent_reminder_preference_is_shared_after_linking():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        code = await create_link_code(db, "telegram", 333)
        await consume_link_code(db, "max", 444, code)
        assert await intelligent_reminders_enabled(db, "max", 444) is True
        await set_intelligent_reminders(db, "telegram", 333, False)
        assert await intelligent_reminders_enabled(db, "max", 444) is False
    await engine.dispose()
