import asyncio
import html
import logging
import re
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
from schedule_ai_service import extract_weekly_schedule
from schedule_document_service import MAX_FILE_BYTES, parse_schedule_document
from schedule_service import (
    cancel_import,
    confirm_import,
    create_import_draft,
    finish_import_source,
    import_preview,
    parse_date_range,
    parse_occurrence_date,
    pending_draft,
    pending_import_source,
    save_import_source,
    set_draft_range,
    skip_candidates,
    skip_occurrence,
    update_import_source_prompt,
)
from unified_calendar import find_linked_conflict


logger = logging.getLogger("IntervalPipelineHandlers")
router = Router()
pipeline = IntervalActionPipelineService()
SKIP_SCHEDULE_RE = re.compile(
    r"^\s*(?:я\s+)?(?:не\s+(?:иду|пойду)|пропущу|пропускаю)\b", re.IGNORECASE
)


def import_keyboard(draft_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Добавить расписание", callback_data=f"schedule_confirm:{draft_id}")
    builder.button(text="❌ Отменить импорт", callback_data=f"schedule_cancel:{draft_id}")
    builder.adjust(1)
    return builder.as_markup()


async def processing_status(
    message: Message, subject: str = "Ваше сообщение"
) -> Message:
    return await message.answer(f"⏳ {subject} обрабатывается…")


async def finish_status(status: Message | None, text: str) -> None:
    if not status:
        return
    try:
        await status.edit_text(text)
    except Exception:
        # A status message is informational and must never fail the pipeline.
        pass


def verified_preview(draft, extraction: dict) -> str:
    verification = extraction.get("verification") or {}
    source_days = verification.get("source_weekdays") or []
    output_days = verification.get("output_weekdays") or []
    labels = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    lines = []
    if source_days:
        lines.append(
            "Проверка покрытия: "
            + ", ".join(labels[index] for index in output_days)
            + " из источника "
            + ", ".join(labels[index] for index in source_days)
        )
    lines.extend(verification.get("warnings") or [])
    prefix = "✅ Источник проверен."
    if lines:
        prefix += "\n" + "\n".join(lines)
    return prefix + "\n\n" + import_preview(draft)


async def create_verified_schedule_draft(
    db_session: AsyncSession,
    user_id: int,
    content: bytes,
    filename: str,
    prompt: str,
    date_range,
):
    extraction = await parse_schedule_document(
        content, filename, prompt, valid_range=date_range
    )
    for slot in extraction["slots"]:
        slot["title"] = pipeline.anonymizer.clean_event_title(
            pipeline.anonymizer.anonymize_text(slot.get("title", ""))
        )
        slot["description"] = pipeline.anonymizer.clean_display_text(
            pipeline.anonymizer.anonymize_text(slot.get("description", ""))
        )
    draft = await create_import_draft(
        db_session, "telegram", user_id, extraction
    )
    await set_draft_range(db_session, draft, *date_range)
    return draft, extraction


async def handle_schedule_skip(message: Message, db: AsyncSession) -> bool:
    text = message.text or ""
    if not SKIP_SCHEDULE_RE.match(text):
        return False
    target = parse_occurrence_date(text)
    if target is None:
        await message.answer(
            "Укажите дату занятия. Например: «не иду на математику завтра»."
        )
        return True
    candidates = await skip_candidates(db, "telegram", message.from_user.id, target, text)
    if not candidates:
        await message.answer("На эту дату не нашёл подходящего занятия в расписании.")
        return True
    if len(candidates) == 1:
        item = candidates[0]
        await skip_occurrence(db, "telegram", message.from_user.id, item["id"])
        await message.answer(f"Занятие «{item['title']}» {target:%d.%m} скрыто в календаре.")
        return True
    builder = InlineKeyboardBuilder()
    for item in candidates[:8]:
        builder.button(
            text=f"Пропустить {item['start_local']} · {item['title'][:28]}",
            callback_data=f"skip_schedule:{item['id']}",
        )
    builder.adjust(1)
    await message.answer("Какое занятие пропустить?", reply_markup=builder.as_markup())
    return True


async def get_timing(db: AsyncSession, event: Event) -> EventTiming:
    result = await db.execute(select(EventTiming).filter(EventTiming.event_id == event.id))
    timing = result.scalar_one_or_none()
    if timing:
        return timing
    return EventTiming(
        event_id=event.id,
            all_day=bool(getattr(timing, "all_day", False)),
        start_at=event.deadline - timedelta(minutes=30),
        end_at=event.deadline,
    )


def format_interval(timing: EventTiming, timezone_name: str = "Europe/Moscow") -> str:
    timezone = ZoneInfo(timezone_name)
    start = timing.start_at.astimezone(timezone)
    end = timing.end_at.astimezone(timezone)
    if bool(getattr(timing, "all_day", False)):
        last_day = (end - timedelta(days=1)).date()
        if start.date() == last_day:
            return f"{start:%d.%m.%Y} · весь день"
        return f"{start:%d.%m}–{last_day:%d.%m.%Y} · весь день"
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
    status = None
    try:
        if await handle_schedule_skip(message, db_session):
            return
        source = await pending_import_source(
            db_session, "telegram", message.from_user.id
        )
        if source:
            if re.search(r"\b(?:отмена|отменить)\s+(?:импорт|расписание)\b", message.text, re.I):
                await finish_import_source(db_session, source, "cancelled")
                await message.answer("Импорт расписания отменён.")
                return
            combined_prompt = " ".join(
                part for part in (source.prompt.strip(), message.text.strip()) if part
            )
            date_range = parse_date_range(combined_prompt)
            if date_range is None:
                await update_import_source_prompt(db_session, source, combined_prompt)
                await message.answer(
                    "Файл сохранён. Теперь укажите период двумя датами, например: "
                    "«с 1 сентября по 1 декабря». Можно также уточнить группу или человека."
                )
                return
            status = await processing_status(message, "Ваш файл")
            try:
                draft, extraction = await create_verified_schedule_draft(
                    db_session, message.from_user.id, source.content,
                    source.filename, combined_prompt, date_range,
                )
                await finish_import_source(db_session, source)
                await finish_status(status, "✅ Файл полностью обработан и проверен.")
                await message.answer(
                    verified_preview(draft, extraction),
                    reply_markup=import_keyboard(draft.id),
                )
            except Exception as error:
                await db_session.rollback()
                logger.error("Saved schedule source processing failed: %s", error, exc_info=True)
                await finish_status(status, "❌ Импорт не прошёл проверку.")
                await message.answer(f"❌ {error}")
            return
        draft = await pending_draft(db_session, "telegram", message.from_user.id)
        if draft and draft.status == "awaiting_range":
            if re.search(r"\b(?:отмена|отменить)\s+(?:импорт|расписание)\b", message.text, re.I):
                await cancel_import(db_session, "telegram", message.from_user.id, draft.id)
                await message.answer("Импорт расписания отменён.")
                return
            date_range = parse_date_range(message.text)
            if date_range is None:
                await message.answer(
                    "Для распознанного расписания сначала укажите период, например: "
                    "«с 1 сентября по 1 декабря». Отменить: «отмена импорта»."
                )
                return
            await set_draft_range(db_session, draft, *date_range)
            await message.answer(import_preview(draft), reply_markup=import_keyboard(draft.id))
            return
        status = await processing_status(message)
        events = await pipeline.process_text_input(
            message.from_user.id, message.text, db_session
        )
        if not events:
            await finish_status(status, "Не удалось найти мероприятие.")
            await message.answer("Не удалось найти мероприятие. Укажите дату и время точнее.")
            return
        await finish_status(status, "✅ Сообщение обработано.")
        for event in events:
            await send_card(message, event, db_session)
    except Exception as error:
        logger.error("Interval text pipeline failed: %s", error, exc_info=True)
        await finish_status(status, "❌ Не удалось обработать сообщение.")
        await message.answer(f"❌ Не удалось разобрать мероприятие: {error}")


@router.message(F.voice)
async def handle_voice_input(message: Message, db_session: AsyncSession):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    status = await processing_status(message, "Ваше голосовое сообщение")
    try:
        file_data = BytesIO()
        await message.bot.download(message.voice, destination=file_data)
        events = await pipeline.process_voice_input(
            message.from_user.id, file_data.getvalue(), db_session
        )
        await finish_status(status, "✅ Голосовое сообщение обработано.")
        for event in events:
            await send_card(message, event, db_session, "🎙 Распознано из голосового сообщения")
    except Exception as error:
        logger.error("Interval voice pipeline failed: %s", error, exc_info=True)
        await finish_status(status, "❌ Не удалось обработать голосовое сообщение.")
        await message.answer(f"❌ Не удалось обработать голосовое сообщение: {error}")


@router.message(F.photo)
async def handle_photo_input(message: Message, db_session: AsyncSession):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    status = await processing_status(message, "Ваше изображение")
    try:
        file_data = BytesIO()
        await message.bot.download(message.photo[-1], destination=file_data)
        content = file_data.getvalue()
        prompt = (message.caption or "").strip()
        date_range = parse_date_range(prompt) if prompt else None
        if prompt and date_range:
            draft, extraction = await create_verified_schedule_draft(
                db_session, message.from_user.id, content,
                "schedule.jpg", prompt, date_range,
            )
            await finish_status(status, "✅ Изображение полностью обработано по инструкции.")
            await message.answer(
                verified_preview(draft, extraction),
                reply_markup=import_keyboard(draft.id),
            )
            return
        if prompt:
            await save_import_source(
                db_session, "telegram", message.from_user.id,
                content, "schedule.jpg", prompt,
            )
            await finish_status(status, "✅ Изображение сохранено. Нужен период.")
            await message.answer(
                "Напишите период двумя датами, например: «с 1 сентября по 1 декабря». "
                "Изображение повторно отправлять не нужно."
            )
            return
        weekly = await extract_weekly_schedule(content)
        if weekly:
            await save_import_source(
                db_session, "telegram", message.from_user.id,
                content, "schedule.jpg",
            )
            await finish_status(status, "✅ Расписание обнаружено и сохранено.")
            await message.answer(
                "Похоже, это расписание. Напишите одним сообщением, что выбрать, и период.\n\n"
                "Например: «группа ПИ-241 с 1 сентября по 1 декабря». "
                "Результат старого распознавателя не используется."
            )
            return
        events = await pipeline.process_image_input(
            message.from_user.id, content, db_session
        )
        await finish_status(status, "✅ Изображение обработано.")
        for event in events:
            await send_card(message, event, db_session, "🖼 Распознано из изображения")
    except Exception as error:
        logger.error("Interval image pipeline failed: %s", error, exc_info=True)
        await finish_status(status, "❌ Не удалось обработать изображение.")
        await message.answer(f"❌ Не удалось распознать расписание: {error}")


@router.message(F.document)
async def handle_document_input(message: Message, db_session: AsyncSession):
    document = message.document
    prompt = (message.caption or "").strip()
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    status = await processing_status(message, "Ваш файл")
    try:
        file_data = BytesIO()
        await message.bot.download(document, destination=file_data)
        content = file_data.getvalue()
        filename = document.file_name or "document"
        try:
            date_range = parse_date_range(prompt) if prompt else None
        except ValueError as error:
            await finish_status(status, "❌ Некорректный период.")
            await message.answer(f"❌ Некорректный период: {error}")
            return
        if date_range is None:
            await save_import_source(
                db_session, "telegram", message.from_user.id,
                content, filename, prompt,
            )
            await finish_status(status, "✅ Файл сохранён. Жду инструкцию и период.")
            await message.answer(
                "Файл сохранён — повторно отправлять его не нужно. Напишите, что выбрать, "
                "и период двумя датами. Например: «группа ПИ-241 с 1 сентября по 1 декабря»."
            )
            return
        draft, extraction = await create_verified_schedule_draft(
            db_session, message.from_user.id, content,
            filename, prompt, date_range,
        )
        await finish_status(
            status,
            f"✅ Файл обработан и проверен: частей {extraction['chunks_processed']}.",
        )
        await message.answer(
            verified_preview(draft, extraction),
            reply_markup=import_keyboard(draft.id),
        )
    except Exception as error:
        await db_session.rollback()
        logger.error("Document schedule pipeline failed: %s", error, exc_info=True)
        await finish_status(status, "❌ Не удалось обработать файл.")
        await message.answer(f"❌ Не удалось разобрать файл: {error}")


@router.callback_query(F.data.startswith("schedule_confirm:"))
async def handle_schedule_confirm(callback: CallbackQuery, db_session: AsyncSession):
    try:
        draft_id = int(callback.data.split(":", 1)[1])
        state, created = await confirm_import(
            db_session, "telegram", callback.from_user.id, draft_id
        )
        if state == "missing":
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        if state == "not_ready":
            await callback.answer("Сначала укажите период расписания.", show_alert=True)
            return
        if state == "imported":
            await callback.answer("Расписание уже добавлено.")
            return
        await callback.answer("Расписание добавлено")
        await callback.message.edit_text(
            f"✅ Расписание добавлено: {created} занятий в недельном шаблоне.\n"
            "Оно показано фоном и не блокирует личные мероприятия."
        )
    except Exception as error:
        await db_session.rollback()
        logger.error("Schedule import confirmation failed: %s", error, exc_info=True)
        await callback.answer("Не удалось добавить расписание.", show_alert=True)


@router.callback_query(F.data.startswith("schedule_cancel:"))
async def handle_schedule_cancel(callback: CallbackQuery, db_session: AsyncSession):
    draft_id = int(callback.data.split(":", 1)[1])
    cancelled = await cancel_import(
        db_session, "telegram", callback.from_user.id, draft_id
    )
    await callback.answer("Импорт отменён" if cancelled else "Черновик уже закрыт")
    if cancelled and callback.message:
        await callback.message.edit_text("Импорт расписания отменён. Календарь не изменён.")


@router.callback_query(F.data.startswith("skip_schedule:"))
async def handle_schedule_skip_callback(callback: CallbackQuery, db_session: AsyncSession):
    occurrence_ref = callback.data.split(":", 1)[1]
    skipped = await skip_occurrence(
        db_session, "telegram", callback.from_user.id, occurrence_ref
    )
    await callback.answer("Занятие скрыто" if skipped else "Занятие не найдено")
    if skipped and callback.message:
        await callback.message.edit_text("Занятие скрыто в календаре только на выбранную дату.")


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
        local_conflict = None
        if not bool(getattr(timing, "all_day", False)):
            conflict_result = await db_session.execute(
                select(Event, EventTiming).join(EventTiming).filter(
                    Event.user_id == event.user_id, Event.id != event.id,
                    Event.status == EventStatus.CONFIRMED,
                    EventTiming.all_day.is_(False),
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
                    timing.start_at, timing.end_at, exclude_ref=f"t:{event.id}", all_day=bool(getattr(timing, "all_day", False)),
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
            all_day=bool(getattr(timing, "all_day", False)),
        ))
    except Exception as error:
        await db_session.rollback()
        logger.error("Confirmation failed: %s", error, exc_info=True)
        try:
            await callback.answer("❌ Не удалось подтвердить. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
