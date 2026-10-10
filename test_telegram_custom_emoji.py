from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from telegram_custom_emoji import (
    CUSTOM_EMOJI,
    CustomEmojiMiddleware,
    custom_emoji_entities,
    decorate,
    parse_legacy_markdown,
)


@pytest.fixture
def bot():
    return Bot("123456:" + "A" * 35)


def utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")


def test_entities_cover_each_brand_emoji_with_utf16_offsets():
    text = "👋 Привет! ℹ️ Справка: 📅 календарь, 🦄 не наш"
    entities = custom_emoji_entities(text)
    assert [utf16_slice(text, e.offset, e.length) for e in entities] == ["👋", "ℹ️", "📅"]
    assert entities[0].custom_emoji_id == CUSTOM_EMOJI["👋"]
    assert entities[1].custom_emoji_id == CUSTOM_EMOJI["ℹ"]


def test_legacy_markdown_becomes_plain_text_and_entities():
    plain, entities = parse_legacy_markdown(
        "⏰ *Напоминание*: _скоро_ `код` [сайт](https://planwithai.ru) a\\_b"
    )
    assert plain == "⏰ Напоминание: скоро код сайт a_b"
    kinds = {(e.type, utf16_slice(plain, e.offset, e.length)) for e in entities}
    assert kinds == {("bold", "Напоминание"), ("italic", "скоро"), ("code", "код"), ("text_link", "сайт")}


@pytest.mark.parametrize("broken", ["*не закрыто", "_нет", "[ссылка без адреса]", "```код"])
def test_markup_telegram_would_reject_is_detected(broken):
    with pytest.raises(ValueError):
        parse_legacy_markdown(broken)


def test_markdown_message_keeps_formatting_and_gains_custom_emoji(bot):
    method = SendMessage(chat_id=1, text="⏰ *Пара* через 30 минут", parse_mode="Markdown")
    decorated = decorate(method, bot)
    assert decorated.parse_mode is None
    assert decorated.text == "⏰ Пара через 30 минут"
    shown = {(e.type, utf16_slice(decorated.text, e.offset, e.length)) for e in decorated.entities}
    assert shown == {("custom_emoji", "⏰"), ("bold", "Пара")}


def test_broken_markdown_is_left_for_telegram_unchanged(bot):
    method = SendMessage(chat_id=1, text="⏰ *не закрыто", parse_mode="Markdown")
    assert decorate(method, bot) is None


def test_html_and_plain_messages(bot):
    html = decorate(SendMessage(chat_id=1, text="<b>📅 План</b>", parse_mode="HTML"), bot)
    assert html.text == f'<b><tg-emoji emoji-id="{CUSTOM_EMOJI["📅"]}">📅</tg-emoji> План</b>'
    plain = decorate(SendMessage(chat_id=1, text="❌ Отменено"), bot)
    assert plain.entities[0].custom_emoji_id == CUSTOM_EMOJI["❌"]
    assert decorate(SendMessage(chat_id=1, text="Без эмодзи"), bot) is None


def test_leading_button_emoji_becomes_the_button_icon(bot):
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Открыть календарь", web_app=WebAppInfo(url="https://x.ru/miniapp")),
        InlineKeyboardButton(text="🦄 Чужой", callback_data="c"),
    ]])
    decorated = decorate(EditMessageReplyMarkup(chat_id=1, message_id=2, reply_markup=markup), bot)
    first, second = decorated.reply_markup.inline_keyboard[0]
    assert (first.text, first.icon_custom_emoji_id) == ("Открыть календарь", CUSTOM_EMOJI["📅"])
    assert first.web_app.url == "https://x.ru/miniapp"
    assert (second.text, second.icon_custom_emoji_id) == ("🦄 Чужой", None)


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=text)


@pytest.mark.anyio
async def test_rejected_custom_emoji_falls_back_to_the_original_request(bot):
    middleware = CustomEmojiMiddleware()
    method = SendMessage(chat_id=1, text="📅 План")
    make_request = AsyncMock(side_effect=[bad_request("Bad Request: custom emoji not allowed"), "sent"])

    assert await middleware(make_request, bot, method) == "sent"
    assert make_request.await_args_list[1].args[1] is method
    assert middleware.enabled is False
    # Later requests go out unchanged without a failing first attempt.
    make_request.reset_mock(side_effect=True)
    make_request.return_value = "sent again"
    await middleware(make_request, bot, method)
    assert make_request.await_count == 1


@pytest.mark.anyio
async def test_custom_emoji_resume_after_pause(bot, monkeypatch):
    # The owner may buy Telegram Premium while the bot runs.
    clock = [1000.0]
    monkeypatch.setattr("telegram_custom_emoji.time.monotonic", lambda: clock[0])
    middleware = CustomEmojiMiddleware(retry_after=600)
    method = SendMessage(chat_id=1, text="📅 План")
    make_request = AsyncMock(side_effect=[bad_request("custom emoji not allowed"), "plain"])
    await middleware(make_request, bot, method)
    assert middleware.enabled is False

    clock[0] += 601
    make_request = AsyncMock(return_value="decorated")
    assert await middleware(make_request, bot, method) == "decorated"
    assert make_request.await_args.args[1].entities[0].custom_emoji_id == CUSTOM_EMOJI["📅"]


@pytest.mark.anyio
async def test_unrelated_errors_do_not_disable_custom_emoji(bot):
    middleware = CustomEmojiMiddleware()
    error = bad_request("Bad Request: chat not found")
    make_request = AsyncMock(side_effect=[error, error])
    with pytest.raises(TelegramBadRequest):
        await middleware(make_request, bot, SendMessage(chat_id=1, text="📅 План"))
    assert middleware.enabled is True


def test_every_emoji_the_bot_writes_has_a_brand_version():
    """New texts must use emoji from t.me/addemoji/planwithai."""
    import pathlib
    import re
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B05-\u2B07\u23E9-\u23FF\u2139\u21A9\u21AA]")
    files = [
        "handlers/pipeline_handlers_intervals.py", "handlers/account.py", "handlers/calendar_import.py", "calendar_import_service.py", "handlers/onboarding.py", "onboarding.py", "handlers/event_cancellation.py",
        "handlers/miniapp_user.py", "handlers/user.py", "history_service.py", "schedule_service.py",
        "calendar_feed_service.py", "services.py", "usage_guide.py", "reminder_summary.py", "scheduler.py",
        "unified_calendar.py",
    ]
    missing = {
        (name, char) for name in files
        for char in emoji.findall(pathlib.Path(name).read_text(encoding="utf-8"))
        if char not in CUSTOM_EMOJI
    }
    assert not missing, sorted(missing)

