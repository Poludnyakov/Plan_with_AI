from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession
from repositories import UserRepository
from config import settings

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, db_session: AsyncSession):
    """
    Handles the /start command.
    Checks if the user exists in the database. If not, registers them.
    """
    user_repo = UserRepository(db_session)
    tg_id = message.from_user.id
    profile_name = message.from_user.full_name.strip() or "друг"
    
    # Check if user exists
    db_user = await user_repo.get_by_tg_id(tg_id)
    if not db_user:
        # Create a new user
        db_user = await user_repo.create(tg_id=tg_id, timezone="Europe/Moscow")
        await db_session.commit()
        greeting = (
            f"👋 Привет, {profile_name}! Рад приветствовать тебя в ИИ-ассистенте *«планиИруй!»*.\n\n"
            f"Я помогу тебе организовать учебный график и напомню о важных дедлайнах.\n"
            f"Отправь мне текстовое или голосовое сообщение с описанием твоих планов, "
            f"например: *'В среду лекция по физике в 10:00 в 305 аудитории'*.\n\n"
            f"Используй ссылки ниже для ручного управления задачами!"
        )
    else:
        greeting = (
            f"👋 С возвращением, {profile_name}!\n\n"
            f"Готов записать новые события. Отправь мне текстовое или голосовое сообщение."
        )
        
    await message.answer(
        greeting, 
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    # Also send the links as a follow-up with direct, bulletproof Markdown links inside
    await message.answer(
        f"🔗 Ссылки быстрого доступа к твоему расписанию:\n\n"
        f"📅 [Открыть Календарь]({settings.APP_URL}/calendar)\n"
        f"📊 [Открыть Список дедлайнов]({settings.APP_URL}/dashboard)",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


@router.message(Command("calendar"))
async def cmd_calendar(message: Message):
    """
    Handles the /calendar command or persistent button clicks.
    Sends direct, bulletproof Markdown links to launch the student's personal calendar grid.
    """
    text = (
        "📅 *Ваш персональный интерактивный календарь готов!*\n\n"
        f"🔗 [Нажмите сюда, чтобы открыть Календарь]({settings.APP_URL}/calendar)\n\n"
        "Вы можете кликать по сетке часов, чтобы мгновенно добавлять новые дедлайны вручную с автоматической рассылкой напоминаний!"
    )
    await message.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    """
    Handles the /list command.
    Sends a beautiful message with a clickable link to open the student's personal dashboard.
    """
    url = f"{settings.APP_URL}/dashboard"
    
    text = (
        "📅 <b>Ваша персональная таблица дедлайнов готова!</b>\n\n"
        f"🔗 <a href='{url}'>Нажмите сюда, чтобы открыть веб-таблицу</a>"
    )
    
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
