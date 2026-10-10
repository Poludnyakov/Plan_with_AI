"""Telegram: import from other calendars (.ics file, iCal link, CalDAV)."""

import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from calendar_import_service import (
    CALDAV_HELP,
    IMPORT_HELP,
    MAX_ICS_BYTES,
    CalendarImportError,
    expects_caldav,
    find_ics_link,
    import_from_caldav,
    import_from_file,
    import_from_link,
    is_ics_filename,
    parse_caldav_credentials,
    result_text,
    stop_waiting_caldav,
    undo_imports,
    undo_payload,
    wait_for_caldav,
)
from handlers.miniapp_user import miniapp_button
from unified_calendar import ensure_platform_user


logger = logging.getLogger("CalendarImport")
router = Router()


def undo_keyboard(results):
    payload = undo_payload(results)
    if not payload:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Отменить импорт", callback_data=payload)
    return builder.as_markup()


async def finish(message: Message, status: Message, db: AsyncSession, run) -> None:
    """Run an import and replace the «loading» note with the result."""
    try:
        results = await run()
        user = await ensure_platform_user(db, "telegram", message.from_user.id)
        await status.edit_text(result_text(results, user.timezone), reply_markup=undo_keyboard(results))
    except CalendarImportError as error:
        await db.rollback()
        await status.edit_text(f"❌ {error}")
    except Exception as error:
        await db.rollback()
        logger.error("Calendar import failed: %s", error, exc_info=True)
        await status.edit_text("❌ Не удалось загрузить календарь. Попробуйте ещё раз позже.")


@router.message(Command("import"))
async def cmd_import(message: Message):
    await message.answer(IMPORT_HELP)


@router.message(Command("caldav"))
async def cmd_caldav(message: Message):
    wait_for_caldav("telegram", message.from_user.id)
    await message.answer(CALDAV_HELP)


@router.message(lambda message: bool(message.document) and is_ics_filename(message.document.file_name, message.document.mime_type))
async def ics_file(message: Message, db_session: AsyncSession):
    document = message.document
    if document.file_size and document.file_size > MAX_ICS_BYTES:
        await message.answer("❌ Календарь слишком большой: максимум 5 МБ.")
        return
    status = await message.answer("⏳ Загружаю события из календаря…")
    data = BytesIO()
    await message.bot.download(document, destination=data)
    await finish(message, status, db_session, lambda: import_from_file(
        db_session, "telegram", message.from_user.id, data.getvalue(), document.file_name or "Календарь",
    ))


@router.message(lambda message: bool(message.text) and not message.text.startswith("/") and find_ics_link(message.text) is not None)
async def ics_link(message: Message, db_session: AsyncSession):
    status = await message.answer("⏳ Загружаю календарь по ссылке…")
    await finish(message, status, db_session, lambda: import_from_link(
        db_session, "telegram", message.from_user.id, find_ics_link(message.text),
    ))


@router.message(lambda message: bool(message.text) and expects_caldav("telegram", message.from_user.id))
async def caldav_access(message: Message, db_session: AsyncSession):
    if message.text.startswith("/"):
        stop_waiting_caldav("telegram", message.from_user.id)
        return
    credentials = parse_caldav_credentials(message.text)
    if not credentials:
        stop_waiting_caldav("telegram", message.from_user.id)
        await message.answer(
            "Не понял данные для CalDAV: нужны три строки — сервер, логин и пароль приложения. "
            "Отправьте /caldav, чтобы попробовать снова."
        )
        return
    stop_waiting_caldav("telegram", message.from_user.id)
    try:
        await message.delete()  # the password should not stay in the chat
        note = ""
    except Exception:
        note = "\nУдалите, пожалуйста, сообщение с паролем из чата."
    status = await message.answer("⏳ Подключаюсь к календарю по CalDAV…" + note)
    await finish(message, status, db_session, lambda: import_from_caldav(
        db_session, "telegram", message.from_user.id, *credentials,
    ))


@router.callback_query(F.data.startswith("calimp_undo:"))
async def undo(callback: CallbackQuery, db_session: AsyncSession):
    try:
        text = await undo_imports(db_session, "telegram", callback.from_user.id, callback.data)
    except Exception as error:
        await db_session.rollback()
        logger.error("Calendar import undo failed: %s", error, exc_info=True)
        await callback.answer("Не удалось отменить импорт.", show_alert=True)
        return
    await callback.answer("Готово")
    if callback.message:
        await callback.message.edit_text(text, reply_markup=miniapp_button("📅 Открыть календарь", "calendar"))
