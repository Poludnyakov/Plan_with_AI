import base64
import json
import logging
from datetime import time
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from config import settings


logger = logging.getLogger("ScheduleAIService")

SCHEDULE_PROMPT = """
Ты анализируешь фотографию. Определи, является ли она именно повторяющимся
недельным расписанием занятий (таблица с днями недели и временем), а не афишей,
одиночным событием, списком дедлайнов или обычным текстом.

Верни только JSON:
{
  "is_recurring_schedule": true,
  "confidence": 0.95,
  "slots": [
    {
      "title": "Математика",
      "weekday": 0,
      "start_time": "09:00",
      "end_time": "10:30",
      "description": "ауд. 201, преподаватель",
      "week_pattern": "every",
      "confidence": 0.94
    }
  ]
}

weekday: понедельник=0, ..., воскресенье=6.
week_pattern: every, odd или even. Если чётность не указана — every.
Не выдумывай неразборчивые названия и время. Если это не недельное расписание
или структура ненадёжна, верни is_recurring_schedule=false и пустой slots.
Дата начала и конца семестра здесь не нужна: пользователь введёт её отдельно.
"""


class ScheduleSlotExtraction(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    description: str = Field(default="", max_length=1000)
    week_pattern: Literal["every", "odd", "even"] = "every"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def normalize_clock(cls, value):
        if isinstance(value, str) and len(value.strip()) == 5:
            return value.strip() + ":00"
        return value

    @model_validator(mode="after")
    def validate_times(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class ScheduleImageExtraction(BaseModel):
    is_recurring_schedule: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    slots: list[ScheduleSlotExtraction] = Field(default_factory=list, max_length=100)


def _clean_json(text: str):
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1]).strip()
        if clean.startswith("json"):
            clean = clean[4:].lstrip()
    return json.loads(clean)


def _normalize_keys(value):
    if isinstance(value, list):
        return [_normalize_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        (key.strip().lstrip("_") if isinstance(key, str) else key): _normalize_keys(item)
        for key, item in value.items()
    }
    expected = {"is_recurring_schedule", "confidence", "slots"}
    if not expected.intersection(normalized):
        for item in normalized.values():
            if isinstance(item, dict) and expected.intersection(item):
                return item
    return normalized


async def extract_weekly_schedule(image_bytes: bytes) -> dict | None:
    """Return a strict weekly schedule or None so callers can use legacy OCR."""
    if not image_bytes or not settings.API_KEY or not settings.FOLDER_ID:
        return None
    client = AsyncOpenAI(
        base_url="https://ai.api.cloud.yandex.net/v1",
        api_key=settings.API_KEY,
        default_headers={
            "Authorization": f"Api-Key {settings.API_KEY}",
            "x-folder-id": settings.FOLDER_ID,
        },
    )
    model_name = settings.YANDEX_CLOUD_MODEL
    model = model_name if model_name.startswith("gpt://") else f"gpt://{settings.FOLDER_ID}/{model_name}"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SCHEDULE_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Проверь и распознай недельное расписание."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                ]},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content or ""
        parsed = ScheduleImageExtraction.model_validate(_normalize_keys(_clean_json(raw)))
    except Exception as error:
        logger.warning("Weekly schedule detection failed; using legacy image path: %s", error)
        return None
    finally:
        await client.close()

    reliable = [slot for slot in parsed.slots if slot.confidence >= 0.60]
    enough_structure = len(reliable) >= 2 and len({slot.weekday for slot in reliable}) >= 2
    if not parsed.is_recurring_schedule or parsed.confidence < 0.80 or not enough_structure:
        return None
    return {
        "confidence": parsed.confidence,
        "slots": [
            {
                **slot.model_dump(mode="json"),
                "start_time": slot.start_time.strftime("%H:%M"),
                "end_time": slot.end_time.strftime("%H:%M"),
            }
            for slot in reliable
        ],
    }
