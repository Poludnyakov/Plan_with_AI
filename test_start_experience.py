from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, ReplyKeyboardRemove, User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.miniapp_user import cmd_start
from max_bot.handler_features import FeatureMaxUpdateHandler


@pytest.mark.anyio
async def test_telegram_start_uses_profile_name_and_removes_old_keyboard():
    profile = TelegramUser(
        id=987654321,
        is_bot=False,
        first_name="Ирина",
        last_name="Иванова",
        username="987654321",
    )
    message = Message(
        message_id=1,
        date=1_700_000_000,
        chat=Chat(id=profile.id, type="private"),
        from_user=profile,
        text="/start",
    )
    db = MagicMock(spec=AsyncSession)
    with (
        patch("handlers.miniapp_user.ensure_identity", new_callable=AsyncMock),
        patch("repositories.UserRepository.get_by_tg_id", new_callable=AsyncMock, return_value=None),
        patch("repositories.UserRepository.create", new_callable=AsyncMock),
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
    ):
        await cmd_start(message, db)

    greeting = answer.call_args_list[0]
    assert "Ирина Иванова" in greeting.args[0]
    assert "987654321" not in greeting.args[0]
    assert isinstance(greeting.kwargs["reply_markup"], ReplyKeyboardRemove)


@pytest.mark.anyio
async def test_max_bot_started_replies_to_chat_with_full_profile_name():
    client = SimpleNamespace(send_message=AsyncMock())
    service = SimpleNamespace(user=AsyncMock())
    settings = SimpleNamespace(miniapp_name="planiruy")
    handler = FeatureMaxUpdateHandler(client, service, settings)
    db = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    with patch("max_bot.handler_features.ensure_identity", new_callable=AsyncMock):
        await handler.handle_update({
            "update_type": "bot_started",
            "chat_id": 112233,
            "user": {
                "user_id": 998877,
                "first_name": "Ирина",
                "last_name": "Иванова",
            },
        }, db)

    assert client.send_message.await_count == 2
    first = client.send_message.await_args_list[0]
    assert "✅ Бот запущен" in first.args[0]
    assert "Ирина Иванова" in first.args[0]
    assert "998877" not in first.args[0]
    assert first.kwargs["chat_id"] == 112233
    assert "user_id" not in first.kwargs
