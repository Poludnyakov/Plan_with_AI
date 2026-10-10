"""Brand custom emoji (t.me/addemoji/planwithai) in outgoing Telegram messages.

A request middleware swaps the regular emoji the bot already uses for the
matching custom emoji, so handlers keep writing plain text. Telegram shows
custom emoji from a bot only if the bot owner has Telegram Premium (or the bot
has a Fragment username); if Telegram rejects a decorated request, the original
request is sent unchanged and decoration pauses for ten minutes.
"""

import logging
import re
import time

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, MessageEntity, ReplyKeyboardMarkup


logger = logging.getLogger("TelegramCustomEmoji")
RETRY_AFTER_SECONDS = 600

# Base emoji → custom emoji id from the «planwithai» set.
CUSTOM_EMOJI = {
    "📅": "5348398979866471633",
    "🔔": "5348297914991028221",
    "📌": "5348402583344032468",
    "⏰": "5348160540462071767",
    "❌": "5348369512095851242",
    "➕": "5348352611399543459",
    "👋": "5350318581139677562",
    "🌅": "5348566964627352879",
    "ℹ": "5348507182977558385",
    "📊": "5348170436066716645",
    "📖": "5348521618362637839",
    "🔗": "5350281506981979121",
    "✅": "5355159739426710107",
    "⚠": "5353021185015720651",
    "🗑": "5355275892522266118",
    "📆": "5352630669409298665",
    "🎙": "5352961991776450560",
    "📚": "5353051696463390550",
    "📕": "5353051696463390550",
    "📜": "5352612806640314467",
    "⏳": "5355217064355210255",
    # The set has the wall clock under ⏰ (the alarm clock keeps ⏰ above).
    "🕒": "5352726193776930942",
    "🕗": "5352726193776930942",
    "📍": "5352772386150196272",
    "🔎": "5352998224120560652",
    "🖼": "5352608962644588974",
    "✏": "5352810246286911209",
    "↩": "5352582840653490629",
    "♻": "5352765827735140920",
    "💳": "5354906203212260026",
    "⬅": "5352941887034535369",
    "➡": "5352790734250488436",
}
EMOJI_RE = re.compile("(" + "|".join(re.escape(emoji) for emoji in CUSTOM_EMOJI) + ")️?")


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def custom_emoji_entities(text: str) -> list[MessageEntity]:
    return [
        MessageEntity(
            type="custom_emoji",
            offset=_utf16(text[:match.start()]),
            length=_utf16(match.group(0)),
            custom_emoji_id=CUSTOM_EMOJI[match.group(1)],
        )
        for match in EMOJI_RE.finditer(text)
    ]


def parse_legacy_markdown(text: str) -> tuple[str, list[MessageEntity]]:
    """Telegram's legacy «Markdown» → plain text and entities.

    Raises ValueError where Telegram would reject the markup; the caller then
    sends the message unchanged.
    """
    plain: list[str] = []
    entities: list[MessageEntity] = []
    length = 0

    def emit(value: str) -> None:
        nonlocal length
        plain.append(value)
        length += _utf16(value)

    def entity(kind: str, value: str, **extra) -> None:
        start = length
        emit(value)
        if value:
            entities.append(MessageEntity(type=kind, offset=start, length=_utf16(value), **extra))

    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and text[index + 1:index + 2] in {"_", "*", "`", "["}:
            emit(text[index + 1])
            index += 2
        elif text.startswith("```", index):
            end = text.find("```", index + 3)
            if end < 0:
                raise ValueError("unclosed pre")
            body = text[index + 3:end]
            language = None
            first, newline, rest = body.partition("\n")
            if newline and re.fullmatch(r"[\w+-]+", first):
                language, body = first, rest
            entity("pre", body, language=language)
            index = end + 3
        elif char in "*_`":
            end = text.find(char, index + 1)
            if end < 0:
                raise ValueError(f"unclosed {char}")
            kind = {"*": "bold", "_": "italic", "`": "code"}[char]
            entity(kind, text[index + 1:end])
            index = end + 1
        elif char == "[":
            close = text.find("]", index + 1)
            if close < 0 or text[close + 1:close + 2] != "(":
                raise ValueError("bad link")
            end = text.find(")", close + 2)
            if end < 0:
                raise ValueError("unclosed link")
            entity("text_link", text[index + 1:close], url=text[close + 2:end])
            index = end + 1
        else:
            emit(char)
            index += 1
    return "".join(plain), entities


