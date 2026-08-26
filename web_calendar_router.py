import hashlib
import hmac
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_models import AccountIdentity, WebLoginTicket
from account_service import aware, create_web_login_ticket, ensure_identity
from dashboard_router import get_bot_username, get_cookie_secret, verify_tg_id
from database import get_db
from max_bot.config import settings as max_settings
from unified_calendar import (
    create_linked_event,
    delete_linked_event,
    list_linked_events,
    payload as calendar_payload,
    toggle_linked_event,
    update_linked_event,
)
from schedule_service import list_schedule_occurrences


router = APIRouter(tags=["Web calendar"])
templates = Jinja2Templates(directory="templates")
ACCOUNT_COOKIE = "planiruy_account_session"


def sign_account_id(account_id: int) -> str:
    value = str(account_id)
    signature = hmac.new(
        get_cookie_secret().encode(), f"account:{value}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{value}.{signature}"


def verify_account_id(value: str | None) -> int | None:
    try:
        raw, signature = (value or "").split(".", 1)
        expected = hmac.new(
            get_cookie_secret().encode(), f"account:{raw}".encode(), hashlib.sha256
        ).hexdigest()
        return int(raw) if hmac.compare_digest(signature, expected) else None
    except (TypeError, ValueError):
        return None


async def browser_identity(request: Request, db: AsyncSession) -> tuple[str, int, int]:
    account_id = verify_account_id(request.cookies.get(ACCOUNT_COOKIE))
    if account_id is not None:
        identities = (await db.execute(
            select(AccountIdentity)
            .filter(AccountIdentity.account_id == account_id)
            .order_by(AccountIdentity.platform.desc())
        )).scalars().all()
        if identities:
            identity = identities[0]
            return identity.platform, identity.external_id, account_id

    telegram_cookie = request.cookies.get("planiruy_session")
    tg_id = verify_tg_id(telegram_cookie, get_cookie_secret()) if telegram_cookie else None
    if tg_id is not None:
        identity = await ensure_identity(db, "telegram", tg_id)
        await db.commit()
        return "telegram", tg_id, identity.account_id
    raise HTTPException(status_code=401, detail="Войдите через Telegram или MAX")


class CalendarEventInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    all_day: bool = False
    reminders: list[datetime] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        if self.end_at - self.start_at > timedelta(days=366 if self.all_day else 7):
            raise ValueError("Продолжительность не может превышать семь дней")
        return self


class CalendarEventUpdate(BaseModel):
    start_at: datetime
    end_at: datetime
    all_day: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    reminders: list[datetime] | None = Field(default=None, max_length=10)


@router.get("/calendar", response_class=HTMLResponse)
async def web_calendar(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        await browser_identity(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="calendar_reliable.html",
        context={
            "request": request,
            "calendar_config": {
                "apiBase": "/api/calendar/events",
                "reauthUrl": "/login",
                "tokenKey": "",
                "platform": "web",
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/telegram/start")
async def telegram_login_start(db: AsyncSession = Depends(get_db)):
    ticket = await create_web_login_ticket(db, "telegram")
    username = (await get_bot_username()).strip().lstrip("@")
    return {
        "token": ticket.token,
        "code": ticket.short_code,
        "deep_link": f"https://t.me/{username}?start=web_{ticket.short_code}",
        "command": f"/start web_{ticket.short_code}",
        "expires_in": 600,
    }


@router.get("/api/auth/telegram/status/{token}")
async def telegram_login_status(token: str, db: AsyncSession = Depends(get_db)):
    ticket = await db.get(WebLoginTicket, token)
    if (
        not ticket
        or ticket.platform != "telegram"
        or aware(ticket.expires_at) < datetime.now(aware(ticket.expires_at).tzinfo)
    ):
        raise HTTPException(status_code=404, detail="Ссылка входа истекла")
    if not ticket.completed_at or ticket.account_id is None:
        return {"status": "pending"}
    response = JSONResponse({"status": "complete", "redirect": "/calendar"})
    response.set_cookie(
        ACCOUNT_COOKIE, sign_account_id(ticket.account_id), httponly=True,
        secure=True, samesite="lax", max_age=30 * 86400, path="/",
    )
    return response


@router.post("/api/auth/max/start")
async def max_login_start(db: AsyncSession = Depends(get_db)):
    ticket = await create_web_login_ticket(db, "max")
    username = max_settings.bot_username.strip().lstrip("@")
    deep_link = f"https://max.ru/{username}?start=web_{ticket.short_code}" if username else None
    return {
        "token": ticket.token,
        "code": ticket.short_code,
        "deep_link": deep_link,
        "command": f"/start web_{ticket.short_code}",
        "expires_in": 600,
    }


@router.get("/api/auth/max/status/{token}")
async def max_login_status(token: str, db: AsyncSession = Depends(get_db)):
    ticket = await db.get(WebLoginTicket, token)
    if not ticket or ticket.platform != "max" or aware(ticket.expires_at) < datetime.now(aware(ticket.expires_at).tzinfo):
        raise HTTPException(status_code=404, detail="Ссылка входа истекла")
    if not ticket.completed_at or ticket.account_id is None:
        return {"status": "pending"}
    response = JSONResponse({"status": "complete", "redirect": "/calendar"})
    response.set_cookie(
        ACCOUNT_COOKIE, sign_account_id(ticket.account_id), httponly=True,
        secure=True, samesite="lax", max_age=30 * 86400, path="/",
    )
    return response


@router.get("/api/calendar/events")
async def web_events(request: Request, db: AsyncSession = Depends(get_db)):
    platform, external_id, _ = await browser_identity(request, db)
    entries = await list_linked_events(db, platform, external_id)
    try:
        schedule = await list_schedule_occurrences(db, platform, external_id)
    except StopAsyncIteration:
        schedule = []
    return [calendar_payload(entry) for entry in entries] + schedule


@router.post("/api/calendar/events", status_code=status.HTTP_201_CREATED)
async def web_create(payload: CalendarEventInput, request: Request, db: AsyncSession = Depends(get_db)):
    platform, external_id, _ = await browser_identity(request, db)
    try:
        entry = await create_linked_event(
            db, platform, external_id, payload.title, payload.description or "",
            payload.start_at, payload.end_at, payload.reminders, payload.all_day,
        )
        return calendar_payload(entry)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch("/api/calendar/events/{event_ref}")
async def web_update(event_ref: str, payload: CalendarEventUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    platform, external_id, _ = await browser_identity(request, db)
    try:
        entry = await update_linked_event(
            db, platform, external_id, event_ref, payload.start_at, payload.end_at,
            payload.title, payload.description, payload.reminders, payload.all_day,
        )
        return calendar_payload(entry)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/calendar/events/{event_ref}/toggle-complete")
async def web_toggle(event_ref: str, request: Request, db: AsyncSession = Depends(get_db)):
    platform, external_id, _ = await browser_identity(request, db)
    try:
        entry = await toggle_linked_event(db, platform, external_id, event_ref)
        return {"id": entry.ref, "is_completed": entry.event.is_completed}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/calendar/events/{event_ref}")
async def web_delete(event_ref: str, request: Request, db: AsyncSession = Depends(get_db)):
    platform, external_id, _ = await browser_identity(request, db)
    try:
        await delete_linked_event(db, platform, external_id, event_ref)
        return {"status": "deleted"}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
