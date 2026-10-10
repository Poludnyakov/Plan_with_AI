from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from account_service import complete_web_login, ensure_identity
from repositories import UserRepository
from onboarding import commands_text, first_run_buttons, tour_page
from usage_guide import usage_guide
from schedule_service import discard_pending_imports


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


def start_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📅 Открыть календарь",
            web_app=WebAppInfo(url=miniapp_url("calendar")),
        )],
        [InlineKeyboardButton(text="ℹ️ Как пользоваться", callback_data="usage_help")],
        [InlineKeyboardButton(text="📖 Обучение за минуту", callback_data="tour:start")],
    ])


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
    cleared_sources, cleared_drafts = await discard_pending_imports(
        db_session, "telegram", tg_id
    )
    await db_session.commit()
    profile_name = message.from_user.full_name.strip() or "друг"
    db_user = await user_repo.get_by_tg_id(tg_id)
    first_time = not db_user
    if first_time:
        await user_repo.create(tg_id=tg_id, timezone="Europe/Moscow")
        await db_session.commit()
        greeting = f"👋 Привет, {profile_name}! Добро пожаловать в «планиИруй!»."
    else:
        greeting = f"👋 С возвращением, {profile_name}!"

    reset_notice = (
        "\n\nПредыдущий незавершённый импорт расписания отменён."
        if cleared_sources or cleared_drafts else ""
    )
    # A reply-keyboard Mini App gets no initData from Telegram and cannot sign
    # in, so an old keyboard of that kind is removed in favour of the menu button.
    await message.answer(
        greeting + reset_notice
        + "\n\nОтправьте текст, голосовое сообщение или откройте календарь "
        "кнопкой «Календарь» слева от поля ввода."
        + ("\n\n" + commands_text("telegram") if first_time else ""),
        reply_markup=ReplyKeyboardRemove(),
    )
    if first_time:
        # The first start: the tour begins right away, the calendar is one tap away.
        from handlers.onboarding import draw

        text, _ = tour_page("telegram", "tour:0")
        await message.answer(text, reply_markup=draw(first_run_buttons()))
        return
    await message.answer(
        "Календарь откроется внутри Telegram и войдёт в ваш аккаунт автоматически.",
        reply_markup=start_buttons(),
    )


@router.message(F.web_app_data)
async def stale_keyboard_calendar(message: Message):
    """The old reply-keyboard button opened the calendar without a sign-in.

    Its page reports this through `sendData`; replace the keyboard with the
    buttons that Telegram launches with the user's signed data.
    """
    await message.answer(
        "Календарь теперь открывается кнопкой «Календарь» слева от поля ввода.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Или нажмите здесь:",
        reply_markup=miniapp_button("📅 Открыть календарь", "calendar"),
    )


@router.message(Command("help", "instruction", "guide"))
async def cmd_help(message: Message):
    await message.answer(usage_guide("telegram"))


@router.callback_query(F.data == "usage_help")
async def callback_help(callback: CallbackQuery):
    if callback.message:
        await callback.message.answer(usage_guide("telegram"))
    await callback.answer()


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
