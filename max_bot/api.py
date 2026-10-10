import asyncio
import logging
import ssl
from typing import Any

import httpx


logger = logging.getLogger("MaxApiClient")


class MaxApiError(RuntimeError):
    pass


class MaxApiClient:
    """Small async client for the official MAX Bot API."""

    def __init__(self, token: str, base_url: str, timeout: float = 30.0):
        if not token:
            raise ValueError("MAX_BOT_TOKEN is not configured")
        tls_context = ssl.create_default_context()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": token},
            verify=tls_context,
            timeout=timeout,
        )
        self.bot_info: dict[str, Any] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(3):
            response = await self._client.request(method, path, **kwargs)
            if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                break
            delay = min(float(response.headers.get("Retry-After", "1")), 5.0)
            await asyncio.sleep(delay)
        if response.is_error:
            logger.error("MAX API %s %s failed: %s %s", method, path, response.status_code, response.text)
            raise MaxApiError(f"MAX API returned HTTP {response.status_code}")
        return response.json() if response.content else {}

    async def send_message(
        self,
        text: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        buttons: list[list[dict[str, Any]]] | None = None,
        format: str = "markdown",
    ) -> dict[str, Any]:
        if (user_id is None) == (chat_id is None):
            raise ValueError("Exactly one of user_id and chat_id is required")
        params = {"user_id": user_id} if user_id is not None else {"chat_id": chat_id}
        body: dict[str, Any] = {"text": text}
        if format:  # None sends plain text: links with «_» stay intact
            body["format"] = format
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        return await self.request("POST", "/messages", params=params, json=body)

    async def answer_callback(
        self,
        callback_id: str,
        *,
        text: str | None = None,
        notification: str | None = None,
        buttons: list[list[dict[str, Any]]] | None = None,
        format: str | None = "markdown",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if text is not None:
            message: dict[str, Any] = {"text": text}
            if format:
                message["format"] = format
            if buttons:
                message["attachments"] = [
                    {"type": "inline_keyboard", "payload": {"buttons": buttons}}
                ]
            body["message"] = message
        if notification:
            body["notification"] = notification[:200]
        return await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        return await self.request("DELETE", "/messages", params={"message_id": message_id})

    async def download(self, url: str) -> bytes:
        response = await self._client.get(url)
        response.raise_for_status()
        return response.content

    async def me(self) -> dict[str, Any]:
        """The bot itself (user_id, username); kept for mini app buttons."""
        self.bot_info = await self.request("GET", "/me")
        return self.bot_info

    async def set_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        return await self.request("PATCH", "/me/commands", json={"commands": commands})

    async def subscriptions(self) -> list[dict[str, Any]]:
        data = await self.request("GET", "/subscriptions")
        if isinstance(data, list):
            return data
        return data.get("subscriptions", []) if isinstance(data, dict) else []

    async def ensure_webhook(self, url: str, secret: str) -> bool:
        subscriptions = await self.subscriptions()
        required_types = {"message_created", "message_callback", "bot_started"}
        existing = next((item for item in subscriptions if item.get("url") == url), None)
        if existing and required_types.issubset(set(existing.get("update_types") or [])):
            return False
        body: dict[str, Any] = {
            "url": url,
            "update_types": sorted(required_types),
        }
        if secret:
            body["secret"] = secret
        result = await self.request("POST", "/subscriptions", json=body)
        if isinstance(result, dict) and result.get("success") is False:
            reason = result.get("message") or "MAX отклонил регистрацию webhook"
            raise MaxApiError(str(reason))
        return True


def callback_button(text: str, payload: str) -> dict[str, str]:
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict[str, str]:
    return {"type": "link", "text": text, "url": url}


def open_app_button(
    text: str, web_app: str | None = None, contact_id: int | None = None
) -> dict[str, Any]:
    button: dict[str, Any] = {"type": "open_app", "text": text}
    if web_app:
        button["web_app"] = web_app
    if contact_id:
        button["contact_id"] = contact_id
    return button


def calendar_button(client: Any, settings: Any, text: str = "📅 Открыть календарь") -> dict[str, Any]:
    """Opens our mini app. MAX needs the bot named in the button: without it a
    button may open the page as an ordinary site (e.g. after the user leaves
    the chat and starts it again)."""
    bot = getattr(client, "bot_info", None) or {}
    web_app = (
        getattr(settings, "miniapp_name", "")
        or getattr(settings, "bot_username", "").strip().lstrip("@")
        or bot.get("username")
        or None
    )
    return open_app_button(text, web_app, bot.get("user_id"))
