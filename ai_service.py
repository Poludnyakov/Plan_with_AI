import base64
import os
import json
import logging
from typing import List, Union, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger("AIService")

# ==========================================
# SYSTEM PROMPT DEFINITION
# ==========================================
SYSTEM_PROMPT = """
Ты — интеллектуальный ассистент студента «планиИруй!».
Твоя задача — извлечь академические или личные события из предоставленного текста или расписания на изображении и представить их в виде строго структурированного JSON-объекта.

Всегда рассчитывай относительные даты (в пятницу, завтра, послезавтра, на следующей неделе), отталкиваясь строго от переданной Текущей даты на сервере. Если сегодня вторник 26 мая 2026 года, то ближайшая пятница — это 29 мая 2026 года. Ошибки в расчете дат недопустимы.

Каждое событие должно содержать следующие поля:
1. "title": Краткое название события (например, "Лекция по матанализу", "Сдача лабы по ИИ").
   - Распознавай и расшифровывай учебные сокращения в названии:
     - "сем" -> "Семинар"
     - "лек" -> "Лекция"
     - "лаб" -> "Лабораторная"
     - "пр" -> "Практика"
2. "deadline": Дата и время дедлайна/события в формате ISO 8601 (YYYY-MM-DDTHH:MM:SSZ).
   - Если указан только день недели или дата без конкретного времени (например, "в понедельник дедлайн"), выставляй время строго на 12:00:00 (в ISO-формате).
   - Приводи все даты к текущему 2026 году.
3. "description": Контекст события, номер аудитории, имя преподавателя, если они упоминаются. Если данных нет, оставляй пустую строку "".
4. "suggested_reminders": Список дат и времени для превентивных напоминаний в формате ISO 8601 (YYYY-MM-DDTHH:MM:SSZ).
   - Для каждого извлеченного мероприятия модель обязана вычислить время дедлайна и сгенерировать ровно ПЯТЬ таймстампов напоминаний:
     1. Ровно за 24 часа (1 сутки) до дедлайна.
     2. Ровно за 12 часов до дедлайна.
     3. Ровно за 1 час (60 минут) до дедлайна.
     4. Ровно за 30 минут до дедлайна.
     5. Ровно за 15 минут до дедлайна.
   - Пример для промпта:
     - Запрос: "В пятницу 29 мая в 15:00 дедлайн по лабе".
       Ответ: deadline="2026-05-29T15:00:00Z", suggested_reminders=["2026-05-28T15:00:00Z", "2026-05-29T03:00:00Z", "2026-05-29T14:00:00Z", "2026-05-29T14:30:00Z", "2026-05-29T14:45:00Z"]
   - Напоминания должны хронологически предшествовать дедлайну.

JSON-схема ответа должна выглядеть строго так:
{
  "events": [
    {
      "title": "строка",
      "deadline": "строка (ISO 8601)",
      "description": "строка",
      "suggested_reminders": ["строка (ISO 8601)"]
    }
  ]
}

Выведи ТОЛЬКО корректный JSON-объект. Не добавляй никаких рассуждений, разметки markdown (типа ```json) или стороннего текста.
"""

# ==========================================
# PYDANTIC VALIDATION MODELS
# ==========================================
class EventExtraction(BaseModel):
    title: str = Field(
        ..., 
        description="Краткое название (например, 'Лекция по матанализу', 'Сдача лабы по ИИ')"
    )
    deadline: str = Field(
        ..., 
        description="Дата и время в формате ISO 8601 (YYYY-MM-DDTHH:MM:SSZ)"
    )
    description: str = Field(
        ..., 
        description="Контекст, номер аудитории, имя преподавателя, если они есть. Пустая строка, если отсутствует."
    )
    suggested_reminders: List[str] = Field(
        ..., 
        description="Список дат/времени для превентивных напоминаний в формате ISO"
    )

class EventListExtraction(BaseModel):
    events: List[EventExtraction] = Field(
        ..., 
        description="Список извлеченных событий"
    )


