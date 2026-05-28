import pytest
from unittest.mock import AsyncMock, patch
import json
import httpx
from ai_service import extract_schedule_with_yandex, EventExtraction, EventListExtraction
from config import settings

@pytest.mark.anyio
async def test_extract_schedule_text_success():
    """
    Tests that extract_schedule_with_yandex correctly parses text inputs,
    invokes the correct model URI, and returns structured event dictionaries.
    """
    mock_json_response = {
        "events": [
            {
                "title": "Лекция по матанализу",
                "deadline": "2026-06-01T12:00:00Z",
                "description": "Аудитория 405, преп. Иванов",
                "suggested_reminders": [
                    "2026-05-31T12:00:00Z",
                    "2026-06-01T00:00:00Z",
                    "2026-06-01T11:00:00Z",
                    "2026-06-01T11:30:00Z",
                    "2026-06-01T11:45:00Z"
                ]
            }
        ]
    }
    
    # Mocking openai client completions.create
    mock_choice = AsyncMock()
    mock_choice.message.content = json.dumps(mock_json_response)
    
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    
    with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_response
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            events = await extract_schedule_with_yandex("завтра лекция по матанализу у Иванова")
            
            assert len(events) == 1
            assert events[0]["title"] == "Лекция по матанализу"
            assert events[0]["deadline"] == "2026-06-01T12:00:00Z"
            assert events[0]["description"] == "Аудитория 405, преп. Иванов"
            assert events[0]["suggested_reminders"] == [
                "2026-05-31T12:00:00Z",
                "2026-06-01T00:00:00Z",
                "2026-06-01T11:00:00Z",
                "2026-06-01T11:30:00Z",
                "2026-06-01T11:45:00Z"
            ]
            
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            assert kwargs["model"] == "gpt://dummy_folder/yandexgpt/latest"
            assert kwargs["response_format"] == {"type": "json_object"}
 
 
@pytest.mark.anyio
async def test_extract_schedule_image_success():
    """
    Tests that extract_schedule_with_yandex correctly detects image input (bytes),
    base64 encodes it, and invokes the vision model (Qwen 3.6-35B).
    """
    mock_json_response = {
        "events": [
            {
                "title": "Сдача лабораторной по ИИ",
                "deadline": "2026-06-05T12:00:00Z",
                "description": "Сдать отчет в Личный кабинет",
                "suggested_reminders": [
                    "2026-06-04T12:00:00Z",
                    "2026-06-05T00:00:00Z",
                    "2026-06-05T11:00:00Z",
                    "2026-06-05T11:30:00Z",
                    "2026-06-05T11:45:00Z"
                ]
            }
        ]
    }
    
    mock_choice = AsyncMock()
    mock_choice.message.content = json.dumps(mock_json_response)
    
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    
    with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_response
        
        fake_image_bytes = b"fake_png_data_12345678"
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            events = await extract_schedule_with_yandex(fake_image_bytes)
            
            assert len(events) == 1
            assert events[0]["title"] == "Сдача лабораторной по ИИ"
            assert events[0]["deadline"] == "2026-06-05T12:00:00Z"
            
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            assert kwargs["model"] == "gpt://dummy_folder/qwen3.6-35b-a3b/latest"
            
            # Verify the structure of the message payload for image inputs
            messages = kwargs["messages"]
            assert messages[1]["role"] == "user"
            assert isinstance(messages[1]["content"], list)
            assert messages[1]["content"][1]["type"] == "image_url"
            assert "data:image/jpeg;base64," in messages[1]["content"][1]["image_url"]["url"]


@pytest.mark.anyio
async def test_extract_schedule_invalid_json():
    """
    Tests that extract_schedule_with_yandex raises ValueError
    when Yandex AI Studio outputs malformed JSON that fails validation.
    """
    mock_choice = AsyncMock()
    mock_choice.message.content = "Malformed JSON: { events = [ ... } "
    
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    
    with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_response
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            with pytest.raises(ValueError) as excinfo:
                await extract_schedule_with_yandex("some prompt")
            assert "not a valid JSON conforming to the schema" in str(excinfo.value)


