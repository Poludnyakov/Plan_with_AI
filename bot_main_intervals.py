import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import MenuButtonDefault, TelegramObject

import interval_models  # registers event_timings before create_all
from config import settings
from database import async_session_maker, init_db
from handlers import account, event_cancellation, miniapp_user, pipeline_handlers, pipeline_handlers_intervals


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


async def main() -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    if not settings.APP_URL.startswith("https://"):
        raise ValueError("APP_URL must use HTTPS")
    await init_db()
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    await bot.delete_my_commands()
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    dispatcher.include_router(miniapp_user.router)
    dispatcher.include_router(account.router)
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
