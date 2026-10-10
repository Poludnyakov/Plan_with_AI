import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import BotCommand, MenuButtonWebApp, TelegramObject, WebAppInfo

import interval_models  # registers event_timings before create_all
from config import settings
from database import async_session_maker, init_db
from handlers import account, calendar_import, event_cancellation, miniapp_user, onboarding, pipeline_handlers, pipeline_handlers_intervals
from handlers.miniapp_user import miniapp_url
from telegram_custom_emoji import CustomEmojiMiddleware


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PlaniruyIntervalBot")


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session_maker() as session:
            data["db_session"] = session
            return await handler(event, data)


async def configure_telegram_menu(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать заново и отменить импорт"),
        BotCommand(command="help", description="Как пользоваться ботом"),
        BotCommand(command="tour", description="Обучение за минуту"),
        BotCommand(command="today", description="План на сегодня"),
        BotCommand(command="week", description="План на 7 дней"),
        BotCommand(command="calendar", description="Открыть календарь"),
        BotCommand(command="list", description="Открыть список дел"),
        BotCommand(command="reminders", description="Настроить напоминания"),
        BotCommand(command="link", description="Объединить Telegram и MAX"),
        BotCommand(command="history", description="История добавлений и удаление"),
        BotCommand(command="calendars", description="Подключить Apple, Google, Яндекс календарь"),
        BotCommand(command="import", description="Загрузить события из другого календаря"),
        BotCommand(command="caldav", description="Загрузить календарь по CalDAV"),
    ])
    # The menu button is the one entry point Telegram launches with signed
    # initData, so the calendar signs in without an extra message.
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Календарь", web_app=WebAppInfo(url=miniapp_url("calendar")),
    ))


async def main() -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    if not settings.APP_URL.startswith("https://"):
        raise ValueError("APP_URL must use HTTPS")
    await init_db()
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    if settings.TELEGRAM_CUSTOM_EMOJI:
        bot.session.middleware(CustomEmojiMiddleware())
    await configure_telegram_menu(bot)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    dispatcher.include_router(miniapp_user.router)
    dispatcher.include_router(account.router)
    dispatcher.include_router(calendar_import.router)
    dispatcher.include_router(onboarding.router)
    dispatcher.include_router(event_cancellation.router)
    dispatcher.include_router(pipeline_handlers_intervals.router)
    dispatcher.include_router(pipeline_handlers.router)

    from scheduler import setup_scheduler
    setup_scheduler(bot, async_session_maker)
    logger.info("Starting interval-aware bot polling")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
