"""Production entrypoint for interval-aware Telegram Mini App."""

import interval_models
from main import app
from miniapp_interval_router import router as interval_router


# Older optional entrypoints register some of the same Mini App URLs on the
# shared FastAPI object.  Keep the interval-aware implementation authoritative
# when modules are loaded together (tests, reloaders, or composite deployments).
interval_route_keys = {
    (route.path, frozenset(route.methods or set())) for route in interval_router.routes
}
app.router.routes[:] = [
    route for route in app.router.routes
    if (getattr(route, "path", None), frozenset(getattr(route, "methods", None) or set()))
    not in interval_route_keys
]
app.include_router(interval_router)


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "planiruy-web"}
