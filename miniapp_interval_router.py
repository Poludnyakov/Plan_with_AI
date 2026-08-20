import asyncio
import hashlib
import hmac
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import interval_models
from config import settings
from dashboard_router import get_cookie_secret, sign_tg_id, verify_tg_id
from database import get_db
from interval_calendar_sync import delete_yandex_interval, sync_yandex_interval
from interval_models import EventTiming
from interval_pipeline import REMINDER_OFFSETS, normalize_datetime
from models import Event, EventStatus, Reminder, ReminderStatus, User
from telegram_miniapp_auth import validate_telegram_init_data
from account_service import ensure_identity
from unified_calendar import (
    create_linked_event,
    delete_linked_event,
    list_linked_events,
    payload as calendar_payload,
    toggle_linked_event,
    update_linked_event,
)


logger = logging.getLogger("MiniAppIntervalRouter")
router = APIRouter(tags=["Telegram Mini App Intervals"])
templates = Jinja2Templates(directory="templates")


class MiniAppAuthRequest(BaseModel):
    init_data: str
    destination: Literal["calendar", "dashboard"] = "calendar"


MINIAPP_TOKEN_TTL_SECONDS = 24 * 3600


def sign_miniapp_access_token(tg_id: int, issued_at: int | None = None) -> str:
    issued = int(time.time()) if issued_at is None else issued_at
    payload = f"{tg_id}.{issued}"
    signature = hmac.new(
        get_cookie_secret().encode("utf-8"),
        f"miniapp:{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_miniapp_access_token(
    token: str, now: int | None = None
) -> int | None:
    try:
        tg_id_text, issued_text, signature = token.split(".", 2)
        issued = int(issued_text)
        current = int(time.time()) if now is None else now
        if issued > current + 60 or current - issued > MINIAPP_TOKEN_TTL_SECONDS:
            return None
        payload = f"{tg_id_text}.{issued}"
        expected = hmac.new(
            get_cookie_secret().encode("utf-8"),
            f"miniapp:{payload}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(signature, expected):
            return int(tg_id_text)
    except (AttributeError, TypeError, ValueError):
        return None
    return None


async def get_miniapp_user_tg_id(request: Request) -> int:
    session_cookie = request.cookies.get("planiruy_session")
    if session_cookie:
        tg_id = verify_tg_id(session_cookie, get_cookie_secret())
        if tg_id is not None:
            return tg_id

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        tg_id = verify_miniapp_access_token(authorization[7:].strip())
        if tg_id is not None:
            return tg_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Сессия Mini App не найдена. Откройте календарь из Telegram ещё раз.",
    )


class IntervalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    start_at: datetime
    end_at: datetime
    reminders: list[datetime] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def validate_times(self):
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        if self.end_at - self.start_at > timedelta(days=7):
            raise ValueError("Продолжительность не может превышать семь дней")
        return self


class IntervalUpdate(BaseModel):
    start_at: datetime
    end_at: datetime
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    reminders: list[datetime] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def validate_times(self):
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        if self.end_at - self.start_at > timedelta(days=7):
            raise ValueError("Продолжительность не может превышать семь дней")
        return self


async def current_user(db: AsyncSession, tg_id: int) -> User:
    result = await db.execute(select(User).filter(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


async def find_conflict(
    db: AsyncSession,
    user_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_event_id: Optional[int] = None,
):
    query = (
        select(Event, EventTiming)
        .join(EventTiming, EventTiming.event_id == Event.id)
        .filter(
            Event.user_id == user_id,
            Event.status == EventStatus.CONFIRMED,
            EventTiming.start_at < end_at,
            EventTiming.end_at > start_at,
        )
    )
    if exclude_event_id is not None:
        query = query.filter(Event.id != exclude_event_id)
    result = await db.execute(query)
    return result.first()


@router.get("/miniapp", response_class=HTMLResponse)
async def miniapp_entry(request: Request, destination: str = "calendar"):
    safe_destination = destination if destination in {"calendar", "dashboard"} else "calendar"
    return templates.TemplateResponse(
        request=request,
        name="miniapp.html",
        context={"request": request, "destination": safe_destination},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/miniapp")
async def miniapp_auth(payload: MiniAppAuthRequest, db: AsyncSession = Depends(get_db)):
    telegram_user = validate_telegram_init_data(
        payload.init_data, settings.TELEGRAM_BOT_TOKEN or ""
    )
    if telegram_user is None:
        raise HTTPException(status_code=401, detail="Не удалось подтвердить аккаунт Telegram")
    tg_id = telegram_user["id"]
    result = await db.execute(select(User).filter(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(tg_id=tg_id, timezone="Europe/Moscow")
        db.add(user)
        await db.commit()
        await db.refresh(user)
    await ensure_identity(db, "telegram", tg_id)
    await db.commit()

    target = "/mini-timeline?view=agenda" if payload.destination == "dashboard" else "/mini-timeline"
    session_cookie = sign_tg_id(tg_id, get_cookie_secret())
    response = JSONResponse({
        "redirect": target,
        "access_token": sign_miniapp_access_token(tg_id),
    })
    response.set_cookie(
        "planiruy_session",
        session_cookie,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=30 * 86400,
        path="/",
    )
    return response


@router.get("/mini-timeline", response_class=HTMLResponse)
async def mini_timeline(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="calendar_reliable.html",
        context={
            "request": request,
            "calendar_config": {
                "apiBase": "/api/v2/events",
                "reauthUrl": "/miniapp?destination=calendar",
                "tokenKey": "planiruy_access_token",
                "platform": "telegram",
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/v2/events")
async def get_interval_events(
    user_tg_id: int = Depends(get_miniapp_user_tg_id),
    db: AsyncSession = Depends(get_db),
):
    entries = await list_linked_events(db, "telegram", user_tg_id)
    return [calendar_payload(entry) for entry in entries]


@router.post("/api/v2/events", status_code=status.HTTP_201_CREATED)
async def create_interval_event(
    payload: IntervalCreate,
    user_tg_id: int = Depends(get_miniapp_user_tg_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await create_linked_event(
            db, "telegram", user_tg_id, payload.title,
            payload.description or "", payload.start_at, payload.end_at,
            payload.reminders,
        )
        return calendar_payload(entry)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch("/api/v2/events/{event_ref}")
async def update_interval_event(
    event_ref: str,
    payload: IntervalUpdate,
    user_tg_id: int = Depends(get_miniapp_user_tg_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await update_linked_event(
            db, "telegram", user_tg_id, event_ref,
            payload.start_at, payload.end_at, payload.title, payload.description,
            payload.reminders,
        )
        return calendar_payload(entry)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/v2/events/{event_id}/toggle-complete")
async def toggle_interval_event(
    event_id: str,
    user_tg_id: int = Depends(get_miniapp_user_tg_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await toggle_linked_event(db, "telegram", user_tg_id, event_id)
        return {"id": entry.ref, "is_completed": entry.event.is_completed}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/v2/events/{event_id}")
async def delete_interval_event(
    event_id: str,
    user_tg_id: int = Depends(get_miniapp_user_tg_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await delete_linked_event(db, "telegram", user_tg_id, event_id)
        return {"status": "deleted"}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
