from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import (
    consume_link_code,
    create_link_code,
    intelligent_reminders_enabled,
    set_intelligent_reminders,
)


router = Router(name="account_commands")


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
    enabled = await intelligent_reminders_enabled(
        db_session, "telegram", message.from_user.id
    )
    await db_session.commit()
    state = "включены" if enabled else "выключены"
    await message.answer(
        f"🧠 Интеллектуальные напоминания сейчас **{state}**.\n\n"
        "Включить: /smart_reminders_on\nВыключить: /smart_reminders_off",
        parse_mode="Markdown",
    )


@router.message(Command("smart_reminders_on"))
async def smart_reminders_on(message: Message, db_session: AsyncSession):
    await set_intelligent_reminders(db_session, "telegram", message.from_user.id, True)
    await message.answer("🧠 Интеллектуальные напоминания включены.")


@router.message(Command("smart_reminders_off"))
async def smart_reminders_off(message: Message, db_session: AsyncSession):
    await set_intelligent_reminders(db_session, "telegram", message.from_user.id, False)
    await message.answer("🔕 Интеллектуальные напоминания выключены. Останутся стандартные интервалы.")
