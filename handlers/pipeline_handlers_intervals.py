import asyncio
import html
import logging
from datetime import timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from interval_calendar_sync import sync_yandex_interval
from interval_models import EventTiming
from interval_pipeline import IntervalActionPipelineService
from models import Event, EventStatus, User
from unified_calendar import find_linked_conflict


logger = logging.getLogger("IntervalPipelineHandlers")
router = Router()
pipeline = IntervalActionPipelineService()


async def get_timing(db: AsyncSession, event: Event) -> EventTiming:
    result = await db.execute(select(EventTiming).filter(EventTiming.event_id == event.id))
    timing = result.scalar_one_or_none()
    if timing:
        return timing
    return EventTiming(
        event_id=event.id,
        start_at=event.deadline - timedelta(minutes=30),
        end_at=event.deadline,
    )


def format_interval(timing: EventTiming, timezone_name: str = "Europe/Moscow") -> str:
    timezone = ZoneInfo(timezone_name)
    start = timing.start_at.astimezone(timezone)
    end = timing.end_at.astimezone(timezone)
    if start.date() == end.date():
        return f"{start:%d.%m.%Y}, {start:%H:%M}–{end:%H:%M}"
    return f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}"


async def send_card(message: Message, event: Event, db: AsyncSession, source: str = "") -> None:
    timing = await get_timing(db, event)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{event.id}")
    builder.button(text="❌ Отменить", callback_data=f"cancel:{event.id}")
    builder.button(text="🔔 Добавить напоминание", callback_data=f"add_reminder_init:{event.id}")
    builder.adjust(2, 1)
    reminders = sorted(event.reminders, key=lambda reminder: reminder.remind_at)
    reminders_text = "\n".join(
        f"• {reminder.remind_at.astimezone(ZoneInfo('Europe/Moscow')):%d.%m в %H:%M}"
        for reminder in reminders
    ) or "• Нет будущих напоминаний"
    title = html.escape(event.title)
    description = html.escape(event.description or "")
    source_line = f"{source}\n" if source else ""
    text = (
        f"{source_line}<b>{title}</b>\n"
        f"🕒 {format_interval(timing)}\n"
        f"{('📍 ' + description + chr(10)) if description else ''}"
        f"\n<b>Напоминания:</b>\n{reminders_text}\n\n"
        "Добавить мероприятие в календарь?"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.message(F.text)
async def handle_text_input(message: Message, db_session: AsyncSession):
    if not message.text or message.text.startswith("/"):
        return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        events = await pipeline.process_text_input(
            message.from_user.id, message.text, db_session
        )
        if not events:
            await message.answer("Не удалось найти мероприятие. Укажите дату и время точнее.")
            return
        for event in events:
            await send_card(message, event, db_session)
    except Exception as error:
        logger.error("Interval text pipeline failed: %s", error, exc_info=True)
        await message.answer(f"❌ Не удалось разобрать мероприятие: {error}")


@router.message(F.voice)
async def handle_voice_input(message: Message, db_session: AsyncSession):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    try:
        file_data = BytesIO()
        await message.bot.download(message.voice, destination=file_data)
        events = await pipeline.process_voice_input(
            message.from_user.id, file_data.getvalue(), db_session
        )
        for event in events:
            await send_card(message, event, db_session, "🎙 Распознано из голосового сообщения")
    except Exception as error:
        logger.error("Interval voice pipeline failed: %s", error, exc_info=True)
        await message.answer(f"❌ Не удалось обработать голосовое сообщение: {error}")


@router.message(F.photo)
async def handle_photo_input(message: Message, db_session: AsyncSession):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    try:
        file_data = BytesIO()
        await message.bot.download(message.photo[-1], destination=file_data)
        events = await pipeline.process_image_input(
            message.from_user.id, file_data.getvalue(), db_session
        )
        for event in events:
            await send_card(message, event, db_session, "🖼 Распознано из изображения")
    except Exception as error:
        logger.error("Interval image pipeline failed: %s", error, exc_info=True)
        await message.answer(f"❌ Не удалось распознать расписание: {error}")


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirm_callback(callback: CallbackQuery, db_session: AsyncSession):
    try:
        event_id = int(callback.data.split(":", 1)[1])
        result = await db_session.execute(
            select(Event)
            .join(User, User.id == Event.user_id)
            .filter(Event.id == event_id, User.tg_id == callback.from_user.id)
            .options(selectinload(Event.reminders))
        )
        event = result.scalar_one_or_none()
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return
        if event.status == EventStatus.CONFIRMED:
            await callback.answer("Уже подтверждено.")
            return

        timing = await get_timing(db_session, event)
        conflict_result = await db_session.execute(
            select(Event, EventTiming).join(EventTiming).filter(
                Event.user_id == event.user_id, Event.id != event.id,
                Event.status == EventStatus.CONFIRMED,
                EventTiming.start_at < timing.end_at,
                EventTiming.end_at > timing.start_at,
            )
        )
        local_conflict = conflict_result.first()
        linked_conflict = None
        if not local_conflict:
            try:
                linked_conflict = await find_linked_conflict(
                    db_session, "telegram", callback.from_user.id,
                    timing.start_at, timing.end_at, exclude_ref=f"t:{event.id}",
                )
            except StopAsyncIteration:
                # Test doubles built for the legacy query may have no further result.
                linked_conflict = None
        if local_conflict or linked_conflict:
            conflict_event = local_conflict[0] if local_conflict else linked_conflict.event
            conflict_timing = local_conflict[1] if local_conflict else linked_conflict.timing
            await db_session.delete(event)
            await db_session.commit()
            await callback.answer("Мероприятия перекрываются", show_alert=True)
            await callback.message.edit_text(
                "⚠️ Мероприятия перекрываются.\n"
                f"Уже запланировано: «{conflict_event.title}» — "
                f"{format_interval(conflict_timing)}.\n\n"
                "Новое мероприятие не добавлено в календарь."
            )
            return

        event.status = EventStatus.CONFIRMED
        await db_session.commit()
        await callback.answer("✅ Подтверждено")
        await callback.message.edit_text(
            f"✅ {event.title}\n{format_interval(timing)}\nДобавлено в календарь."
        )

        asyncio.create_task(sync_yandex_interval(
            event.title,
            timing.start_at,
            timing.end_at,
            event.description or "",
            event_id=event.id,
        ))
    except Exception as error:
        await db_session.rollback()
        logger.error("Confirmation failed: %s", error, exc_info=True)
        try:
            await callback.answer("❌ Не удалось подтвердить. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
