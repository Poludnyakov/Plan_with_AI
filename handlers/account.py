from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram import F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import (
    consume_link_code,
    create_link_code,
    intelligent_reminders_enabled,
    set_intelligent_reminders,
)
from reminder_service import (
    acknowledge_delivery,
    preference_text,
    reminder_preference,
    snooze_delivery,
    update_reminder_preference,
)
from unified_calendar import get_owned_entry


router = Router(name="account_commands")


def reminder_panel(preference):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🔔 Уведомления: {'вкл' if preference.enabled else 'выкл'}",
        callback_data="reminder_pref:enabled",
    )
    builder.button(text="Количество: изменить", callback_data="reminder_pref:frequency")
    builder.button(
        text=f"🌅 Сводка: {'вкл' if preference.daily_summary else 'выкл'}",
        callback_data="reminder_pref:summary",
    )
    builder.button(
        text=f"🕗 Время: {preference.summary_hour:02d}:00",
        callback_data="reminder_pref:summary_hour",
    )
    builder.button(
        text=f"⏰ Позже: {preference.snooze_minutes} мин",
        callback_data="reminder_pref:snooze",
    )
    builder.button(
        text=f"💳 AI: {'вкл' if preference.use_ai else 'выкл'}",
        callback_data="reminder_pref:ai",
    )
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("link", "connect"))
async def link_accounts(message: Message, db_session: AsyncSession):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        code = await create_link_code(db_session, "telegram", message.from_user.id)
        await message.answer(
            "🔗 **Объединение Telegram и MAX**\n\n"
            f"Отправьте в другом боте команду:\n`/link {code}`\n\n"
            "Код действует 10 минут. После подключения оба календаря покажут одно расписание.",
            parse_mode="Markdown",
        )
        return
    try:
        identities = await consume_link_code(
            db_session, "telegram", message.from_user.id, parts[1]
        )
        platforms = " и ".join("Telegram" if key == "telegram" else "MAX" for key in identities)
        await message.answer(f"✅ Аккаунты объединены: {platforms}. Календарь теперь общий.")
    except ValueError as error:
        await db_session.rollback()
        await message.answer(f"❌ {error}")


@router.message(Command("smart_reminders", "reminders"))
async def smart_reminders_status(message: Message, db_session: AsyncSession):
    preference = await update_reminder_preference(
        db_session, "telegram", message.from_user.id, "platform"
    )
    await message.answer(
        preference_text(preference),
        parse_mode="Markdown",
        reply_markup=reminder_panel(preference),
    )


@router.message(Command("smart_reminders_on"))
async def smart_reminders_on(message: Message, db_session: AsyncSession):
    preference = await reminder_preference(db_session, "telegram", message.from_user.id)
    if not preference.use_ai:
        preference = await update_reminder_preference(
            db_session, "telegram", message.from_user.id, "ai"
        )
    await message.answer(
        "💳 AI-анализ времени включён. Он может расходовать средства Yandex Cloud.",
        reply_markup=reminder_panel(preference),
    )


@router.message(Command("smart_reminders_off"))
async def smart_reminders_off(message: Message, db_session: AsyncSession):
    preference = await reminder_preference(db_session, "telegram", message.from_user.id)
    if preference.use_ai:
        preference = await update_reminder_preference(
            db_session, "telegram", message.from_user.id, "ai"
        )
    await message.answer(
        "✅ Платный AI отключён. Используются бесплатные локальные правила.",
        reply_markup=reminder_panel(preference),
    )


@router.callback_query(F.data.startswith("reminder_pref:"))
async def reminder_preference_callback(callback: CallbackQuery, db_session: AsyncSession):
    action = callback.data.split(":", 1)[1]
    preference = await update_reminder_preference(
        db_session, "telegram", callback.from_user.id, action
    )
    await callback.answer("Настройка сохранена")
    await callback.message.edit_text(
        preference_text(preference), parse_mode="Markdown",
        reply_markup=reminder_panel(preference),
    )


@router.callback_query(F.data.startswith("reminder_ack:"))
async def reminder_ack_callback(callback: CallbackQuery, db_session: AsyncSession):
    _, prefix, raw_event_id = callback.data.split(":", 2)
    event_ref = f"{prefix}:{raw_event_id}"
    entry = await get_owned_entry(
        db_session, "telegram", callback.from_user.id, event_ref
    )
    if not entry:
        await callback.answer("Событие не найдено", show_alert=True)
        return
    await acknowledge_delivery(
        db_session, entry.source, entry.event.id, callback.from_user.id
    )
    await callback.answer("Учтено")
    await callback.message.edit_text(f"✅ Учтено: {entry.event.title}")


@router.callback_query(F.data.startswith("reminder_snooze:"))
async def reminder_snooze_callback(callback: CallbackQuery, db_session: AsyncSession):
    _, prefix, raw_event_id = callback.data.split(":", 2)
    event_ref = f"{prefix}:{raw_event_id}"
    entry = await get_owned_entry(
        db_session, "telegram", callback.from_user.id, event_ref
    )
    if not entry:
        await callback.answer("Событие не найдено", show_alert=True)
        return
    try:
        snooze_at, minutes = await snooze_delivery(
            db_session, entry.source, entry.event.id, callback.from_user.id,
            entry.timing.start_at,
        )
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer(f"Напомню через {minutes} мин")
    await callback.message.edit_text(
        f"⏰ Напомню о «{entry.event.title}» в {snooze_at.astimezone():%H:%M}."
    )
