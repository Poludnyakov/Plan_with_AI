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
from calendar_feed_service import connect_text, feed_links, feed_token
from history_service import (
    addition_history,
    delete_history_item,
    deletion_question,
    find_history_item,
    history_page,
)
from config import settings
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


def calendar_feed_keyboard(links: dict[str, str]):
    builder = InlineKeyboardBuilder()
    builder.button(text="📆 Подключить календарь", url=links["connect"])
    builder.button(text="📅 Google Календарь", url=links["google"])
    builder.button(text="♻️ Новая ссылка", callback_data="calendar_feed:reset")
    builder.adjust(1)
    return builder.as_markup()


async def send_calendar_feed(message: Message, db_session: AsyncSession, tg_id: int, reset: bool) -> None:
    token = await feed_token(db_session, "telegram", tg_id, reset=reset)
    links = feed_links(token, settings.APP_URL)
    prefix = "♻️ Старая ссылка отключена.\n\n" if reset else ""
    await message.answer(
        prefix + connect_text(links), parse_mode=None,
        reply_markup=calendar_feed_keyboard(links), disable_web_page_preview=True,
    )


@router.message(Command("calendars", "subscribe", "ics"))
async def calendar_feed_command(message: Message, db_session: AsyncSession):
    argument = ((message.text or "").split(maxsplit=1) + [""])[1].strip().casefold()
    await send_calendar_feed(
        message, db_session, message.from_user.id, reset=argument in {"new", "reset", "сброс", "новая"},
    )


@router.callback_query(F.data == "calendar_feed:reset")
async def calendar_feed_reset(callback: CallbackQuery, db_session: AsyncSession):
    await callback.answer("Старая ссылка отключена")
    await send_calendar_feed(callback.message, db_session, callback.from_user.id, reset=True)


def history_keyboard(page, offset: int, more: bool):
    builder = InlineKeyboardBuilder()
    sizes = []
    deletable = [(number, item) for number, item in enumerate(page, offset + 1) if item.deletable]
    for number, item in deletable:
        builder.button(text=f"🗑 {number}", callback_data=f"hist:ask:{item.ref}")
    sizes += [4] * (len(deletable) // 4) + ([len(deletable) % 4] if len(deletable) % 4 else [])
    navigation = 0
    if offset:
        builder.button(text="⬅️ Новее", callback_data=f"hist:page:{max(0, offset - 8)}")
        navigation += 1
    if more:
        builder.button(text="➡️ Ещё", callback_data=f"hist:page:{offset + 8}")
        navigation += 1
    if navigation:
        sizes.append(navigation)
    if sizes:
        builder.adjust(*sizes)
    return builder.as_markup()


async def history_view(db_session: AsyncSession, tg_id: int, offset: int = 0):
    items = await addition_history(db_session, "telegram", tg_id)
    text, page, more = history_page(items, offset)
    return text, history_keyboard(page, offset, more)


@router.message(Command("history"))
async def history_command(message: Message, db_session: AsyncSession):
    text, markup = await history_view(db_session, message.from_user.id)
    await message.answer(text, parse_mode=None, reply_markup=markup)


@router.callback_query(F.data.startswith("hist:"))
async def history_callback(callback: CallbackQuery, db_session: AsyncSession):
    _, action, ref = (callback.data.split(":", 2) + [""])[:3]
    tg_id = callback.from_user.id
    if action == "page":
        text, markup = await history_view(db_session, tg_id, int(ref or 0))
        await callback.answer()
        await callback.message.edit_text(text, parse_mode=None, reply_markup=markup)
        return
    if action == "no":
        text, markup = await history_view(db_session, tg_id)
        await callback.answer("Ничего не удалено")
        await callback.message.edit_text(text, parse_mode=None, reply_markup=markup)
        return
    if action == "ask":
        item = await find_history_item(db_session, "telegram", tg_id, ref)
        if not item or not item.deletable:
            await callback.answer("Запись уже удалена.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Да, удалить", callback_data=f"hist:del:{ref}")
        builder.button(text="Отмена", callback_data="hist:no:")
        builder.adjust(2)
        await callback.answer()
        await callback.message.edit_text(
            deletion_question(item), parse_mode=None, reply_markup=builder.as_markup()
        )
        return
    if action == "del":
        try:
            result = await delete_history_item(db_session, "telegram", tg_id, ref)
        except Exception:
            await db_session.rollback()
            await callback.answer("Не удалось удалить. Попробуйте ещё раз.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="📜 К истории", callback_data="hist:page:0")
        await callback.answer("Удалено")
        await callback.message.edit_text(result, parse_mode=None, reply_markup=builder.as_markup())
        return
    await callback.answer()


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
