import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from config import settings
from database import init_db, async_session_maker
from handlers import user, pipeline_handlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("PlaniruyBot")


class DbSessionMiddleware(BaseMiddleware):
    """
    Middleware to inject a fresh SQLAlchemy AsyncSession
    into data of every Telegram update, automatically committing/rolling back.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        async with async_session_maker() as session:
            data["db_session"] = session
            return await handler(event, data)


async def set_bot_commands(bot: Bot):
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Перезапустить ассистента"),
        BotCommand(command="calendar", description="Открыть интерактивный календарь"),
        BotCommand(command="list", description="Открыть список дедлайнов (дашборд)")
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands successfully registered in Menu.")


async def main():
    logger.info("Initializing Planiruy Telegram Bot...")
    
    # 1. Initialize DB tables
    logger.info("Initializing database tables...")
    await init_db()
    
    # 2. Verify Telegram Token is set
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please configure it in your .env file.")
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")
        
    # 3. Initialize Bot and Dispatcher
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    await set_bot_commands(bot)
    dp = Dispatcher()
    
    # 4. Attach Database Session Middleware to outer handlers
    dp.update.outer_middleware(DbSessionMiddleware())
    
    # 5. Register Routers
    dp.include_router(user.router)
    dp.include_router(pipeline_handlers.router)
    
    # 5.5. Setup Background Task Scheduler
    from scheduler import setup_scheduler
    setup_scheduler(bot, async_session_maker)
    
    # 6. Start Polling
    logger.info("Starting Telegram Bot long polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
