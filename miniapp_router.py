import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from dashboard_router import get_cookie_secret, sign_tg_id
from database import get_db
from models import User
from telegram_miniapp_auth import validate_telegram_init_data


logger = logging.getLogger("MiniAppRouter")
router = APIRouter(tags=["Telegram Mini App"])
templates = Jinja2Templates(directory="templates")


class MiniAppAuthRequest(BaseModel):
    init_data: str
    destination: Literal["calendar", "dashboard"] = "calendar"


@router.get("/miniapp", response_class=HTMLResponse)
async def miniapp_entry(request: Request, destination: str = "calendar"):
    safe_destination = destination if destination in {"calendar", "dashboard"} else "calendar"
    return templates.TemplateResponse(
        request=request,
        name="miniapp.html",
        context={"request": request, "destination": safe_destination},
    )


@router.post("/api/auth/miniapp")
async def miniapp_auth(
    payload: MiniAppAuthRequest,
    db: AsyncSession = Depends(get_db),
):
    telegram_user = validate_telegram_init_data(
        payload.init_data,
        settings.TELEGRAM_BOT_TOKEN or "",
    )
    if telegram_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось подтвердить аккаунт Telegram. Откройте Mini App заново.",
        )

    tg_id = telegram_user["id"]
    result = await db.execute(select(User).filter(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(tg_id=tg_id, timezone="Europe/Moscow")
        db.add(user)
        await db.commit()
        await db.refresh(user)

    redirect_target = "/dashboard" if payload.destination == "dashboard" else "/calendar"
    response = JSONResponse({"redirect": redirect_target})
    response.set_cookie(
        key="planiruy_session",
        value=sign_tg_id(tg_id, get_cookie_secret()),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=30 * 86400,
    )
    logger.info("Telegram Mini App session created for tg_id=%s", tg_id)
    return response
