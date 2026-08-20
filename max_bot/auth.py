import hashlib
import hmac
import json
import time
from collections import Counter
from typing import Optional
from urllib.parse import parse_qsl


def validate_max_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 3600,
    now: Optional[int] = None,
) -> Optional[dict]:
    """Validate MAX WebAppData according to the official HMAC-SHA256 flow."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    counts = Counter(key for key, _ in pairs)
    if any(count != 1 for count in counts.values()) or counts.get("hash") != 1:
        return None
    fields = dict(pairs)
    received_hash = fields.pop("hash")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        return None
    try:
        auth_date = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if auth_date > current + 60 or current - auth_date > max_age_seconds:
            return None
        user = json.loads(fields["user"])
        identity = user.get("user_id", user.get("id"))
        user["user_id"] = int(identity)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return user


def sign_max_session(max_user_id: int, bot_token: str) -> str:
    value = str(max_user_id)
    signature = hmac.new(bot_token.encode(), f"max-session:{value}".encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def verify_max_session(value: str, bot_token: str) -> Optional[int]:
    try:
        user_id, signature = value.split(".", 1)
        expected = hmac.new(
            bot_token.encode(), f"max-session:{user_id}".encode(), hashlib.sha256
        ).hexdigest()
        return int(user_id) if hmac.compare_digest(signature, expected) else None
    except (AttributeError, TypeError, ValueError):
        return None


def sign_max_access_token(max_user_id: int, bot_token: str, issued_at: Optional[int] = None) -> str:
    issued = int(time.time()) if issued_at is None else issued_at
    payload = f"{max_user_id}.{issued}"
    signature = hmac.new(bot_token.encode(), f"max-access:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_max_access_token(
    token: str, bot_token: str, ttl_seconds: int = 86400, now: Optional[int] = None
) -> Optional[int]:
    try:
        user_id, issued_text, signature = token.split(".", 2)
        issued = int(issued_text)
        current = int(time.time()) if now is None else now
        if issued > current + 60 or current - issued > ttl_seconds:
            return None
        payload = f"{user_id}.{issued}"
        expected = hmac.new(
            bot_token.encode(), f"max-access:{payload}".encode(), hashlib.sha256
        ).hexdigest()
        return int(user_id) if hmac.compare_digest(signature, expected) else None
    except (AttributeError, TypeError, ValueError):
        return None
