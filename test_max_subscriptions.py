from unittest.mock import AsyncMock

import pytest

from max_bot.api import MaxApiClient, MaxApiError


@pytest.mark.anyio
async def test_subscriptions_accepts_array_response():
    client = object.__new__(MaxApiClient)
    client.request = AsyncMock(return_value=[{"url": "https://planwithai.ru/max/webhook"}])
    assert await client.subscriptions() == [{"url": "https://planwithai.ru/max/webhook"}]


@pytest.mark.anyio
async def test_subscriptions_accepts_wrapped_response():
    client = object.__new__(MaxApiClient)
    client.request = AsyncMock(return_value={"subscriptions": [{"url": "https://example.test"}]})
    assert await client.subscriptions() == [{"url": "https://example.test"}]


@pytest.mark.anyio
async def test_ensure_webhook_posts_when_subscription_is_missing():
    client = object.__new__(MaxApiClient)
    client.subscriptions = AsyncMock(return_value=[])
    client.request = AsyncMock(return_value={"success": True})
    assert await client.ensure_webhook(
        "https://planwithai.ru/max/webhook", "valid_secret_123"
    ) is True
    client.request.assert_awaited_once_with(
        "POST",
        "/subscriptions",
        json={
            "url": "https://planwithai.ru/max/webhook",
            "update_types": ["bot_started", "message_callback", "message_created"],
            "secret": "valid_secret_123",
        },
    )


@pytest.mark.anyio
async def test_ensure_webhook_updates_old_subscription_without_bot_started():
    client = object.__new__(MaxApiClient)
    client.subscriptions = AsyncMock(return_value=[{
        "url": "https://planwithai.ru/max/webhook",
        "update_types": ["message_created", "message_callback"],
    }])
    client.request = AsyncMock(return_value={"success": True})
    assert await client.ensure_webhook(
        "https://planwithai.ru/max/webhook", "valid_secret_123"
    ) is True
    assert "bot_started" in client.request.await_args.kwargs["json"]["update_types"]


@pytest.mark.anyio
async def test_ensure_webhook_rejects_unsuccessful_200_response():
    client = object.__new__(MaxApiClient)
    client.subscriptions = AsyncMock(return_value=[])
    client.request = AsyncMock(return_value={"success": False, "message": "invalid secret"})
    with pytest.raises(MaxApiError, match="invalid secret"):
        await client.ensure_webhook(
            "https://planwithai.ru/max/webhook", "valid_secret_123"
        )
