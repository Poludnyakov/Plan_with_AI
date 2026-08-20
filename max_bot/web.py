import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker, get_db
from interval_pipeline import REMINDER_OFFSETS, normalize_datetime

from .auth import sign_max_access_token, sign_max_session, validate_max_init_data, verify_max_access_token, verify_max_session
from .calendar import delete_max_yandex, sync_max_yandex
from .config import settings
from .models import MaxEvent, MaxEventTiming, MaxInboxUpdate, MaxReminder, MaxUser
from account_service import ensure_identity
from unified_calendar import (
    create_linked_event,
    delete_linked_event,
    list_linked_events,
    payload as calendar_payload,
    toggle_linked_event,
    update_linked_event,
)


logger = logging.getLogger("MaxWeb")
router = APIRouter(prefix="/max", tags=["MAX Bot and Mini App"])
templates = Jinja2Templates(directory=["max_bot/templates", "templates"])


class AuthRequest(BaseModel):
    init_data: str


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    reminders: list[datetime] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        if self.end_at - self.start_at > timedelta(days=7):
            raise ValueError("Продолжительность не может превышать семь дней")
        return self


class EventUpdate(BaseModel):
    start_at: datetime
    end_at: datetime
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    reminders: list[datetime] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        if self.end_at - self.start_at > timedelta(days=7):
            raise ValueError("Продолжительность не может превышать семь дней")
        return self


async def authenticated_user_id(request: Request) -> int:
    cookie = request.cookies.get("planiruy_max_session")
    if cookie:
        value = verify_max_session(cookie, settings.bot_token)
        if value is not None:
            return value
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        value = verify_max_access_token(authorization[7:].strip(), settings.bot_token)
        if value is not None:
            return value
    raise HTTPException(status_code=401, detail="Откройте календарь заново из MAX")


async def current_user(db: AsyncSession, max_user_id: int) -> MaxUser:
    result = await db.execute(select(MaxUser).filter(MaxUser.max_user_id == max_user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь MAX не найден")
    return user


async def conflict(db: AsyncSession, user_id: int, start: datetime, end: datetime, exclude: int | None = None):
    query = select(MaxEvent, MaxEventTiming).join(MaxEventTiming).filter(
        MaxEvent.user_id == user_id, MaxEvent.status == "confirmed",
        MaxEventTiming.start_at < end, MaxEventTiming.end_at > start,
    )
    if exclude is not None:
        query = query.filter(MaxEvent.id != exclude)
    return (await db.execute(query)).first()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "planiruy-max"}


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    if settings.webhook_secret and request.headers.get("X-Max-Bot-Api-Secret") != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    payload = await request.json()
    identity = (
        ((payload.get("callback") or {}).get("callback_id"))
        or (((payload.get("message") or {}).get("body") or {}).get("mid"))
        or f"{payload.get('update_type')}:{payload.get('timestamp')}"
    )
    key = hashlib.sha256(str(identity).encode()).hexdigest()
    existing = await db.get(MaxInboxUpdate, key)
    if existing:
        if existing.status == "failed":
            background.add_task(process_inbox, request.app, key)
            return {"ok": True, "retry": True}
        return {"ok": True, "duplicate": True}
    db.add(MaxInboxUpdate(key=key, payload=payload))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {"ok": True, "duplicate": True}
    background.add_task(process_inbox, request.app, key)
    return {"ok": True}


async def process_inbox(app, key: str) -> None:
    async with async_session_maker() as db:
        record = await db.get(MaxInboxUpdate, key)
        if not record or record.status == "done":
            return
        record.attempts += 1
        record.status = "processing"
        await db.commit()
        try:
            await app.state.max_handler.handle_update(record.payload, db)
            record.status, record.last_error = "done", None
        except Exception as error:
            await db.rollback()
            record = await db.get(MaxInboxUpdate, key)
            record.status, record.last_error = "failed", str(error)[:2000]
            logger.exception("MAX webhook update %s failed", key)
        await db.commit()


@router.get("/miniapp", response_class=HTMLResponse)
async def miniapp(request: Request):
    return templates.TemplateResponse(request=request, name="miniapp.html", context={"request": request}, headers={"Cache-Control": "no-store"})


@router.post("/api/auth")
async def auth(payload: AuthRequest, db: AsyncSession = Depends(get_db)):
    max_user = validate_max_init_data(payload.init_data, settings.bot_token)
    if not max_user:
        raise HTTPException(status_code=401, detail="Не удалось подтвердить аккаунт MAX")
    max_user_id = int(max_user.get("user_id", max_user.get("id")))
    result = await db.execute(select(MaxUser).filter(MaxUser.max_user_id == max_user_id))
    if not result.scalar_one_or_none():
        db.add(MaxUser(max_user_id=max_user_id))
    await ensure_identity(db, "max", max_user_id)
    await db.commit()
    response = JSONResponse({"redirect": "/max/timeline", "access_token": sign_max_access_token(max_user_id, settings.bot_token)})
    response.set_cookie(
        "planiruy_max_session", sign_max_session(max_user_id, settings.bot_token),
        httponly=True, secure=True, samesite="none", max_age=30 * 86400, path="/max",
    )
    return response


@router.get("/timeline", response_class=HTMLResponse)
async def timeline(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="calendar_reliable.html",
        context={
            "request": request,
            "calendar_config": {
                "apiBase": "/max/api/events",
                "reauthUrl": "/max/miniapp",
                "tokenKey": "planiruy_max_access_token",
                "platform": "max",
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/events")
async def events(max_user_id: int = Depends(authenticated_user_id), db: AsyncSession = Depends(get_db)):
    entries = await list_linked_events(db, "max", max_user_id)
    return [calendar_payload(entry) for entry in entries]


@router.post("/api/events", status_code=status.HTTP_201_CREATED)
async def create_event(payload: EventCreate, max_user_id: int = Depends(authenticated_user_id), db: AsyncSession = Depends(get_db)):
    try:
        entry = await create_linked_event(
            db, "max", max_user_id, payload.title, payload.description or "",
            payload.start_at, payload.end_at, payload.reminders,
        )
        return calendar_payload(entry)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch("/api/events/{event_ref}")
async def update_event(
    event_ref: str, payload: EventUpdate,
    max_user_id: int = Depends(authenticated_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await update_linked_event(
            db, "max", max_user_id, event_ref, payload.start_at, payload.end_at,
            payload.title, payload.description, payload.reminders,
        )
        return calendar_payload(entry)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/events/{event_id}/toggle-complete")
async def toggle(event_id: str, max_user_id: int = Depends(authenticated_user_id), db: AsyncSession = Depends(get_db)):
    try:
        entry = await toggle_linked_event(db, "max", max_user_id, event_id)
        return {"id": entry.ref, "is_completed": entry.event.is_completed}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/events/{event_id}")
async def remove(event_id: str, max_user_id: int = Depends(authenticated_user_id), db: AsyncSession = Depends(get_db)):
    try:
        await delete_linked_event(db, "max", max_user_id, event_id)
        return {"status": "deleted"}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
