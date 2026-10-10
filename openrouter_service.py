"""Small, provider boundary for every OpenRouter parsing request."""

import asyncio
import base64
import logging
import os
import re

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from config import settings


logger = logging.getLogger("OpenRouter")
RETRY_DELAY_SECONDS = 1.5
DEFAULT_TIMEOUT_SECONDS = 90.0
# OpenRouter retires models (a free variant, an old vision model): then a request
# for that kind goes to a working one instead of failing every message.
FALLBACK_MODELS = {
    "TEXT": "deepseek/deepseek-v4-flash-0731",
    "FILES": "deepseek/deepseek-v4.1-flash",
    "OCR": "qwen/qwen3.7-plus",
}


NO_SPEECH_TEXT = (
    "Не удалось разобрать речь в голосовом сообщении. Скажите ещё раз, "
    "например: «завтра в 15:00 встреча»."
)


def _is_transient(error: Exception) -> bool:
    """A dropped connection, a rate limit or a provider 5xx: worth one retry.

    A timeout is not: the user has already waited the whole timeout.
    """
    if isinstance(error, APITimeoutError):
        return False
    return isinstance(error, (APIConnectionError, RateLimitError, InternalServerError))


def fallback_model(model: str) -> str | None:
    """The replacement for a configured model; OPENROUTER_FALLBACK_MODEL_FOR_<KIND> overrides."""
    for kind, default in FALLBACK_MODELS.items():
        if model == getattr(settings, f"OPENROUTER_MODEL_FOR_{kind}", None):
            candidate = os.getenv(f"OPENROUTER_FALLBACK_MODEL_FOR_{kind}") or default
            return candidate if candidate != model else None
    return None


def _model_gone(error: Exception) -> bool:
    """The model no longer exists or is not served (retired, free tier closed)."""
    if isinstance(error, NotFoundError):
        return True
    return isinstance(error, BadRequestError) and "not a valid model" in str(error).lower()


def _reasoning_required(error: Exception) -> bool:
    """Some providers of a model (e.g. Gemini 3.x) refuse to turn reasoning off."""
    return isinstance(error, BadRequestError) and "reasoning is mandatory" in str(error).lower()


def reasoning_setting(effort: str) -> dict:
    """OpenRouter `reasoning` parameter for an effort name; "none" turns it off."""
    if effort == "none":
        return {"enabled": False, "exclude": True}
    return {"effort": effort, "exclude": True}


def _api_root(url: str | None = None) -> str:
    """Return the API root accepted by the OpenAI SDK, never a chat endpoint."""
    configured = (url or settings.OPENROUTER_URL or "").rstrip("/")
    suffix = "/chat/completions"
    if configured.endswith(suffix):
        configured = configured[: -len(suffix)]
    return (configured or "https://openrouter.ai/api/v1").rstrip("/")


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
    if settings.OPENROUTER_APP_TITLE:
        headers["X-OpenRouter-Title"] = settings.OPENROUTER_APP_TITLE
    return headers


def require_openrouter(model: str | None) -> str:
    if not settings.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY не настроен.")
    if not model:
        raise ValueError("Не выбрана модель OpenRouter для этого типа входных данных.")
    return model


def openrouter_client(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> AsyncOpenAI:
    require_openrouter("configured")
    return AsyncOpenAI(
        base_url=_api_root(),
        api_key=settings.OPENROUTER_API_KEY,
        default_headers=_headers(),
        # Do not let a queued free-model request block a messenger update for minutes.
        timeout=timeout,
        max_retries=0,
    )


async def chat_completion(
    *,
    model: str,
    messages: list[dict],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    reasoning: dict | None = None,
    timeout: float | None = None,
):
    """Call OpenRouter's OpenAI-compatible chat endpoint with direct model IDs."""
    client = openrouter_client(timeout) if timeout else openrouter_client()
    arguments: dict = {
        "model": require_openrouter(model),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        arguments["max_tokens"] = max_tokens
    if response_format is not None:
        arguments["response_format"] = response_format
    if reasoning is not None:
        arguments["extra_body"] = {"reasoning": reasoning}
    deadline = timeout or DEFAULT_TIMEOUT_SECONDS

    async def create():
        # The SDK timeout bounds each read, and OpenRouter keeps a queued
        # request alive with blank keep-alive bytes; bound the whole call.
        try:
            return await asyncio.wait_for(
                client.chat.completions.create(**arguments), deadline
            )
        except asyncio.TimeoutError as error:
            raise APITimeoutError(
                request=httpx.Request("POST", _api_root() + "/chat/completions")
            ) from error

    try:
        try:
            try:
                return await create()
            except Exception as error:
                if _reasoning_required(error) and arguments.get("extra_body", {}).get("reasoning", {}).get("enabled") is False:
                    # Use the least reasoning the provider accepts instead of failing.
                    arguments["extra_body"]["reasoning"] = {"effort": "low", "exclude": True}
                    return await create()
                if not _is_transient(error):
                    raise
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            return await create()
        except Exception as error:
            replacement = fallback_model(arguments["model"])
            if not (_model_gone(error) and replacement):
                raise
            logger.warning(
                "OpenRouter model %s is unavailable (%s); using %s. Update the model in the env.",
                arguments["model"], str(error)[:200], replacement,
            )
            arguments["model"] = replacement
            return await create()
    finally:
        await client.close()


_CYRILLIC_RE = re.compile("[А-Яа-яЁё]")
UNAVAILABLE_TEXT = (
    "Сервис распознавания сейчас не ответил. Попробуйте ещё раз через минуту."
)


def public_error_text(error: Exception) -> str:
    """What a messenger user may see about a failure.

    Our own validation errors are written in Russian for the user; provider,
    network and library errors carry technical details and stay in the log.
    """
    if isinstance(error, (ValueError, LookupError)):
        text = str(error).strip()
        if text and _CYRILLIC_RE.search(text) and "Error code" not in text:
            return text
    return UNAVAILABLE_TEXT


async def transcribe_audio(audio_bytes: bytes, audio_format: str = "ogg") -> str:
    """Use OpenRouter STT; its endpoint is separate from chat/completions."""
    model = require_openrouter(settings.OPENROUTER_MODEL_FOR_AUDIO)
    if not audio_bytes:
        raise ValueError("Аудиосообщение пустое.")
    endpoint = _api_root() + "/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        **_headers(),
    }
    payload = {
        "model": model,
        "input_audio": {
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "format": audio_format,
        },
        "language": "ru",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    client.post(endpoint, headers=headers, json=payload), timeout=90.0
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                # A dropped connection, a rate limit or a provider 5xx: one retry.
                if attempt == 2 or isinstance(error, httpx.TimeoutException):
                    raise
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        response.raise_for_status()
    text = str(response.json().get("text") or "").strip()
    if not text:
        raise ValueError(NO_SPEECH_TEXT)
    return text
