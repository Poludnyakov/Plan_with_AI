from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import (
    Chat, MenuButtonWebApp, Message, ReplyKeyboardRemove, User as TelegramUser, WebAppData,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot_main_intervals import configure_telegram_menu
from handlers.miniapp_user import cmd_help, cmd_start, stale_keyboard_calendar
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
        patch("handlers.miniapp_user.discard_pending_imports", new_callable=AsyncMock, return_value=(0, 0)),
        patch("repositories.UserRepository.get_by_tg_id", new_callable=AsyncMock, return_value=None),
        patch("repositories.UserRepository.create", new_callable=AsyncMock),
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
    ):
        await cmd_start(message, db)

    greeting = answer.call_args_list[0]
    assert "Ирина Иванова" in greeting.args[0]
    assert "987654321" not in greeting.args[0]
    # A reply-keyboard Mini App receives no initData, so the old one is removed.
    assert isinstance(greeting.kwargs["reply_markup"], ReplyKeyboardRemove)
    assert "слева от поля ввода" in greeting.args[0]
    # The first start lists the commands and begins the tour right away.
    assert all(command in greeting.args[0] for command in ("/tour", "/today", "/week", "/history", "/help"))
    tour = answer.call_args_list[1]
    assert "Знакомство за минуту" in tour.args[0]
    buttons = [button for row in tour.kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons if button.callback_data] == ["tour:1", "tour:end"]
    assert any(button.web_app for button in buttons)


@pytest.mark.anyio
async def test_telegram_returning_user_gets_buttons_not_the_tour():
    profile = TelegramUser(id=555, is_bot=False, first_name="Ира")
    message = Message(message_id=1, date=1_700_000_000, chat=Chat(id=555, type="private"), from_user=profile, text="/start")
    db = MagicMock(spec=AsyncSession)
    with (
        patch("handlers.miniapp_user.ensure_identity", new_callable=AsyncMock),
        patch("handlers.miniapp_user.discard_pending_imports", new_callable=AsyncMock, return_value=(0, 0)),
        patch("repositories.UserRepository.get_by_tg_id", new_callable=AsyncMock, return_value=object()),
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
    ):
        await cmd_start(message, db)
    assert "С возвращением" in answer.call_args_list[0].args[0]
    assert "/tour" not in answer.call_args_list[0].args[0]
    start_keyboard = answer.call_args_list[1].kwargs["reply_markup"]
    assert start_keyboard.inline_keyboard[1][0].callback_data == "usage_help"
    assert start_keyboard.inline_keyboard[2][0].callback_data == "tour:start"


@pytest.mark.anyio
async def test_telegram_help_sends_factual_usage_guide():
    profile = TelegramUser(id=42, is_bot=False, first_name="Ирина")
    message = Message(
        message_id=2,
        date=1_700_000_000,
        chat=Chat(id=profile.id, type="private"),
        from_user=profile,
        text="/help",
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await cmd_help(message)

    guide = answer.await_args.args[0]
    assert "Как пользоваться" in guide
    assert "/calendar" in guide
    assert "/reminders" in guide
    assert "может ошибаться" in guide


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
    assert first.kwargs["buttons"][1][0]["payload"] == "usage_help"
    assert "/tour" in first.args[0] and "/week" in first.args[0]
    tour = client.send_message.await_args_list[1]
    assert "Знакомство за минуту" in tour.args[0] and tour.kwargs["chat_id"] == 112233
    assert [button.get("payload") for row in tour.kwargs["buttons"] for button in row if button.get("payload")] == ["tour:1", "tour:end"]


@pytest.mark.anyio
async def test_max_help_command_sends_same_platform_guide():
    client = SimpleNamespace(send_message=AsyncMock())
    handler = FeatureMaxUpdateHandler(
        client, SimpleNamespace(), SimpleNamespace(miniapp_name="planiruy")
    )

    await handler.handle_message(
        {"sender": {"user_id": 42}, "body": {"text": "/help"}},
        MagicMock(spec=AsyncSession),
    )

    guide = client.send_message.await_args.args[0]
    assert "внутри MAX" in guide
    assert "/link" in guide
    assert client.send_message.await_args.kwargs["user_id"] == 42


@pytest.mark.anyio
async def test_telegram_menu_button_opens_calendar_mini_app():
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    with patch("handlers.miniapp_user.settings.APP_URL", "https://planwithai.ru"):
        await configure_telegram_menu(bot)
    button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == "Календарь"
    assert button.web_app.url == "https://planwithai.ru/miniapp?destination=calendar"
    commands = [command.command for command in bot.set_my_commands.await_args.args[0]]
    assert "calendar" in commands


@pytest.mark.anyio
async def test_stale_keyboard_calendar_is_replaced_with_signed_buttons():
    profile = TelegramUser(id=42, is_bot=False, first_name="Ирина")
    message = Message(
        message_id=3,
        date=1_700_000_000,
        chat=Chat(id=profile.id, type="private"),
        from_user=profile,
        web_app_data=WebAppData(data="open_calendar", button_text="📅 Открыть календарь"),
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await stale_keyboard_calendar(message)
    assert isinstance(answer.call_args_list[0].kwargs["reply_markup"], ReplyKeyboardRemove)
    inline = answer.call_args_list[1].kwargs["reply_markup"].inline_keyboard[0][0]
    assert inline.web_app.url.endswith("/miniapp?destination=calendar")
