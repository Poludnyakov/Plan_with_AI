"""Calendar import for the Mini App and the site; each router adds its own sign-in."""

import base64
import binascii
import logging

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from calendar_import_service import (
    MAX_ICS_BYTES,
    CalendarImportError,
    import_from_caldav,
    import_from_file,
    import_from_link,
    result_text,
)
from unified_calendar import ensure_platform_user


logger = logging.getLogger("CalendarImportApi")


class CalendarImportRequest(BaseModel):
    filename: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=MAX_ICS_BYTES)
    # The file as is (base64): a zip or a Windows-1251 file must not pass through text.
    content_base64: str | None = Field(default=None, max_length=MAX_ICS_BYTES * 3)
    url: str | None = Field(default=None, max_length=2048)
    server: str | None = Field(default=None, max_length=512)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)


async def run_import(
    db: AsyncSession, platform: str, external_id: int, kind: str, payload: CalendarImportRequest
) -> dict:
    try:
        if kind == "file" and (payload.content_base64 or payload.content):
            try:
                data = (
                    base64.b64decode(payload.content_base64, validate=True)
                    if payload.content_base64 else payload.content.encode("utf-8")
                )
            except (binascii.Error, ValueError) as error:
                raise CalendarImportError("Файл повреждён при загрузке. Попробуйте ещё раз.") from error
            if len(data) > MAX_ICS_BYTES:
                raise CalendarImportError("Файл слишком большой: максимум 5 МБ.")
            results = await import_from_file(db, platform, external_id, data, payload.filename or "Календарь")
        elif kind == "link" and payload.url:
            results = await import_from_link(db, platform, external_id, payload.url)
        elif kind == "caldav" and payload.server and payload.username and payload.password:
            results = await import_from_caldav(
                db, platform, external_id, payload.server, payload.username, payload.password
            )
        else:
            raise CalendarImportError("Заполните все поля.")
    except CalendarImportError as error:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        await db.rollback()
        logger.error("Calendar import failed: %s", error, exc_info=True)
        raise HTTPException(status_code=502, detail="Не удалось загрузить календарь. Попробуйте позже.") from error
    user = await ensure_platform_user(db, platform, external_id)
    return {
        "message": result_text(results, user.timezone, in_app=True),
        "created": sum(result.created for result in results),
        "imports": [result.import_id for result in results if result.import_id],
    }
