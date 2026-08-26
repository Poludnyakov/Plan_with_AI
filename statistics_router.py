from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from statistics_service import product_statistics


router = APIRouter(tags=["Statistics"])
templates = Jinja2Templates(directory="templates")


@router.get("/_statistics", response_class=HTMLResponse, include_in_schema=False)
async def statistics_page(request: Request, db: AsyncSession = Depends(get_db)):
    statistics = await product_statistics(db)
    return templates.TemplateResponse(
        request=request,
        name="statistics.html",
        context={"request": request, "statistics": statistics},
        headers={"Cache-Control": "no-store"},
    )
