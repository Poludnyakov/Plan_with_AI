import base64
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, model_validator

from config import settings


logger = logging.getLogger("IntervalAIService")


INTERVAL_SYSTEM_PROMPT = """
Ты — календарный ассистент «планиИруй!». Извлеки из сообщения все реальные
мероприятия и верни только JSON без Markdown.

Для каждого мероприятия верни:
- title: короткое понятное название;
- start_at: точное время начала ISO 8601 с часовым поясом;
- end_at: точное время окончания ISO 8601 с часовым поясом;
- description: место и детали или пустая строка.
- all_day: true, если пользователь не указал время; иначе false.

Правила времени:
1. Фраза «с 15 до 17», «15:00–17:00» означает начало в 15:00 и окончание в 17:00.
2. Если дано начало, но не дано окончание, используй продолжительность 60 минут.
3. Для лекции/семинара/контрольной без окончания используй 90 минут.
4. Для формулировки «сдать до 18:00» создай 30-минутный блок, заканчивающийся в 18:00.
5. Если пользователь назвал только дату или период дат, это событие НА ВЕСЬ ДЕНЬ: all_day=true, start_at — 00:00 первого дня, end_at — 00:00 дня после последнего. Не придумывай время 12:00. «Завтра собрать чемоданы» — all_day=true.
6. Диапазон «с 13 по 15 августа» для all_day включает оба дня, то есть end_at должен быть 16 августа 00:00.
7. end_at обязан быть строго позже start_at.
8. Относительные даты вычисляй от переданных текущих даты и времени.
9. Пользователь находится в часовом поясе Europe/Moscow (+03:00).

Формат:
{"events":[{"title":"Контрольная","start_at":"2026-08-20T15:00:00+03:00","end_at":"2026-08-20T17:00:00+03:00","description":"","all_day":false}]}
"""


INTERVAL_JSON_FIELDS = {"events", "title", "start_at", "end_at", "description", "all_day"}


def normalize_interval_payload(value):
    """Normalize harmless key variations sometimes produced by an LLM."""
    if isinstance(value, list):
        return [normalize_interval_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {}
    for key, item in value.items():
        normalized_key = key
        if isinstance(key, str):
            candidate = key.strip().lstrip("_")
            if candidate in INTERVAL_JSON_FIELDS:
                normalized_key = candidate
        normalized[normalized_key] = normalize_interval_payload(item)

    if "events" not in normalized:
        for item in normalized.values():
            if isinstance(item, dict) and isinstance(item.get("events"), list):
                return {"events": item["events"]}
    return normalized


class IntervalExtraction(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    start_at: datetime
    end_at: datetime
    description: str = ""
    all_day: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_date_only_interval(cls, value):
        if not isinstance(value, dict):
            return value
        start_raw, end_raw = value.get("start_at"), value.get("end_at")
        is_date_only = (
            isinstance(start_raw, str) and len(start_raw) == 10
            and isinstance(end_raw, str) and len(end_raw) == 10
        )
        if not is_date_only:
            return value
        try:
            start_date = date.fromisoformat(str(start_raw)[:10])
            end_date = date.fromisoformat(str(end_raw)[:10])
        except (TypeError, ValueError):
            return value
        # Date-only end dates from an LLM are conventionally inclusive.
        normalized = dict(value)
        normalized["all_day"] = True
        normalized["start_at"] = f"{start_date.isoformat()}T00:00:00+03:00"
        normalized["end_at"] = f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00+03:00"
        return normalized

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")
        max_duration = timedelta(days=366 if self.all_day else 7)
        if self.end_at - self.start_at > max_duration:
            raise ValueError("event duration is too long")
        return self


class IntervalListExtraction(BaseModel):
    events: List[IntervalExtraction]

_TIME_HINT_RE = re.compile(
    r"(?:\b(?:в|к|до)\s+\d{1,2}(?::\d{2})?\b|\b\d{1,2}:\d{2}\b|\b(?:утром|дн[её]м|вечером|ночью)\b)",
    re.IGNORECASE,
)


def mark_default_all_day(events: List[IntervalExtraction], source: Union[str, bytes]) -> None:
    """Enforce the product rule when an LLM forgets its all_day flag."""
    if not isinstance(source, str) or _TIME_HINT_RE.search(source):
        return
    zone = ZoneInfo("Europe/Moscow")
    for event in events:
        if event.all_day:
            continue
        local_start = event.start_at.astimezone(zone)
        local_end = event.end_at.astimezone(zone)
        event.all_day = True
        event.start_at = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = local_end.date()
        if any((local_end.hour, local_end.minute, local_end.second, local_end.microsecond)):
            end_date += timedelta(days=1)
        if end_date <= event.start_at.date():
            end_date = event.start_at.date() + timedelta(days=1)
        event.end_at = event.start_at.replace(year=end_date.year, month=end_date.month, day=end_date.day)


async def extract_intervals(text_or_image: Union[str, bytes]) -> List[dict]:
    if not settings.API_KEY:
        raise ValueError("Yandex API_KEY is not configured.")
    if not settings.FOLDER_ID:
        raise ValueError("Yandex FOLDER_ID is not configured.")

    now = datetime.now(ZoneInfo("Europe/Moscow"))
    system_prompt = (
        f"Сейчас {now.isoformat()}, день недели {now.strftime('%A')}.\n"
        + INTERVAL_SYSTEM_PROMPT
    )
    is_image = isinstance(text_or_image, bytes)
    base_url = "https://ai.api.cloud.yandex.net/v1" if is_image else settings.YANDEX_BASE_URL
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=settings.API_KEY,
        default_headers={
            "Authorization": f"Api-Key {settings.API_KEY}",
            "x-folder-id": settings.FOLDER_ID,
        },
    )

    if is_image:
        image_data = base64.b64encode(text_or_image).decode("ascii")
        model_name = settings.YANDEX_CLOUD_MODEL
        model = model_name if model_name.startswith("gpt://") else f"gpt://{settings.FOLDER_ID}/{model_name}"
        user_content = [
            {"type": "text", "text": "Извлеки мероприятия и интервалы времени из расписания."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
        ]
    else:
        model = f"gpt://{settings.FOLDER_ID}/yandexgpt/latest"
        user_content = text_or_image

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    finally:
        await client.close()

    response_text = response.choices[0].message.content or ""
    try:
        payload = normalize_interval_payload(json.loads(response_text))
        if isinstance(payload, list):
            payload = {"events": payload}
        result = IntervalListExtraction.model_validate(payload)
    except Exception:
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.splitlines()[1:-1]).strip()
            if clean.startswith("json"):
                clean = clean[4:].lstrip()
        try:
            payload = normalize_interval_payload(json.loads(clean))
            if isinstance(payload, list):
                payload = {"events": payload}
            result = IntervalListExtraction.model_validate(payload)
        except Exception as error:
            logger.error("Invalid interval AI response: %s", response_text)
            raise ValueError("Нейросеть вернула некорректное время мероприятия.") from error

    mark_default_all_day(result.events, text_or_image)
    return [event.model_dump() for event in result.events]
