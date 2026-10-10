import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from database import async_session_maker, init_db

from .api import MaxApiClient
from .config import settings
from .handler_features import FeatureMaxUpdateHandler
from .scheduler_full import setup_full_scheduler
from .service import MaxEventService
from .web import router


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PlaniruyMax")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.bot_token:
        raise ValueError("MAX_BOT_TOKEN is not configured")
    if not settings.webhook_secret:
        raise ValueError("MAX_WEBHOOK_SECRET is not configured")
    if not settings.public_base_url.startswith("https://"):
        raise ValueError("MAX_PUBLIC_BASE_URL must use HTTPS")
    await init_db()
    client = MaxApiClient(settings.bot_token, settings.api_base_url)
    app.state.max_client = client
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), settings)
    app.state.max_handler = handler
    scheduler = setup_full_scheduler(client, handler, async_session_maker)
    try:
        try:
            await client.me()
        except Exception:
            logger.exception("Could not read the MAX bot profile; calendar buttons use MAX_BOT_USERNAME")
        try:
            await client.set_commands([
                {"name": "start", "description": "Запустить ассистента"},
                {"name": "help", "description": "Как пользоваться ботом"},
                {"name": "tour", "description": "Обучение за минуту"},
                {"name": "today", "description": "План на сегодня"},
                {"name": "week", "description": "План на 7 дней"},
                {"name": "calendar", "description": "Открыть календарь"},
                {"name": "list", "description": "Показать расписание"},
                {"name": "link", "description": "Объединить Telegram и MAX"},
                {"name": "history", "description": "История добавлений и удаление"},
                {"name": "calendars", "description": "Подключить Apple, Google, Яндекс календарь"},
                {"name": "import", "description": "Загрузить события из другого календаря"},
                {"name": "caldav", "description": "Загрузить календарь по CalDAV"},
                {"name": "smart_reminders", "description": "Настроить умные напоминания"},
            ])
        except Exception:
            logger.exception("Could not update MAX command menu; webhook service stays online")
        if settings.auto_register_webhook:
            try:
                created = await client.ensure_webhook(
                    settings.webhook_url, settings.webhook_secret
                )
                logger.info(
                    "MAX webhook %s", "registered" if created else "already registered"
                )
            except Exception:
                logger.exception(
                    "Could not register MAX webhook automatically; service stays online"
                )
        yield
    finally:
        scheduler.shutdown(wait=False)
        await client.close()


app = FastAPI(title="планиИруй! для MAX", lifespan=lifespan)
app.include_router(router)
# Brand images; in production nginx sends /static to the main app anyway.
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "static", check_dir=False),
    name="static",
)
