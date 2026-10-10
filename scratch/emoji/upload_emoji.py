"""Add the new emoji to the «planwithai» custom emoji set and print the code mapping.

Run on your computer (the set owner's Telegram user id is required by the Bot API):
    python upload_emoji.py <ваш_telegram_user_id>
The bot token is read from TELEGRAM_BOT_TOKEN (environment or .env next to the bot).
"""
import json, os, pathlib, sys, urllib.request, uuid

HERE = pathlib.Path(__file__).resolve().parent
SET_NAME = "planwithai"
EMOJI = json.loads((HERE / "emoji.json").read_text(encoding="utf-8"))


def token() -> str:
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        return os.environ["TELEGRAM_BOT_TOKEN"]
    for env in (HERE / ".env", HERE.parent / ".env", pathlib.Path.cwd() / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    sys.exit("TELEGRAM_BOT_TOKEN не найден")


def call(method: str, fields: dict, files: dict | None = None) -> dict:
    boundary = uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    for key, path in (files or {}).items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{path.name}\"\r\n"
                 "Content-Type: image/webp\r\n\r\n").encode() + path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    reply = json.load(urllib.request.urlopen(request, timeout=60))
    if not reply.get("ok"):
        raise RuntimeError(reply)
    return reply["result"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    TOKEN, owner = token(), sys.argv[1]
    known = {s["emoji"].rstrip("️") for s in call("getStickerSet", {"name": SET_NAME})["stickers"]}
    for name, emoji in EMOJI.items():
        if emoji.rstrip("️") in known:
            print("уже в наборе:", emoji)
            continue
        sticker = {"sticker": "attach://file", "format": "static", "emoji_list": [emoji]}
        call("addStickerToSet", {"user_id": owner, "name": SET_NAME, "sticker": json.dumps(sticker)},
             {"file": HERE / "telegram" / f"{name}.webp"})
        print("добавлен:", emoji, name)
    print("\nВставьте в CUSTOM_EMOJI (telegram_custom_emoji.py):")
    for item in call("getStickerSet", {"name": SET_NAME})["stickers"]:
        print(f'    "{item["emoji"].rstrip(chr(0xFE0F))}": "{item["custom_emoji_id"]}",')
