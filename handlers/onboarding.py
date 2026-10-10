"""Telegram: the one-minute tour and the plan summaries (/today, /week)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.miniapp_user import miniapp_url
from onboarding import tour_page
from reminder_service import preference_text, update_reminder_preference
from reminder_summary import daily_summary_text, weekly_summary_text
from unified_calendar import ensure_platform_user


router = Router()


def draw(rows) -> InlineKeyboardMarkup:
    """Neutral buttons (see onboarding.py) as Telegram buttons."""
    keyboard = []
    for row in rows:
        line = []
        for kind, text, value in row:
            if kind == "copy":
                line.append(InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value)))
            elif kind == "app":
                line.append(InlineKeyboardButton(text=text, web_app=WebAppInfo(url=miniapp_url("calendar"))))
            else:
                line.append(InlineKeyboardButton(text=text, callback_data=value))
        keyboard.append(line)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def summary_keyboard(kind: str) -> InlineKeyboardMarkup:
    # «summary_switch» edits the summary in place; «summary:» (from the tour) sends a new one.
    other = ("callback", "📊 План на неделю", "summary_switch:week") if kind == "today" else ("callback", "🌅 План на сегодня", "summary_switch:today")
    return draw([[other], [("app", "📅 Открыть календарь", "")]])


async def summary_text(db: AsyncSession, user_id: int, kind: str) -> str:
    user = await ensure_platform_user(db, "telegram", user_id)
    build = weekly_summary_text if kind == "week" else daily_summary_text
    return await build(db, "telegram", user_id, user.timezone, style="html")


@router.message(Command("tour", "onboarding", "обучение"))
async def cmd_tour(message: Message):
    text, rows = tour_page("telegram", "tour:0")
    await message.answer(text, reply_markup=draw(rows))


@router.callback_query(F.data.startswith("tour:"))
async def tour_step(callback: CallbackQuery):
    text, rows = tour_page("telegram", callback.data)
    await callback.answer()
    if callback.message and callback.data == "tour:start":
        # From the welcome: keep its buttons and start the tour below.
        await callback.message.answer(text, reply_markup=draw(rows))
    elif callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=draw(rows))
        except TelegramBadRequest:
            # An old message that can no longer be edited: continue in a new one.
            await callback.message.answer(text, reply_markup=draw(rows))


@router.callback_query(F.data == "tour_open:reminders")
async def tour_reminders(callback: CallbackQuery, db_session: AsyncSession):
    from handlers.account import reminder_panel

    preference = await update_reminder_preference(db_session, "telegram", callback.from_user.id, "platform")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            preference_text(preference), parse_mode="Markdown", reply_markup=reminder_panel(preference)
        )


@router.message(Command("today"))
async def cmd_today(message: Message, db_session: AsyncSession):
    text = await summary_text(db_session, message.from_user.id, "today")
    await message.answer(text, parse_mode="HTML", reply_markup=summary_keyboard("today"))


@router.message(Command("week"))
async def cmd_week(message: Message, db_session: AsyncSession):
    text = await summary_text(db_session, message.from_user.id, "week")
    await message.answer(text, parse_mode="HTML", reply_markup=summary_keyboard("week"))


@router.callback_query(F.data.in_({"summary:today", "summary:week", "summary_switch:today", "summary_switch:week"}))
async def switch_summary(callback: CallbackQuery, db_session: AsyncSession):
    action, kind = callback.data.split(":", 1)
    text = await summary_text(db_session, callback.from_user.id, kind)
    await callback.answer()
    if not callback.message:
        return
    if action == "summary_switch":
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=summary_keyboard(kind))
            return
        except TelegramBadRequest:
            pass  # too old to edit: send it below
    await callback.message.answer(text, parse_mode="HTML", reply_markup=summary_keyboard(kind))