@pytest.mark.anyio
async def test_real_yandex_ai_connection():
    """
    Real integration test checking the connection and credentials for Yandex AI Studio completions.
    Runs only if API_KEY and FOLDER_ID are present in settings.
    """
    if not settings.API_KEY or not settings.FOLDER_ID:
        pytest.skip("Yandex credentials are not configured in .env. Skipping real connection test.")
        
    from openai import AsyncOpenAI
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        base_url=settings.YANDEX_BASE_URL,
        api_key=settings.API_KEY,
        default_headers={
            "Authorization": f"Api-Key {settings.API_KEY}",
            "x-folder-id": settings.FOLDER_ID
        }
    )
    
    # We attempt a tiny chat completion call to verify connectivity and credentials.
    # We expect a successful response, validating that base_url and API_KEY/FOLDER_ID authenticate perfectly.
    try:
        async with client:
            response = await client.chat.completions.create(
                model=f"gpt://{settings.FOLDER_ID}/yandexgpt/latest",
                messages=[
                    {"role": "user", "content": "Привет, подтверди что связь работает словом OK."}
                ],
                max_tokens=10
            )
            answer = response.choices[0].message.content.strip()
            print(f"\n[YANDEX AI INTEGRATION] Connection successful! Yandex GPT response: {answer}")
            assert len(answer) > 0
    except Exception as e:
        pytest.fail(f"Connection to Yandex AI Studio failed completely with error: {e}")


@pytest.mark.anyio
async def test_extract_schedule_system_prompt_dynamic_date():
    """
    Tests that the system prompt dynamically receives the current server date and day of week
    from datetime.now(), starts with the required header prefix, and contains the strict relative date rule.
    """
    mock_json_response = {
        "events": []
    }
    
    mock_choice = AsyncMock()
    mock_choice.message.content = json.dumps(mock_json_response)
    
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    
    from datetime import datetime
    fixed_now = datetime(2026, 5, 26, 15, 30, 0)  # 2026-05-26 is Tuesday (вторник)
    
    with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create, \
         patch("ai_service.datetime") as mock_datetime:
         
        mock_create.return_value = mock_response
        mock_datetime.now.return_value = fixed_now
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            await extract_schedule_with_yandex("завтра лекция")
            
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            
            messages = kwargs["messages"]
            system_message = messages[0]
            
            assert system_message["role"] == "system"
            content = system_message["content"]
            
            # Verify prefix format
            expected_prefix = "Текущая дата на сервере: 2026-05-26 (день недели: вторник). Текущий год — 2026."
            assert content.startswith(expected_prefix)
            
            # Verify strict relative date rule
            expected_rule = (
                "Всегда рассчитывай относительные даты (в пятницу, завтра, послезавтра, на следующей неделе), "
                "отталкиваясь строго от переданной Текущей даты на сервере. Если сегодня вторник 26 мая 2026 года, "
                "то ближайшая пятница — это 29 мая 2026 года. Ошибки в расчете дат недопустимы."
            )
            assert expected_rule in content
            
            # Verify strict reminders math rules and examples
            assert "вычислить время дедлайна и сгенерировать ровно ПЯТЬ таймстампов" in content
            assert "Ровно за 24 часа (1 сутки) до дедлайна" in content
            assert "Ровно за 12 часов до дедлайна" in content
            assert "Ровно за 1 час (60 минут) до дедлайна" in content
            assert "Ровно за 30 минут до дедлайна" in content
            assert "Ровно за 15 минут до дедлайна" in content
            assert "В пятницу 29 мая в 15:00 дедлайн по лабе" in content
            assert 'suggested_reminders=["2026-05-28T15:00:00Z", "2026-05-29T03:00:00Z", "2026-05-29T14:00:00Z", "2026-05-29T14:30:00Z", "2026-05-29T14:45:00Z"]' in content


@pytest.mark.anyio
async def test_extract_schedule_image_custom_model():
    """
    Tests that extract_schedule_with_yandex correctly handles a custom YANDEX_CLOUD_MODEL configuration,
    ensuring that it prepends gpt:// folder prefix only if not already present.
    """
    mock_json_response = {"events": []}
    mock_choice = AsyncMock()
    mock_choice.message.content = json.dumps(mock_json_response)
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    
    with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_response
        fake_image_bytes = b"fake_image_bytes"
        
        # Scenario 1: Model name is a simple name (should prepend gpt://folder/)
        with patch.object(settings, "API_KEY", "dummy_key"), \
             patch.object(settings, "FOLDER_ID", "dummy_folder"), \
             patch.object(settings, "YANDEX_CLOUD_MODEL", "custom-model/latest"):
            await extract_schedule_with_yandex(fake_image_bytes)
            assert mock_create.call_args[1]["model"] == "gpt://dummy_folder/custom-model/latest"
            
        # Scenario 2: Model name already starts with gpt://
        mock_create.reset_mock()
        with patch.object(settings, "API_KEY", "dummy_key"), \
             patch.object(settings, "FOLDER_ID", "dummy_folder"), \
             patch.object(settings, "YANDEX_CLOUD_MODEL", "gpt://another_folder/another_model/latest"):
            await extract_schedule_with_yandex(fake_image_bytes)
            assert mock_create.call_args[1]["model"] == "gpt://another_folder/another_model/latest"

