"""FastAPI entrypoint for the lightweight Telegram Mini App calendar UI."""

from main import app
from miniapp_ui_router import router as miniapp_ui_router


app.include_router(miniapp_ui_router)