def _html_with_custom_emoji(text: str) -> str:
    return EMOJI_RE.sub(
        lambda match: f'<tg-emoji emoji-id="{CUSTOM_EMOJI[match.group(1)]}">{match.group(0)}</tg-emoji>',
        text,
    )


def _decorated_markup(markup):
    """Leading brand emoji on a button becomes the button's custom icon."""
    if isinstance(markup, InlineKeyboardMarkup):
        rows, field = markup.inline_keyboard, "inline_keyboard"
    elif isinstance(markup, ReplyKeyboardMarkup):
        rows, field = markup.keyboard, "keyboard"
    else:
        return markup
    changed = False
    new_rows = []
    for row in rows:
        new_row = []
        for button in row:
            match = EMOJI_RE.match(getattr(button, "text", "") or "")
            if match and not getattr(button, "icon_custom_emoji_id", None):
                text = button.text[match.end():].lstrip() or button.text
                button = button.model_copy(update={
                    "text": text, "icon_custom_emoji_id": CUSTOM_EMOJI[match.group(1)],
                })
                changed = True
            new_row.append(button)
        new_rows.append(new_row)
    return markup.model_copy(update={field: new_rows}) if changed else markup


def decorate(method, bot: Bot):
    """Return a copy of the method with custom emoji, or None if nothing changes."""
    update: dict = {}
    if isinstance(method, (SendMessage, EditMessageText)):
        text = method.text or ""
        parse_mode = method.parse_mode
        if isinstance(parse_mode, Default):
            parse_mode = bot.default[parse_mode.name]
        if EMOJI_RE.search(text):
            if method.entities:
                pass  # explicit entities: offsets are the caller's responsibility
            elif parse_mode is None:
                update["entities"] = custom_emoji_entities(text)
            elif str(parse_mode).casefold() == "html":
                update["text"] = _html_with_custom_emoji(text)
            elif str(parse_mode) == "Markdown":
                try:
                    plain, entities = parse_legacy_markdown(text)
                except ValueError:
                    plain, entities = "", []
                if plain:
                    update.update(
                        text=plain, parse_mode=None,
                        entities=entities + custom_emoji_entities(plain),
                    )
    if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
        markup = _decorated_markup(method.reply_markup)
        if markup is not method.reply_markup:
            update["reply_markup"] = markup
    return method.model_copy(update=update) if update else None


class CustomEmojiMiddleware(BaseRequestMiddleware):
    """Decorates requests; after a rejection pauses, then tries again.

    A pause rather than a permanent switch-off: the owner may buy Telegram
    Premium while the bot is running, and that must take effect by itself.
    """

    def __init__(self, retry_after: float = RETRY_AFTER_SECONDS) -> None:
        self.retry_after = retry_after
        self.paused_until = 0.0

    @property
    def enabled(self) -> bool:
        return time.monotonic() >= self.paused_until

    async def __call__(self, make_request, bot: Bot, method):
        decorated = decorate(method, bot) if self.enabled else None
        if decorated is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, decorated)
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                raise
            # Not sent: retry exactly what the handler asked for. If that works,
            # the custom emoji were the cause, so pause decorating for a while.
            result = await make_request(bot, method)
            self.paused_until = time.monotonic() + self.retry_after
            logger.warning(
                "Custom emoji rejected by Telegram, plain emoji for %s s: %s",
                int(self.retry_after), error,
            )
            return result
