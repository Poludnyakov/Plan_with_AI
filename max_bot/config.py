import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MaxSettings:
    bot_token: str = os.getenv("MAX_BOT_TOKEN", "")
    webhook_secret: str = os.getenv("MAX_WEBHOOK_SECRET", "")
    public_base_url: str = os.getenv("MAX_PUBLIC_BASE_URL", "https://planwithai.ru")
    miniapp_name: str = os.getenv("MAX_MINIAPP_NAME", "")
    bot_username: str = os.getenv("MAX_BOT_USERNAME", "")
    api_base_url: str = os.getenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
    auto_register_webhook: bool = os.getenv("MAX_AUTO_REGISTER_WEBHOOK", "true").lower() in {
        "1", "true", "yes", "on"
    }

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/max/webhook"

    @property
    def miniapp_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/max/miniapp"


settings = MaxSettings()
