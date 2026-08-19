from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
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


def get_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="📅 Календарь дедлайнов",
            web_app=WebAppInfo(url=miniapp_url("calendar")),
        )]],
        resize_keyboard=True,
        persistent=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db_session: AsyncSession):
    user_repo = UserRepository(db_session)
    tg_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    db_user = await user_repo.get_by_tg_id(tg_id)
    if not db_user:
        await user_repo.create(tg_id=tg_id, timezone="Europe/Moscow")
        await db_session.commit()
        greeting = f"👋 Привет, {username}! Добро пожаловать в «планиИруй!»."
    else:
        greeting = f"👋 С возвращением, {username}!"

    await message.answer(
        greeting + "\n\nОтправьте текст, голосовое сообщение или откройте календарь кнопкой ниже.",
        reply_markup=get_reply_keyboard(),
    )
    await message.answer(
        "Календарь откроется внутри Telegram и войдёт в ваш аккаунт автоматически.",
        reply_markup=miniapp_button("📅 Открыть календарь", "calendar"),
    )


@router.message(Command("calendar"))
@router.message(F.text == "📅 Календарь дедлайнов")
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
