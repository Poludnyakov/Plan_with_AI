from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from config import settings
from openrouter_service import _api_root, chat_completion, transcribe_audio


def test_api_root_removes_chat_completions_suffix():
    assert _api_root("https://openrouter.ai/api/v1/chat/completions") == "https://openrouter.ai/api/v1"
    assert _api_root("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"


@pytest.mark.anyio
async def test_chat_completion_forwards_document_reasoning_budget_and_closes_client():
    create = AsyncMock(return_value="response")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"):
        result = await chat_completion(model="model", messages=[], max_tokens=8000, reasoning={"effort": "low", "exclude": True})
    assert result == "response"
    assert create.await_args.kwargs["extra_body"] == {"reasoning": {"effort": "low", "exclude": True}}
    assert create.await_args.kwargs["max_tokens"] == 8000
    client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_chat_completion_retries_a_dropped_connection_once(monkeypatch):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    create = AsyncMock(side_effect=[APIConnectionError(request=request), "response"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    monkeypatch.setattr("openrouter_service.RETRY_DELAY_SECONDS", 0)
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"):
        assert await chat_completion(model="model", messages=[]) == "response"
    assert create.await_count == 2


@pytest.mark.anyio
async def test_chat_completion_does_not_repeat_a_timeout(monkeypatch):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    create = AsyncMock(side_effect=APITimeoutError(request=request))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"):
        with pytest.raises(APITimeoutError):
            await chat_completion(model="model", messages=[])
    assert create.await_count == 1
    client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_transcribe_audio_uses_openrouter_stt_endpoint():
    response = httpx.Response(
        200,
        json={"text": "привет планируй"},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/audio/transcriptions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response) as post, \
         patch.object(settings, "OPENROUTER_API_KEY", "test-key"), \
         patch.object(settings, "OPENROUTER_MODEL_FOR_AUDIO", "openai/whisper-large-v3-turbo"), \
         patch.object(settings, "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"):
        text = await transcribe_audio(b"ogg-data")

    assert text == "привет планируй"
    assert post.await_args.args[0] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert post.await_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert post.await_args.kwargs["json"]["model"] == "openai/whisper-large-v3-turbo"
    assert post.await_args.kwargs["json"]["input_audio"]["format"] == "ogg"


@pytest.mark.anyio
async def test_chat_completion_bounds_the_whole_request(monkeypatch):
    # OpenRouter keeps a queued request open with keep-alive bytes, so the
    # SDK read timeout never fires; the call as a whole must still end.
    import asyncio

    async def stalled(**arguments):
        await asyncio.sleep(10)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=stalled)), close=AsyncMock())
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"):
        with pytest.raises(APITimeoutError):
            await chat_completion(model="model", messages=[], timeout=0.05)
    client.close.assert_awaited_once()


def test_public_error_text_hides_provider_details():
    from openrouter_service import UNAVAILABLE_TEXT, public_error_text

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    assert public_error_text(ValueError("Окончание должно быть позже начала")) == "Окончание должно быть позже начала"
    assert public_error_text(APIConnectionError(request=request)) == UNAVAILABLE_TEXT
    assert public_error_text(ValueError("Error code: 404 - {'error': 'model'}")) == UNAVAILABLE_TEXT
    assert public_error_text(RuntimeError("boom")) == UNAVAILABLE_TEXT


def _gone(message="Qwen3 VL 32B Instruct was deprecated on Oct 9, 2026."):
    from openai import NotFoundError
    response = httpx.Response(404, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    return NotFoundError(message, response=response, body={"error": {"message": message}})


@pytest.mark.anyio
async def test_a_retired_model_falls_back_to_a_working_one(monkeypatch):
    create = AsyncMock(side_effect=[_gone(), "response"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL_FOR_OCR", raising=False)
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"), \
         patch.object(settings, "OPENROUTER_MODEL_FOR_OCR", "qwen/qwen3-vl-32b-instruct"):
        assert await chat_completion(model="qwen/qwen3-vl-32b-instruct", messages=[]) == "response"
    assert [call.kwargs["model"] for call in create.await_args_list] == ["qwen/qwen3-vl-32b-instruct", "qwen/qwen3.7-plus"]


@pytest.mark.anyio
async def test_fallback_can_be_configured_and_other_errors_are_not_masked(monkeypatch):
    from openai import BadRequestError
    create = AsyncMock(side_effect=[_gone("This model is unavailable for free."), "response"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL_FOR_TEXT", "my/text-model")
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"), \
         patch.object(settings, "OPENROUTER_MODEL_FOR_TEXT", "deepseek/x:free"):
        await chat_completion(model="deepseek/x:free", messages=[])
    assert create.await_args_list[1].kwargs["model"] == "my/text-model"
    # A bad request about the input is the caller's problem, not a missing model.
    bad = BadRequestError("JSON mode is not supported", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
    create = AsyncMock(side_effect=[bad])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"), \
         patch.object(settings, "OPENROUTER_MODEL_FOR_TEXT", "deepseek/x:free"):
        with pytest.raises(BadRequestError):
            await chat_completion(model="deepseek/x:free", messages=[])
    assert create.await_count == 1


@pytest.mark.anyio
async def test_mandatory_reasoning_is_turned_on_instead_of_failing():
    from openai import BadRequestError
    refused = BadRequestError(
        "Reasoning is mandatory for this endpoint and cannot be disabled.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
    )
    create = AsyncMock(side_effect=[refused, "response"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    with patch("openrouter_service.openrouter_client", return_value=client), \
         patch.object(settings, "OPENROUTER_API_KEY", "test"):
        result = await chat_completion(model="google/gemini-3.8-flash", messages=[], reasoning={"enabled": False, "exclude": True})
    assert result == "response"
    assert create.await_args_list[1].kwargs["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}
