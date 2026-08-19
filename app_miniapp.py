"""FastAPI entrypoint with Telegram Mini App authentication enabled."""

from main import app
from miniapp_router import router as miniapp_router


app.include_router(miniapp_router)
