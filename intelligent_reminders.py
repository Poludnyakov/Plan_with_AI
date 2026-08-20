import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from config import settings
REMINDER_OFFSETS = (
    timedelta(hours=24), timedelta(hours=12), timedelta(hours=1),
    timedelta(minutes=30), timedelta(minutes=15),
)


logger = logging.getLogger("IntelligentReminders")

SMART_REMINDER_PROMPT = """
Ты — интеллектуальный помощник по напоминаниям «планиИруй!». Выбери от одного
до четырёх действительно полезных моментов для напоминания о мероприятии.
Верни только JSON: {"reminders":["ISO 8601 с часовым поясом"]}.

Принципы:
1. Не создавай бессмысленно частые уведомления. Каждое должно давать время
   подготовиться или не забыть выйти/подключиться.
2. Учитывай тип события, начало, окончание, время суток и описание.
3. Для утренней встречи обычно полезны вечер накануне (примерно 19:00–21:00)
   и напоминание за час утром.
4. Для дневной встречи — утром того же дня и за 60 минут. Для поездки или
   очной встречи оставь запас на дорогу, если место известно.
5. Для домашней работы, проекта или дедлайна планируй время на выполнение:
   например, при дедлайне в 12:00 напомни около 09:00 и, если задача объёмная,
   ещё вечером накануне.
6. Для контрольной или экзамена напомни заранее для подготовки (1–3 дня),
   вечером накануне и утром, но не превращай это в спам.
7. Для короткой бытовой задачи достаточно одного напоминания за 30–60 минут.
8. Не ставь напоминания в прошлом или после начала мероприятия. По возможности
   избегай периода 23:00–07:00 по местному времени.
9. Текущее время, часовой пояс и точные границы события переданы пользователем.
"""


class ReminderResponse(BaseModel):
    reminders: list[datetime] = Field(default_factory=list, max_length=4)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _clean_payload(value):
    if isinstance(value, list):
        return [_clean_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {str(key).strip().lstrip("_"): _clean_payload(item) for key, item in value.items()}
    if "reminders" in cleaned:
        return {"reminders": cleaned["reminders"]}
    for item in cleaned.values():
        if isinstance(item, dict) and "reminders" in item:
            return {"reminders": item["reminders"]}
    return cleaned


def valid_times(
    values: Iterable[datetime], start_at: datetime, now: datetime | None = None
) -> list[datetime]:
    now = _aware(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = _aware(start_at).astimezone(timezone.utc)
    unique = {
        _aware(value).astimezone(timezone.utc).replace(second=0, microsecond=0)
        for value in values
        if now < _aware(value).astimezone(timezone.utc) < start
    }
    return sorted(unique)[:4]


def contextual_fallback(
    title: str, start_at: datetime, timezone_name: str = "Europe/Moscow",
    now: datetime | None = None,
) -> list[datetime]:
    start = _aware(start_at)
    zone = ZoneInfo(timezone_name)
    local = start.astimezone(zone)
    lowered = (title or "").casefold().replace("ё", "е")
    suggestions: list[datetime] = []
    if any(word in lowered for word in ("экзамен", "контрольн", "зачет", "защита")):
        suggestions.extend([start - timedelta(days=2), start - timedelta(days=1)])
        suggestions.append(local.replace(hour=20, minute=0) - timedelta(days=1))
        suggestions.append(start - timedelta(hours=1))
    elif any(word in lowered for word in ("домаш", "дз", "дедлайн", "сдать", "проект")):
        suggestions.extend([
            local.replace(hour=20, minute=0) - timedelta(days=1),
            start - timedelta(hours=3),
        ])
    elif local.hour <= 10:
        suggestions.extend([
            local.replace(hour=20, minute=0) - timedelta(days=1),
            start - timedelta(hours=1),
        ])
    else:
        suggestions.extend([start - timedelta(hours=6), start - timedelta(hours=1)])
    return valid_times(suggestions, start, now)


def standard_reminders(
    start_at: datetime, now: datetime | None = None
) -> list[datetime]:
    start = _aware(start_at)
    return valid_times((start - offset for offset in REMINDER_OFFSETS), start, now)


async def recommend_reminders(
    title: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    timezone_name: str = "Europe/Moscow",
    now: datetime | None = None,
) -> list[datetime]:
    current = _aware(now or datetime.now(timezone.utc))
    fallback = contextual_fallback(title, start_at, timezone_name, current)
    if not settings.API_KEY or not settings.FOLDER_ID:
        return fallback
    client = AsyncOpenAI(
        base_url=settings.YANDEX_BASE_URL,
        api_key=settings.API_KEY,
        default_headers={
            "Authorization": f"Api-Key {settings.API_KEY}",
            "x-folder-id": settings.FOLDER_ID,
        },
    )
    user_prompt = (
        f"Сейчас: {current.isoformat()}\nЧасовой пояс: {timezone_name}\n"
        f"Событие: {title}\nОписание: {description or 'нет'}\n"
        f"Начало: {_aware(start_at).isoformat()}\nОкончание: {_aware(end_at).isoformat()}"
    )
    model_name = settings.YANDEX_CLOUD_MODEL
    model = (
        model_name
        if model_name.startswith("gpt://")
        else f"gpt://{settings.FOLDER_ID}/{model_name}"
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SMART_REMINDER_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:-1]).removeprefix("json").strip()
        result = ReminderResponse.model_validate(_clean_payload(json.loads(text)))
        return valid_times(result.reminders, start_at, current) or fallback
    except Exception as error:
        logger.warning("AI reminder recommendation failed: %s", error, exc_info=True)
        return fallback
    finally:
        await client.close()


async def build_reminders(
    smart_enabled: bool,
    title: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    timezone_name: str = "Europe/Moscow",
    now: datetime | None = None,
) -> list[datetime]:
    if smart_enabled:
        return await recommend_reminders(
            title, description, start_at, end_at, timezone_name, now
        )
    return standard_reminders(start_at, now)
