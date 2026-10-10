"""Drive the real Telegram dispatcher with simulated updates.

Outgoing Bot API calls are captured instead of being sent; OpenRouter and the
database (planiruy_e2e) are real. Yandex sync is disabled.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
_env = dict(l.strip().split("=", 1) for l in open(ROOT / ".env") if "=" in l and not l.startswith("#"))
os.environ["DATABASE_URL"] = _env["DATABASE_URL"].rsplit("/", 1)[0] + "/planiruy_e2e"
for key in ("YANDEX_EMAIL", "YANDEX_APP_PASSWORD", "TELEGRAM_BOT_TOKEN"):
    os.environ[key] = ""
os.environ.setdefault("APP_URL", "https://planwithai.ru")
os.environ["OPENROUTER_MODEL_FOR_TEXT"] = os.environ.get("E2E_TEXT_MODEL", "deepseek/deepseek-v4-flash-0731")
if os.environ.get("E2E_OCR_MODEL"):
    os.environ["OPENROUTER_MODEL_FOR_OCR"] = os.environ["E2E_OCR_MODEL"]

import itertools
import logging
from datetime import datetime, timezone

import database
database.engine.echo = False
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile
from aiogram.types import (
    CallbackQuery, Chat, Document, File, Message, Update, User,
)

import bot_main_intervals as entry
from handlers import account, calendar_import, event_cancellation, miniapp_user, onboarding, pipeline_handlers, pipeline_handlers_intervals
from telegram_custom_emoji import CustomEmojiMiddleware

logging.basicConfig(level=logging.WARNING)
BOT_USER = User(id=999, is_bot=True, first_name="planwithai", username="planwithai_bot")
_ids = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self, sim):
        super().__init__()
        self.sim = sim

    async def make_request(self, bot, method, timeout=None):
        self.sim.requests.append(method)
        name = type(method).__name__
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id)
        returning = getattr(method, "__returning__", None)
        if returning is Message or name in {"SendMessage", "EditMessageText", "EditMessageReplyMarkup"}:
            chat_id = getattr(method, "chat_id", None) or self.sim.chat_id
            message_id = getattr(method, "message_id", None)
            if name == "SendMessage":
                message_id = next(_ids)
            msg = Message(
                message_id=message_id, date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="private"), from_user=BOT_USER,
                text=getattr(method, "text", None) or self.sim.texts.get(message_id, ""),
                reply_markup=markup if hasattr(markup := getattr(method, "reply_markup", None), "inline_keyboard") else None,
            )
            self.sim.record(name, msg, method)
            return msg.as_(bot)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        path = url.split("/", 6)[-1]
        data = self.sim.files[path]
        yield data

    async def close(self):
        pass


_DP = None


def dispatcher():
    global _DP
    if _DP is None:
        _DP = Dispatcher()
        _DP.update.outer_middleware(entry.DbSessionMiddleware())
        for r in (miniapp_user.router, account.router, calendar_import.router, onboarding.router, event_cancellation.router,
                  pipeline_handlers_intervals.router, pipeline_handlers.router):
            _DP.include_router(r)
    return _DP


class TgSim:
    def __init__(self, user_id=777000001, name="Тест"):
        self.user = User(id=user_id, is_bot=False, first_name=name, language_code="ru")
        self.chat_id = user_id
        self.requests = []
        self.log = []  # (kind, message_id, text, markup)
        self.texts = {}
        self.markups = {}
        self.files = {}
        self.bot = Bot("123456:" + "A" * 35, session=FakeSession(self))
        self.bot.session.middleware(CustomEmojiMiddleware())
        self.dp = dispatcher()
        self.cursor = 0

    def record(self, name, msg, method):
        text = getattr(method, "text", None)
        if text is not None:
            self.texts[msg.message_id] = text
        markup = getattr(method, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            self.markups[msg.message_id] = markup
        self.log.append((name, msg.message_id, text, markup))

    def _message(self, **kw):
        return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                       chat=Chat(id=self.chat_id, type="private"), from_user=self.user, **kw)

    async def _feed(self, **kw):
        start = len(self.log)
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), **kw))
        return self.log[start:]

    async def text(self, text):
        return await self._feed(message=self._message(text=text))

    async def document(self, path, caption=None):
        data = Path(path).read_bytes()
        file_id = f"doc{next(_ids)}"
        self.files[file_id] = data
        doc = Document(file_id=file_id, file_unique_id=file_id, file_name=Path(path).name, file_size=len(data))
        return await self._feed(message=self._message(document=doc, caption=caption))

    def buttons(self, message_id=None):
        if message_id is None:
            message_id = max(self.markups) if self.markups else None
        markup = self.markups.get(message_id)
        if not markup:
            return []
        return [b for row in markup.inline_keyboard for b in row]

    async def press(self, label, message_id=None):
        """Press the inline button whose text contains `label`."""
        # A leading emoji becomes the button's custom icon, so «🗑 2» is shown as «2».
        bare = label.lstrip("🗑📅✅❌↩️✏️♻️➕⬅➡ \ufe0f")
        for b in self.buttons(message_id):
            if (label.lower() in b.text.lower() or (bare != label and b.text == bare)) and b.callback_data:
                mid = message_id or max(self.markups)
                return await self.click(b.callback_data, mid)
        raise LookupError(f"no button {label!r} in {[b.text for b in self.buttons(message_id)]}")

    async def click(self, data, message_id):
        msg = Message(message_id=message_id, date=datetime.now(timezone.utc),
                      chat=Chat(id=self.chat_id, type="private"), from_user=BOT_USER,
                      text=self.texts.get(message_id, ""))
        cq = CallbackQuery(id=str(next(_ids)), from_user=self.user, chat_instance="x",
                           message=msg, data=data)
        before = len(self.requests)
        out = await self._feed(callback_query=cq)
        answers = [r for r in self.requests[before:] if type(r).__name__ == "AnswerCallbackQuery"]
        return out, [a.text for a in answers]


def show(entries, prefix="  "):
    for name, mid, text, markup in entries:
        if text is None and markup is None:
            continue
        buttons = ""
        if markup is not None and hasattr(markup, "inline_keyboard"):
            buttons = " | кнопки: " + ", ".join(
                f"[{b.text}{'·'+b.icon_custom_emoji_id[-4:] if b.icon_custom_emoji_id else ''}→{b.callback_data or 'webapp'}]"
                for row in markup.inline_keyboard for b in row)
        print(f"{prefix}{name}#{mid}: {(text or '').strip()}{buttons}")


async def user_events(tg_id):
    """Personal events of a Telegram user as (title, status, start, end, all_day) in Moscow time."""
    from zoneinfo import ZoneInfo
    from sqlalchemy import select
    from models import Event, User as DbUser
    from interval_models import EventTiming
    zone = ZoneInfo("Europe/Moscow")
    async with database.async_session_maker() as db:
        rows = (await db.execute(
            select(Event.title, Event.status, EventTiming.start_at, EventTiming.end_at, EventTiming.all_day)
            .join(DbUser, DbUser.id == Event.user_id).join(EventTiming, EventTiming.event_id == Event.id)
            .filter(DbUser.tg_id == tg_id).order_by(EventTiming.start_at)
        )).all()
    return [(t, str(getattr(st, "value", st)), s.astimezone(zone), e.astimezone(zone), a) for t, st, s, e, a in rows]


async def user_series(tg_id):
    from sqlalchemy import select
    from schedule_models import ScheduleSeries
    from account_service import account_id_for
    async with database.async_session_maker() as db:
        account = await account_id_for(db, "telegram", tg_id)
        return (await db.execute(select(ScheduleSeries).filter(ScheduleSeries.account_id == account)
                .order_by(ScheduleSeries.valid_from, ScheduleSeries.start_time))).scalars().all()
