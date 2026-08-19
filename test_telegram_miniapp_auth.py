import hashlib
import hmac
import json
from urllib.parse import urlencode

from telegram_miniapp_auth import validate_telegram_init_data


BOT_TOKEN = "123456:test-token"


def build_init_data(user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAEAAAE",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_init_data_returns_user():
    init_data = build_init_data(987654321, 1_700_000_000)
    user = validate_telegram_init_data(init_data, BOT_TOKEN, now=1_700_000_100)
    assert user["id"] == 987654321


def test_tampered_init_data_is_rejected():
    init_data = build_init_data(987654321, 1_700_000_000).replace("987654321", "111111111")
    assert validate_telegram_init_data(init_data, BOT_TOKEN, now=1_700_000_100) is None


def test_expired_init_data_is_rejected():
    init_data = build_init_data(987654321, 1_700_000_000)
    assert validate_telegram_init_data(init_data, BOT_TOKEN, now=1_700_010_000) is None
