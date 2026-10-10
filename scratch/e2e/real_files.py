import asyncio, glob, time
from pathlib import Path
from harness import TgSim, user_series, show
from database import init_db
ROOT = Path(__file__).resolve().parents[2]

async def run(path, caption=None, followups=()):
    sim = TgSim(user_id=750000000 + int(time.time() * 1000) % 1000000)
    t = time.monotonic()
    out = await sim.document(path, caption)
    for text in followups:
        out += await sim.text(text)
    series = await user_series(sim.user.id)
    final = [x for n, _, x, _ in out if n == "SendMessage" and x and not x.startswith("⏳")]
    print(f"\n» {Path(path).name[:40]} подпись={caption!r} ответы={list(followups)} → {len(series)} событий за {time.monotonic()-t:.0f}s")
    for line in final:
        print("   бот:", line.replace("\n", " ⏎ ")[:300])
    return series

async def main():
    await init_db()
    autumn = str(ROOT / "ОСЕНЬ 26_27_тест.xlsx")
    course = sorted(glob.glob(str(ROOT / "расписание _ годовой*.xlsx")))[0]
    for caption, answers in (("521", ["с 1 сентября по 31 декабря"]), (None, ["AI360", "522", "с 1 сентября по 31 декабря"])):
        series = await run(autumn, caption, answers)
        for item in series:
            print(f"      {'пн вт ср чт пт сб вс'.split()[item.weekday]} {item.start_time:%H:%M}-{item.end_time:%H:%M} {item.week_pattern:5} {item.title}")
if __name__ == "__main__":
    asyncio.run(main())
