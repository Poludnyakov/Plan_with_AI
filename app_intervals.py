"""Production entrypoint for interval-aware Telegram Mini App."""

import interval_models
from main import app
from miniapp_interval_router import router as interval_router


app.include_router(interval_router)
