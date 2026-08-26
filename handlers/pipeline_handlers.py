import logging
from io import BytesIO
from typing import Union
from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from services import ActionPipelineService
from models import Event, Reminder, EventStatus, ReminderStatus
from reminder_service import acknowledge_delivery
from repositories import EventRepository

logger = logging.getLogger("PipelineHandlers")
router = Router()
pipeline_service = ActionPipelineService()

@router.message(F.text)
async def handle_text_input(message: Message, db_session: AsyncSession):
    """
    Handles plain text user messages, extracts events using the pipeline,
    and returns a confirmation card.
    """
    # Exclude commands from pipeline
    if message.text.startswith("/"):
        return
        
    # Show typing indicator
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        tg_id = message.from_user.id
        events = await pipeline_service.process_text_input(
            user_tg_id=tg_id,
            raw_text=message.text,
            db_session=db_session
        )
        
        if not events:
            await message.answer("🤔 Мне не удалось распознать никаких событий в этом сообщении. Пожалуйста, уточни детали!")
            return
            
        for event in events:
            # Create inline buttons
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить", callback_data=f"confirm:{event.id}")
            builder.button(text="❌ Отменить", callback_data=f"cancel:{event.id}")
            builder.button(text="🔔 Добавить напоминание", callback_data=f"add_reminder_init:{event.id}")
            builder.adjust(2, 1)
            
            # Format reminders list
            reminders_list = sorted(event.reminders, key=lambda r: r.remind_at)
            reminders_str = "\n".join([f"  - {rem.remind_at.strftime('%Y-%m-%d в %H:%M')}" for rem in reminders_list])
            if not reminders_str:
                reminders_str = "  - Нет предложенных напоминаний"
                
            card_text = (
                f"📝 *Найдено новое событие!*\n\n"
                f"📌 *Задача*: {event.title}\n"
                f"⏰ *Дедлайн*: {event.deadline.strftime('%Y-%m-%d в %H:%M')}\n"
                f"🔔 *Напоминания*:\n{reminders_str}\n\n"
                f"Всё верно? Подтвердите добавление в план."
            )
            await message.answer(card_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Error in text handler: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке сообщения. Попробуй еще раз!")


@router.message(F.voice)
async def handle_voice_input(message: Message, db_session: AsyncSession):
    """
    Handles voice messages, downloads them, transcribes via SpeechKit,
    and runs the pipeline.
    """
    # Show recording voice indicator
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    
    transcribed_text = None
    try:
        # Download voice file to bytes in memory
        file_in_memory = BytesIO()
        await message.bot.download(message.voice, destination=file_in_memory)
        audio_bytes = file_in_memory.getvalue()
        
        # 1. Transcribe Voice Step
        transcribed_text = await pipeline_service.speechkit_service.transcribe_voice(audio_bytes)
        if not transcribed_text or not transcribed_text.strip():
            raise ValueError("SpeechKit transcribed empty text from voice message.")
        
        # 2. Pipe to text processing
        tg_id = message.from_user.id
        events = await pipeline_service.process_text_input(
            user_tg_id=tg_id,
            raw_text=transcribed_text,
            db_session=db_session
        )
        
        if not events:
            await message.answer("🤔 Мне не удалось распознать никаких событий в голосовом сообщении. Пожалуйста, уточни детали!")
            return
            
        for event in events:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить", callback_data=f"confirm:{event.id}")
            builder.button(text="❌ Отменить", callback_data=f"cancel:{event.id}")
            builder.button(text="🔔 Добавить напоминание", callback_data=f"add_reminder_init:{event.id}")
            builder.adjust(2, 1)
            
            reminders_list = sorted(event.reminders, key=lambda r: r.remind_at)
            reminders_str = "\n".join([f"  - {rem.remind_at.strftime('%Y-%m-%d в %H:%M')}" for rem in reminders_list])
            if not reminders_str:
                reminders_str = "  - Нет предложенных напоминаний"
                
            card_text = (
                f"📝 *Найдено новое событие!*\n\n"
                f"📌 *Задача*: {event.title}\n"
                f"⏰ *Дедлайн*: {event.deadline.strftime('%Y-%m-%d в %H:%M')}\n"
                f"🔔 *Напоминания*:\n{reminders_str}\n\n"
                f"Всё верно? Подтвердите добавление в план."
            )
            await message.answer(card_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Error in voice handler: {e}", exc_info=True)
        if transcribed_text and transcribed_text.strip():
            error_msg = (
                f"❌ Произошла ошибка при обработке этого запроса.\n"
                f"Текст вашего сообщения: '{transcribed_text}'\n"
                f"Попробуйте перефразировать или добавить задачу текстом."
            )
        else:
            error_msg = "❌ Произошла ошибка при обработке голосового сообщения. Попробуй еще раз!"
        await message.answer(error_msg)


@router.message(F.photo)
async def handle_photo_input(message: Message, db_session: AsyncSession):
    """
    Handles image messages containing schedules, downloads them,
    processes them directly via Yandex AI Studio multimodal vision completions,
    and registers draft events in the database.
    """
    # Show upload photo action
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    
    try:
        # aiogram photos contain a list of PhotoSize objects. We use the largest one for maximum quality.
        photo = message.photo[-1]
        file_in_memory = BytesIO()
        await message.bot.download(photo, destination=file_in_memory)
        image_bytes = file_in_memory.getvalue()
        
        # Process image directly through multimodal Yandex AI Studio completions
        tg_id = message.from_user.id
        events = await pipeline_service.process_image_input(
            user_tg_id=tg_id,
            image_bytes=image_bytes,
            db_session=db_session
        )
        
        if not events:
            await message.answer("🤔 Мне не удалось обнаружить конкретных дат или дедлайнов на этом изображении. Пожалуйста, отправьте их текстом или голосовым!")
            return
            
        for event in events:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить", callback_data=f"confirm:{event.id}")
            builder.button(text="❌ Отменить", callback_data=f"cancel:{event.id}")
            builder.button(text="🔔 Добавить напоминание", callback_data=f"add_reminder_init:{event.id}")
            builder.adjust(2, 1)
            
            reminders_list = sorted(event.reminders, key=lambda r: r.remind_at)
            reminders_str = "\n".join([f"  - {rem.remind_at.strftime('%Y-%m-%d в %H:%M')}" for rem in reminders_list])
            if not reminders_str:
                reminders_str = "  - Нет предложенных напоминаний"
                
            card_text = (
                f"📝 *Найдено новое событие из расписания!*\n\n"
                f"📌 *Задача*: {event.title}\n"
                f"⏰ *Дедлайн*: {event.deadline.strftime('%Y-%m-%d в %H:%M')}\n"
                f"🔔 *Напоминания*:\n{reminders_str}\n\n"
                f"Всё верно? Подтвердите добавление в план."
            )
            await message.answer(card_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in photo handler: {e}", exc_info=True)
        error_msg = (
            f"❌ Мне не удалось распознать расписание на этом изображении.\n"
            f"Пожалуйста, убедитесь, что текст на картинке четкий, печатный и хорошо освещен, или отправьте его текстом."
        )
        await message.answer(error_msg, parse_mode="Markdown")


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirm_callback(callback: CallbackQuery, db_session: AsyncSession):
    """
    Confirms a draft event and sets its status to confirmed in the DB.
    """
    event_id = int(callback.data.split(":")[1])
    
    try:
        # Load event with reminders
        result = await db_session.execute(
            select(Event)
            .filter(Event.id == event_id)
            .options(selectinload(Event.reminders))
        )
        event = result.scalar_one_or_none()
        
        if not event:
            await callback.answer("⚠️ Событие не найдено.")
            await callback.message.edit_text("⚠️ *Событие не найдено или уже удалено.*", parse_mode="Markdown")
            return
            
        # Check for scheduling conflicts
        event_repo = EventRepository(db_session)
        conflicting_event = await event_repo.get_conflicting_event(
            user_id=event.user_id,
            deadline=event.deadline,
            exclude_event_id=event.id
        )
        if conflicting_event:
            display_time = conflicting_event.deadline.strftime('%d.%m.%Y в %H:%M')
            try:
                import pytz
                from models import User
                user_result = await db_session.execute(
                    select(User).filter(User.id == event.user_id)
                )
                user = user_result.scalar_one_or_none()
                if user and user.timezone:
                    tz = pytz.timezone(user.timezone)
                    display_deadline = conflicting_event.deadline
                    if display_deadline.tzinfo is not None:
                        display_deadline = display_deadline.astimezone(tz)
                    else:
                        display_deadline = pytz.utc.localize(display_deadline).astimezone(tz)
                    display_time = display_deadline.strftime('%d.%m.%Y в %H:%M')
            except Exception:
                pass
                
            await callback.answer("⚠️ Конфликт расписания!", show_alert=True)
            card_text = (
                f"⚠️ *Конфликт расписания!*\n\n"
                f"Вы пытаетесь запланировать задачу на уже забронированное время.\n"
                f"Существующая задача: *'{conflicting_event.title}'* ({display_time}).\n\n"
                f"Пожалуйста, выберите другое время!"
            )
            await callback.message.edit_text(card_text, parse_mode="Markdown")
            return
            
        event.status = EventStatus.CONFIRMED
        await db_session.commit()
        
        # Trigger Yandex Calendar Sync
        from yandex_calendar_service import YandexCalendarService
        await YandexCalendarService().add_deadline_to_yandex(
            title=event.title,
            deadline=event.deadline,
            description=event.description
        )
        
        await callback.answer("✅ Подтверждено!")
        await callback.message.edit_text(
            f"🚀 *Отлично! Задача '{event.title}' добавлена в календарь, я напомню о ней вовремя.*",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error in confirm callback: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при подтверждении.")


@router.callback_query(F.data.startswith("cancel:"))
async def handle_cancel_callback(callback: CallbackQuery, db_session: AsyncSession):
    """
    Cancels a draft event and deletes it (and its cascaded reminders) from the DB.
    """
    event_id = int(callback.data.split(":")[1])
    
    try:
        result = await db_session.execute(select(Event).filter(Event.id == event_id))
        event = result.scalar_one_or_none()
        
        if not event:
            await callback.answer("⚠️ Событие уже отменено.")
            await callback.message.edit_text("❌ *Добавление отменено.*", parse_mode="Markdown")
            return
            
        # Delete event
        await db_session.delete(event)
        await db_session.commit()
        
        await callback.answer("❌ Отменено")
        await callback.message.edit_text("❌ *Добавление отменено.*", parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error in cancel callback: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при отмене.")


@router.callback_query(F.data.startswith("complete_event:"))
async def handle_complete_event_callback(callback: CallbackQuery, db_session: AsyncSession):
    """
    Marks the event as completed (is_completed = True) to disable subsequent reminders.
    """
    event_id = int(callback.data.split(":")[1])
    
    try:
        result = await db_session.execute(select(Event).filter(Event.id == event_id))
        event = result.scalar_one_or_none()
        
        if not event:
            await callback.answer("⚠️ Событие не найдено.")
            await callback.message.edit_text("⚠️ *Событие не найдено или уже удалено.*", parse_mode="Markdown")
            return
            
        event.is_completed = True
        await acknowledge_delivery(
            db_session, "telegram", event.id, callback.from_user.id
        )
        
        await callback.answer("✅ Задача выполнена!")
        await callback.message.edit_text(
            f"🎉 *Отлично! Задача '{event.title}' отмечена как выполненная.*",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error in complete event callback: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при завершении задачи.")


class AddReminderStates(StatesGroup):
    waiting_for_reminder_datetime = State()


@router.callback_query(F.data.startswith("add_reminder_init:"))
async def handle_add_reminder_init(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    event_id = int(callback.data.split(":")[1])
    
    result = await db_session.execute(select(Event).filter(Event.id == event_id))
    event = result.scalar_one_or_none()
    
    if not event:
        await callback.answer("⚠️ Событие не найдено.")
        return
        
    await state.set_state(AddReminderStates.waiting_for_reminder_datetime)
    await state.update_data(
        event_id=event_id,
        card_message_id=callback.message.message_id
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить добавление", callback_data="cancel_add_reminder")
    
    prompt_message = await callback.message.answer(
        "➕ *Добавление напоминания*\n\n"
        "Отправьте дату и время нового напоминания в формате:\n"
        "`ДД.ММ.ГГГГ ЧЧ:ММ` (например: `29.05.2026 14:15`)\n\n"
        "Для отмены отправьте /cancel или нажмите кнопку ниже.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.update_data(prompt_message_id=prompt_message.message_id)
    await callback.answer()


@router.callback_query(F.data == "cancel_add_reminder")
async def handle_cancel_add_reminder_callback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await callback.answer()
        return
        
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("❌ Добавление отменено")


@router.message(AddReminderStates.waiting_for_reminder_datetime, F.text.in_({"/cancel", "Отмена", "отмена"}))
async def handle_cancel_add_reminder_message(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    await state.clear()
    await message.answer("❌ Добавление напоминания отменено.")
    if prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass


@router.message(AddReminderStates.waiting_for_reminder_datetime)
async def handle_reminder_datetime_input(message: Message, state: FSMContext, db_session: AsyncSession):
    input_text = message.text.strip()
    try:
        parsed_dt = datetime.strptime(input_text, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "⚠️ *Неверный формат!*\n"
            "Пожалуйста, отправьте дату и время строго в формате `ДД.ММ.ГГГГ ЧЧ:ММ`.\n"
            "Пример: `29.05.2026 14:15`.\n"
            "Или отправьте /cancel для отмены."
        )
        return

    data = await state.get_data()
    event_id = data.get("event_id")
    card_message_id = data.get("card_message_id")
    prompt_message_id = data.get("prompt_message_id")
    
    result = await db_session.execute(
        select(Event)
        .filter(Event.id == event_id)
        .options(selectinload(Event.reminders))
    )
    event = result.scalar_one_or_none()
    
    if not event:
        await state.clear()
        await message.answer("⚠️ Извините, событие не найдено. Возможно, оно было отменено.")
        return

    if event.deadline.tzinfo is not None:
        parsed_dt = parsed_dt.replace(tzinfo=event.deadline.tzinfo)

    if parsed_dt >= event.deadline:
        await message.answer(
            "⚠️ *Ошибка:* Время напоминания не может быть позже или равным дедлайну задачи!\n"
            f"Дедлайн задачи: {event.deadline.strftime('%d.%m.%Y в %H:%M')}.\n"
            "Попробуйте ввести другое время."
        )
        return

    # Create new reminder in DB
    new_reminder = Reminder(
        event_id=event.id,
        remind_at=parsed_dt,
        status=ReminderStatus.PENDING
    )
    db_session.add(new_reminder)
    await db_session.commit()
    
    try:
        await message.delete()
    except Exception:
        pass
    if prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass
            
    await state.clear()
    
    # Reload event to reflect new reminder in card
    result = await db_session.execute(
        select(Event)
        .filter(Event.id == event_id)
        .options(selectinload(Event.reminders))
    )
    refreshed_event = result.scalar_one()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{refreshed_event.id}")
    builder.button(text="❌ Отменить", callback_data=f"cancel:{refreshed_event.id}")
    builder.button(text="🔔 Добавить напоминание", callback_data=f"add_reminder_init:{refreshed_event.id}")
    builder.adjust(2, 1)
    
    reminders_list = sorted(refreshed_event.reminders, key=lambda r: r.remind_at)
    reminders_str = "\n".join([f"  - {rem.remind_at.strftime('%Y-%m-%d в %H:%M')}" for rem in reminders_list])
    if not reminders_str:
        reminders_str = "  - Нет предложенных напоминаний"
        
    card_text = (
        f"📝 *Найдено новое событие!*\n\n"
        f"📌 *Задача*: {refreshed_event.title}\n"
        f"⏰ *Дедлайн*: {refreshed_event.deadline.strftime('%Y-%m-%d в %H:%M')}\n"
        f"🔔 *Напоминания*:\n{reminders_str}\n\n"
        f"Всё верно? Подтвердите добавление в план."
    )
    
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=card_message_id,
            text=card_text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to edit confirmation card: {e}")
        await message.answer("✅ Напоминание успешно добавлено!")
