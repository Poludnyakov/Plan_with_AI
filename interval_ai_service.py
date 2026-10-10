import base64
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from openrouter_service import chat_completion, reasoning_setting
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
7. end_at обязан быть строго позже start_at. Ночной интервал («с 22 до 2»)
   заканчивается на следующий день. Но если окончание раньше начала и это не
   ночь («с 18 до 16»), не переноси его на другой день: верни время как написано
   (end_at раньше start_at) — приложение попросит пользователя проверить.
7а. Срок без времени («до пятницы сдать эссе», «к 20 октября отчёт») — событие
   на весь день В ДЕНЬ СРОКА (в пятницу, 20 октября), а не сегодня. «До конца
   недели» — ближайшее воскресенье, «до конца месяца» — последний день месяца.
8. Относительные даты вычисляй от переданных текущих даты и времени. День недели
   без даты бери из списка ближайших дней выше.
9. Пользователь находится в часовом поясе Europe/Moscow (+03:00).
10. Если последнее сообщение просит перенести или изменить ранее упомянутое событие, используй историю только для разрешения «его», «её», «это» и верни желаемую итоговую версию этого события. Не извлекай отдельные события из истории.
    Просьба перенести или изменить событие («перенеси спортзал на 15:30»)
    всегда даёт ровно одно событие: название из сообщения и новое время, даже
    если этого события нет в истории. Пустой список здесь недопустим.

Формат (ГГГГ-ММ-ДД — вычисленная тобой дата, а не дата из примера):
{"events":[{"title":"Контрольная","start_at":"ГГГГ-ММ-ДДT15:00:00+03:00","end_at":"ГГГГ-ММ-ДДT17:00:00+03:00","description":"","all_day":false}]}
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

_HOUR_WORDS = (
    r"(?:час|один|одного|два|двух|двум|три|тр[её]х|тр[её]м|четыре|четыр[её]х|четыр[её]м|"
    r"пять|пяти|шесть|шести|семь|семи|восемь|восьми|девять|девяти|десять|десяти|"
    r"одиннадцать|одиннадцати|двенадцать|двенадцати)"
)
_ORDINAL_HOURS = (
    r"(?:первого|второго|третьего|четв[её]ртого|пятого|шестого|седьмого|восьмого|"
    r"девятого|десятого|одиннадцатого|двенадцатого)"
)
# «на 11» is a time; «на 11 октября», «10.11» or «с 1 по 7» are dates.
_NOT_A_DAY = (
    r"(?![./]\d)(?!\s*(?:-?го\s+)?(?:январ|феврал|март|апрел|ма[йя]|июн|июл|"
    r"август|сентябр|октябр|ноябр|декабр|числ|по\s+\d))"
)
_PREPOSITION = r"\b(?:в|во|к|до|с|со|около|после|от|на)\s+"
# Any way of naming a clock time in Russian; without one the event is all-day.
_TIME_HINT_RE = re.compile("|".join((
    r"\d{1,2}:\d{2}",
    _PREPOSITION + r"\d{1,2}(?:[.-]\d{2})?\b" + _NOT_A_DAY,
    r"\b\d{1,2}\s*(?:ч|час|часа|часов)\b",
    r"\bв\s+обед\b",
    _PREPOSITION + _HOUR_WORDS + r"\b(?!\s+(?:лет|год|раз|дн|недел|месяц|человек))",
    r"\bпол[\s-]?" + _ORDINAL_HOURS + r"\b",
    r"\b(?:половин[аеуы]|четверть)\s+" + _ORDINAL_HOURS + r"\b",
    r"\bбез\s+(?:четверти|пяти|десяти|пятнадцати|двадцати|\d{1,2})\b",
    r"\b(?:утра|утром|дн[её]м|вечера|вечером|ночи|ночью|полдень|полудня|полудню|"
    r"полночь|полуночи|полуночью)\b",
    # «в 3 часа дня», but not «через два дня».
    r"(?:\bчас[аов]*|\b(?:в|к|до|с|со|около|после)\s+(?:\d{1,2}|" + _HOUR_WORDS + r"))\s+дня\b",
    r"\bчерез\s+(?:(?:\d+|один|одну|два|две|три|четыре|пять|шесть|десять|"
    r"пятнадцать|двадцать|тридцать|сорок|пару|полтора|несколько)\s+)?"
    r"(?:час|часа|часов|полчаса|минут[уы]?|мин)\b",
)), re.IGNORECASE)


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


WEEKDAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def upcoming_days_text(now: datetime) -> str:
    """Exact dates of the coming weekdays: models miscount «в субботу» otherwise."""
    lines = []
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        label = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(offset, "")
        if offset == 7:
            label = "через неделю"
        name = WEEKDAY_NAMES[day.weekday()]
        lines.append(f"{name} {day.isoformat()}" + (f" ({label})" if label else ""))
    return (
        "Ближайшие дни: " + "; ".join(lines) + ". «В субботу» — ближайшая суббота из "
        "списка; день, совпадающий с сегодняшним, — сегодня, а если названное время "
        "уже прошло — через неделю."
    )


_WEEKDAY_FORMS = (
    r"понедельник\w*|пн", r"вторник\w*|вт", r"сред[аеуы]|ср", r"четверг\w*|чт",
    r"пятниц[аеуы]|пт", r"суббот[аеуы]|сб", r"воскресень[еяю]|вс",
)


def roll_past_named_weekday(events: List[IntervalExtraction], source: Union[str, bytes], now: datetime) -> None:
    """«В пятницу с 10 до 12», said on Friday evening, is next Friday.

    A model sometimes picks today although that time has already passed.
    """
    if not isinstance(source, str):
        return
    text = source.casefold()
    names_today = re.search(r"\b(?:" + _WEEKDAY_FORMS[now.weekday()] + r")\b", text)
    if not names_today or re.search(r"\bсегодня\b|\d{1,2}[./]\d{1,2}|\d{1,2}\s+[а-я]+бря|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа)", text):
        return
    for event in events:
        local_start = event.start_at.astimezone(now.tzinfo)
        if local_start.date() == now.date() and event.end_at <= now:
            event.start_at += timedelta(days=7)
            event.end_at += timedelta(days=7)


async def extract_intervals(
    text_or_image: Union[str, bytes],
    context: list[str] | None = None,
) -> List[dict]:
    if not settings.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not configured.")

    now = datetime.now(ZoneInfo("Europe/Moscow"))
    system_prompt = (
        f"Сейчас {now.isoformat()}, день недели {now.strftime('%A')}.\n"
        + upcoming_days_text(now) + "\n"
        + INTERVAL_SYSTEM_PROMPT
    )
    if context and not isinstance(text_or_image, bytes):
        history = "\n".join(
            f"- {item[:1200]}" for item in context[-6:] if item.strip()
        )
        if history:
            system_prompt += (
                "\n\nКороткая история диалога (только для понимания ссылок на "
                "предыдущие события; не создавай по ней новые события):\n"
                + history
            )
    is_image = isinstance(text_or_image, bytes)

    if is_image:
        image_data = base64.b64encode(text_or_image).decode("ascii")
        model = settings.OPENROUTER_MODEL_FOR_OCR
        user_content = [
            {"type": "text", "text": "Извлеки мероприятия и интервалы времени из расписания."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
        ]
    else:
        model = settings.OPENROUTER_MODEL_FOR_TEXT
        user_content = text_or_image

    response = await chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        reasoning=reasoning_setting(settings.OPENROUTER_TEXT_REASONING_EFFORT),
    )

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
            if "end_at must be later" in str(error):
                raise ValueError(
                    "Окончание получилось раньше начала. Проверьте время, "
                    "например: «с 16 до 18»."
                ) from error
            raise ValueError(
                "Не получилось понять дату или время. Напишите их точнее, "
                "например: «завтра в 15:00»."
            ) from error

    mark_default_all_day(result.events, text_or_image)
    roll_past_named_weekday(result.events, text_or_image, now)
    return [event.model_dump() for event in result.events]