# ==========================================
# ASYNCHRONOUS AI EXTRACTION FUNCTION
# ==========================================
async def extract_schedule_with_yandex(text_or_image_prompt: Union[str, bytes]) -> List[dict]:
    """
    Asynchronously extracts student schedule events from a text prompt or an image
    using Yandex AI Studio models (YandexGPT 5.1 Pro for text, Qwen 3.6-35B for image schedules).
    
    :param text_or_image_prompt: Raw text query, path to image file, base64 image string, or raw image bytes.
    :return: List of dictionaries conforming to the EventExtraction Pydantic model.
    """
    if not settings.API_KEY:
        raise ValueError("Yandex API_KEY is not configured in settings.")
    if not settings.FOLDER_ID:
        raise ValueError("Yandex FOLDER_ID is not configured in settings.")

    # Dynamic current date and day of week
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_day_of_week = days_ru[now.weekday()]
    
    dynamic_system_prompt = (
        f"Текущая дата на сервере: {current_date} (день недели: {current_day_of_week}). Текущий год — 2026.\n"
        + SYSTEM_PROMPT
    )

    # Detect input type (text vs. image)
    is_image = False
    image_base64 = ""

    if isinstance(text_or_image_prompt, bytes):
        is_image = True
        image_base64 = base64.b64encode(text_or_image_prompt).decode("utf-8")
    elif isinstance(text_or_image_prompt, str):
        # Check if the string points to an existing file (e.g. local image path)
        if os.path.exists(text_or_image_prompt) and os.path.isfile(text_or_image_prompt):
            is_image = True
            with open(text_or_image_prompt, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode("utf-8")
        # Check if the string is a base64 data URI or raw base64
        elif text_or_image_prompt.startswith("data:image/") or ";base64," in text_or_image_prompt:
            is_image = True
            if "," in text_or_image_prompt:
                image_base64 = text_or_image_prompt.split(",", 1)[1]
            else:
                image_base64 = text_or_image_prompt
        elif len(text_or_image_prompt) > 1000 and text_or_image_prompt.isalnum():
            # Likely raw base64 string
            is_image = True
            image_base64 = text_or_image_prompt

    # Initialize OpenAI-compatible Async client for Yandex Cloud
    client_base_url = "https://ai.api.cloud.yandex.net/v1" if is_image else settings.YANDEX_BASE_URL
    client = AsyncOpenAI(
        base_url=client_base_url,
        api_key=settings.API_KEY,
        default_headers={
            "Authorization": f"Api-Key {settings.API_KEY}",
            "x-folder-id": settings.FOLDER_ID
        }
    )

    # Define model URIs as per Yandex Cloud convention
    if is_image:
        # Use user-specified Qwen model from Yandex AI Studio for image schedules
        model_name = settings.YANDEX_CLOUD_MODEL
        if not model_name.startswith("gpt://"):
            model = f"gpt://{settings.FOLDER_ID}/{model_name}"
        else:
            model = model_name
        logger.info(f"Extracting schedule from image using Yandex AI Studio model: {model}")
        messages = [
            {
                "role": "system",
                "content": dynamic_system_prompt
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Распознай расписание и дедлайны с этого изображения и преобразуй в структурированный JSON."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    }
                ]
            }
        ]
    else:
        # Use YandexGPT 5.1 Pro for text
        model = f"gpt://{settings.FOLDER_ID}/yandexgpt/latest"
        logger.info(f"Extracting schedule from text using model: {model}")
        messages = [
            {
                "role": "system",
                "content": dynamic_system_prompt
            },
            {
                "role": "user",
                "content": text_or_image_prompt
            }
        ]

    # Call completions API forcing structured JSON format output
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1
        )
    finally:
        await client.close()

    response_text = response.choices[0].message.content
    logger.info(f"Received response from Yandex AI Studio: {response_text}")

    # Parse and validate the strict JSON output through our Pydantic schema
    try:
        validated_data = EventListExtraction.model_validate_json(response_text)
    except Exception as e:
        logger.error(f"Failed to validate Yandex AI Studio output against JSON schema. Error: {e}. Output: {response_text}")
        
        # Try a robust fallback if there is surrounding markdown formatting block
        try:
            clean_text = response_text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean_text = "\n".join(lines).strip()
            validated_data = EventListExtraction.model_validate_json(clean_text)
        except Exception as fallback_err:
            raise ValueError(f"Yandex AI response is not a valid JSON conforming to the schema: {fallback_err}. Raw text: {response_text}")

    # Return list of dictionaries as required
    return [event.model_dump() for event in validated_data.events]


class YandexGPTService:
    """
    YandexGPTService is a service layer wrapper for the OpenAI-compatible
    Yandex AI Studio completions structured output extractor.
    """
    async def extract_schedule(self, text_or_image_prompt: Union[str, bytes]) -> List[dict]:
        return await extract_schedule_with_yandex(text_or_image_prompt)
