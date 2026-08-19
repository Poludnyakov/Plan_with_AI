"""Server-side validation for Telegram Mini App initData."""

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 3600,
    now: Optional[int] = None,
) -> Optional[dict]:
    """Return the authenticated Telegram user, or None for invalid data."""
    if not init_data or not bot_token:
        return None

    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = fields.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        return None

    try:
        auth_date = int(fields["auth_date"])
        current_time = int(time.time()) if now is None else now
        if auth_date > current_time + 60 or current_time - auth_date > max_age_seconds:
            return None
        user = json.loads(fields["user"])
        user["id"] = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    return user
