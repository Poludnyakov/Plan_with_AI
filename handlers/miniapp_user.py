from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from account_service import complete_web_login, ensure_identity
from repositories import UserRepository


router = Router()


def miniapp_url(destination: str = "calendar") -> str:
    safe_destination = destination if destination in {"calendar", "dashboard"} else "calendar"
    return f"{settings.APP_URL.rstrip('/')}/miniapp?destination={safe_destination}"


def miniapp_button(text: str, destination: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text=text,
            web_app=WebAppInfo(url=miniapp_url(destination)),
        )]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db_session: AsyncSession):
    user_repo = UserRepository(db_session)
    tg_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("web_"):
        try:
            await complete_web_login(db_session, "telegram", tg_id, parts[1][4:])
            await message.answer("✅ Вход на сайт подтверждён. Можно вернуться в браузер.")
        except ValueError as error:
            await db_session.rollback()
            await message.answer(f"❌ {error}")
        return
    await ensure_identity(db_session, "telegram", tg_id)
    await db_session.commit()
    profile_name = message.from_user.full_name.strip() or "друг"
    db_user = await user_repo.get_by_tg_id(tg_id)
    if not db_user:
        await user_repo.create(tg_id=tg_id, timezone="Europe/Moscow")
        await db_session.commit()
        greeting = f"👋 Привет, {profile_name}! Добро пожаловать в «планиИруй!»."
    else:
        greeting = f"👋 С возвращением, {profile_name}!"

    await message.answer(
        greeting + "\n\nОтправьте текст, голосовое сообщение или откройте календарь кнопкой ниже.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Календарь откроется внутри Telegram и войдёт в ваш аккаунт автоматически.",
        reply_markup=miniapp_button("📅 Открыть календарь", "calendar"),
    )


@router.message(Command("calendar"))
async def cmd_calendar(message: Message):
    await message.answer(
        "📅 Ваш персональный календарь готов.",
        reply_markup=miniapp_button("Открыть календарь", "calendar"),
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    await message.answer(
        "📊 Ваш список дедлайнов готов.",
        reply_markup=miniapp_button("Открыть список", "dashboard"),
    )
