"""Schedule photos through the real Telegram and MAX handlers."""
import asyncio, itertools, sys, time
from pathlib import Path
import harness
from harness import TgSim, user_series
from aiogram.types import PhotoSize
from database import init_db
from max_flows import FakeMaxClient, MaxSim
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService
import database
from sqlalchemy import select, func
from schedule_models import ScheduleException
from account_service import account_id_for
from schedule_models import ScheduleSeries

PHOTO = Path(__file__).resolve().parent / "photo"
DAYS = "пн вт ср чт пт сб вс".split()
ids = itertools.count(1)

async def series_of(platform, uid):
    async with database.async_session_maker() as db:
        acc = await account_id_for(db, platform, uid)
        rows = (await db.execute(select(ScheduleSeries).filter(ScheduleSeries.account_id == acc)
                .order_by(ScheduleSeries.weekday, ScheduleSeries.start_time))).scalars().all()
        skipped = dict((await db.execute(select(ScheduleException.series_id, func.count()).filter(
            ScheduleException.series_id.in_([s.id for s in rows] or [0])).group_by(ScheduleException.series_id))).all())
    return rows, skipped

def dump(rows, skipped):
    for s in rows:
        extra = f" (пропусков: {skipped[s.id]})" if s.id in skipped else ""
        print(f"      {DAYS[s.weekday]} {s.start_time:%H:%M}-{s.end_time:%H:%M} {s.week_pattern:5} {s.title}{extra}")

async def tg(name, caption, followups):
    sim = TgSim(user_id=785000000 + int(time.time() * 1000) % 1000000)
    data = (PHOTO / name).read_bytes()
    fid = f"photo{next(ids)}"
    sim.files[fid] = data
    photo = [PhotoSize(file_id=fid, file_unique_id=fid, width=1280, height=720, file_size=len(data))]
    t = time.monotonic()
    out = await sim._feed(message=sim._message(photo=photo, caption=caption))
    for text in followups:
        out += await sim.text(text)
        if any("Добавить" in (b.text or "") or "Подтверд" in (b.text or "") for b in sim.buttons()):
            pass
    rows, skipped = await series_of("telegram", sim.user.id)
    print(f"\n[TG] » {name} подпись={caption!r} ответы={followups} → {len(rows)} за {time.monotonic()-t:.0f}s")
    for n, _, x, m in out:
        if x and not x.startswith("⏳"):
            print("   бот:", x.replace("\n", " ⏎ ")[:260])
    dump(rows, skipped)

async def mx(name, caption, followups):
    client = FakeMaxClient()
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
    sim = MaxSim(handler, client, 796000000 + int(time.time() * 1000) % 100000)
    url = f"https://files.example/p{next(ids)}"
    client.files[url] = (PHOTO / name).read_bytes()
    t = time.monotonic()
    await sim._run({"update_type": "message_created", "message": {
        "sender": {"user_id": sim.user_id}, "recipient": {"user_id": 1},
        "body": {"mid": f"u{next(ids)}", "text": caption or "", "attachments": [
            {"type": "image", "payload": {"url": url}}]}}})
    for text in followups:
        await sim.text(text)
    rows, skipped = await series_of("max", sim.user_id)
    print(f"\n[MAX] » {name} → {len(rows)} за {time.monotonic()-t:.0f}s")
    dump(rows, skipped)

PERIOD = "с 1 сентября по 31 декабря"
CASES = {
    "school": ("school_photo.jpg", None, [PERIOD]),
    "sport": ("sport_photo.jpg", None, [PERIOD]),
    "uni": ("uni_photo.jpg", None, [PERIOD]),
    "easy": ("easy_timetable.png", None, [PERIOD]),
    "school_clean": ("school_clean.jpg", None, [PERIOD]),
    "sport_clean": ("sport_clean.jpg", None, [PERIOD]),
    "uni_clean": ("uni_clean.jpg", None, [PERIOD]),
    "school_cap": ("school_photo.jpg", "расписание с 1 сентября по 31 декабря", []),
}

async def main():
    await init_db()
    platform = sys.argv[1]
    for key in sys.argv[2:] or CASES:
        await (tg if platform == "tg" else mx)(*CASES[key])
asyncio.run(main())
