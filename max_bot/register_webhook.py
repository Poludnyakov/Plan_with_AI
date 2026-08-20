import asyncio

from .api import MaxApiClient
from .config import settings


async def main() -> None:
    client = MaxApiClient(settings.bot_token, settings.api_base_url)
    try:
        created = await client.ensure_webhook(settings.webhook_url, settings.webhook_secret)
        print("registered" if created else "already registered")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

