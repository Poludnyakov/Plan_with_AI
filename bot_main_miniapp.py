import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import MenuButtonDefault, TelegramObject

from config import settings
from database import async_session_maker, init_db
from handlers import miniapp_user, pipeline_handlers


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("PlaniruyBot")


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


async def set_bot_menu(bot: Bot) -> None:
    await bot.delete_my_commands()
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def main() -> None:
    await init_db()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")
    if not settings.APP_URL.startswith("https://"):
        raise ValueError("Telegram Mini App requires an HTTPS APP_URL.")

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    await set_bot_menu(bot)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    dispatcher.include_router(miniapp_user.router)
    dispatcher.include_router(pipeline_handlers.router)

    from scheduler import setup_scheduler
    setup_scheduler(bot, async_session_maker)
    logger.info("Starting Telegram Mini App bot polling...")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
