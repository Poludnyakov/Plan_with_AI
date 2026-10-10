"""Voice messages through the real Telegram and MAX handlers (real STT + LLM)."""
import asyncio, itertools, sys, time
from pathlib import Path
import harness
from harness import TgSim, user_events
from aiogram.types import Voice
from database import init_db
from max_flows import FakeMaxClient, MaxSim, joined
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService

VOICE = Path(__file__).resolve().parent / "voice"
ids = itertools.count(1)


async def tg_voice(sim, name):
    data = (VOICE / name).read_bytes()
    file_id = f"voice{next(ids)}"
    sim.files[file_id] = data
    voice = Voice(file_id=file_id, file_unique_id=file_id, duration=4, mime_type="audio/ogg", file_size=len(data))
    out = await sim._feed(message=sim._message(voice=voice))
    texts = [t for n, _, t, _ in out if t and not t.startswith("⏳")]
    print(f"\n[TG] » {name}")
    for t in texts:
        print("   бот:", t.replace("\n", " ⏎ ")[:300])
    return out


async def max_voice(sim, name):
    url = f"https://files.example/v{next(ids)}"
    sim.c.files[url] = (VOICE / name).read_bytes()
    print(f"\n[MAX] » {name}")
    return await sim._run({"update_type": "message_created", "message": {
        "sender": {"user_id": sim.user_id}, "recipient": {"user_id": 1},
        "body": {"mid": f"u{next(ids)}", "text": "", "attachments": [
            {"type": "audio", "payload": {"url": url}}]}}})


ORDER = ["v01.ogg", "v02.ogg", "v03.ogg", "v04.ogg", "v05.ogg", "v06.ogg", "v08.ogg",
         "v09.ogg", "v10.ogg", "v11.ogg", "v12.ogg", "v01_noisy.ogg", "noise.ogg", "silence.ogg"]


async def main():
    await init_db()
    platform = sys.argv[1] if len(sys.argv) > 1 else "tg"
    if platform == "tg":
        sim = TgSim(user_id=780000000 + int(time.time()) % 100000)
        for name in ORDER:
            await tg_voice(sim, name)
            if name == "v03.ogg":
                print("   [нажата «Подтвердить»]")
                await sim.press("Подтверд")
        await tg_voice(sim, "v07.ogg")       # «перенеси спортзал на восемь вечера»
        await sim.press("Подтверд") if any("Подтверд" in b.text for b in sim.buttons()) else None
        await tg_voice(sim, "v13.ogg")       # «отмени встречу с научным руководителем»
        for row in await user_events(sim.user.id):
            print("   БД:", row[0], row[1], row[2].strftime("%d.%m %H:%M"), "–", row[3].strftime("%d.%m %H:%M"), "весь день" if row[4] else "")
    else:
        client = FakeMaxClient()
        handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
        sim = MaxSim(handler, client, 790000000 + int(time.time()) % 100000)
        for name in ORDER:
            await max_voice(sim, name)
            if name == "v03.ogg":
                await sim.press("Подтвердить")
        await max_voice(sim, "v07.ogg")
        await max_voice(sim, "v13.ogg")
        import database
        from zoneinfo import ZoneInfo
        from sqlalchemy import select
        from max_bot.models import MaxEvent, MaxEventTiming, MaxUser
        zone = ZoneInfo("Europe/Moscow")
        async with database.async_session_maker() as db:
            rows = (await db.execute(select(MaxEvent.title, MaxEvent.status, MaxEventTiming.start_at, MaxEventTiming.end_at, MaxEventTiming.all_day)
                .join(MaxUser, MaxUser.id == MaxEvent.user_id).join(MaxEventTiming, MaxEventTiming.event_id == MaxEvent.id)
                .filter(MaxUser.max_user_id == sim.user_id).order_by(MaxEventTiming.start_at))).all()
        for t, st, a, b, d in rows:
            print("   БД:", t, st, a.astimezone(zone).strftime("%d.%m %H:%M"), "–", b.astimezone(zone).strftime("%d.%m %H:%M"), "весь день" if d else "")
asyncio.run(main())
