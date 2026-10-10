from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from handlers.onboarding import draw, summary_keyboard
from max_bot.models import MaxUser
from models import User
from onboarding import DONE_TEXT, EXAMPLE, tour_buttons, tour_page, tour_steps
from reminder_summary import daily_summary_text, weekly_summary_text
from unified_calendar import create_linked_event


ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)  # Monday 10:00 in Moscow


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    with patch("unified_calendar.build_user_reminders", new_callable=AsyncMock, return_value=[]), \
         patch("unified_calendar._sync"):
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([User(tg_id=11, timezone="Europe/Moscow"), MaxUser(max_user_id=33)])
            await session.commit()
            yield session
    await engine.dispose()


def test_tour_walks_forward_and_back_and_ends():
    steps = tour_steps("telegram")
    assert len(steps) == 6 and steps[1].startswith("✏️ Шаг 1 из 5")
    assert "слева от поля ввода" in steps[3] and "под сообщениями" in tour_steps("max")[3]
    text, rows = tour_page("telegram", "tour:0")
    assert [button[2] for row in rows for button in row] == ["tour:1", "tour:end"]
    text, rows = tour_page("telegram", "tour:1")
    flat = [button for row in rows for button in row]
    assert ("copy", "✏️ Скопировать пример", EXAMPLE) in flat
    assert {button[2] for button in flat} >= {"tour:0", "tour:2", "tour:end"}
    _, rows = tour_page("telegram", "tour:5")
    assert [button[2] for button in rows[-1]] == ["tour:4", "tour:end"]
    assert tour_page("max", "tour:end")[0] == DONE_TEXT
    # Broken callback data never fails.
    assert tour_page("telegram", "tour:99")[0] == steps[-1] and tour_page("telegram", "tour:x")[0] == steps[0]


def test_telegram_buttons_copy_the_example_and_open_the_mini_app():
    markup = draw(tour_buttons(1))
    assert markup.inline_keyboard[0][0].copy_text.text == EXAMPLE
    app = draw(tour_buttons(3)).inline_keyboard[0][0]
    assert app.web_app.url.endswith("/miniapp?destination=calendar")
    summary = summary_keyboard("today").inline_keyboard
    assert summary[0][0].callback_data == "summary_switch:week" and summary[1][0].web_app


def test_max_buttons_use_clipboard_and_open_app():
    from max_bot.handler_features import FeatureMaxUpdateHandler
    handler = FeatureMaxUpdateHandler(None, None, type("S", (), {"miniapp_name": "", "bot_username": "bot"})())
    rows = handler.draw(tour_buttons(1))
    assert rows[0][0] == {"type": "clipboard", "text": "✏️ Скопировать пример", "payload": EXAMPLE}
    assert handler.draw(tour_buttons(3))[0][0]["type"] == "open_app"


@pytest.mark.anyio
async def test_week_summary_days_free_days_overlaps_and_escaping(db):
    at = lambda day, hour, minute=0: datetime(2026, 10, day, hour, minute, tzinfo=ZONE)
    await create_linked_event(db, "telegram", 11, "Созвон <команды>", "", at(12, 15), at(12, 16))
    await create_linked_event(db, "telegram", 11, "Отчёт_по *физике*", "", at(13, 9), at(13, 10))
    await create_linked_event(db, "telegram", 11, "Командировка", "", at(15, 0), at(17, 0), all_day=True)
    with patch("unified_calendar.find_linked_conflict", new_callable=AsyncMock, return_value=None):
        # Imported calendars may overlap: the summary has to say so.
        await create_linked_event(db, "telegram", 11, "Вебинар", "", at(13, 9, 30), at(13, 11))
    text = await weekly_summary_text(db, "telegram", 11, "Europe/Moscow", NOW, style="html")
    assert text.startswith("📊 <b>План на 7 дней</b>") and "Событий: 4" in text
    assert "<b>Сегодня, 12.10</b>" in text and "<b>Завтра, 13.10</b>" in text
    assert "15:00–16:00  Созвон &lt;команды&gt;" in text and "Отчёт_по *физике*" in text
    assert "• весь день  Командировка" in text and "<b>Пт, 16.10</b>" in text
    assert "Свободные дни: Ср 14.10, Сб 17.10, Вс 18.10" in text
    assert "⚠️ Вт 13.10: пересекаются «Отчёт_по *физике*» и «Вебинар»" in text
    max_text = await weekly_summary_text(db, "telegram", 11, "Europe/Moscow", NOW)
    assert max_text.startswith("📊 **План на 7 дней**")
    today = await daily_summary_text(db, "telegram", 11, "Europe/Moscow", NOW, style="html")
    assert today.startswith("🌅 <b>План на сегодня</b>") and "Созвон &lt;команды&gt;" in today


@pytest.mark.anyio
async def test_empty_week(db):
    text = await weekly_summary_text(db, "max", 33, "Europe/Moscow", NOW)
    assert "Неделя свободна" in text
