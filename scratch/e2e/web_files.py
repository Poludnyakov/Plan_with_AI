import asyncio, sys, time
from pathlib import Path
from harness import TgSim, user_series
from database import init_db
HERE = Path(__file__).resolve().parent
DAYS = "пн вт ср чт пт сб вс".split()

async def run(name, caption=None, followups=()):
    sim = TgSim(user_id=760000000 + int(time.time() * 1000) % 1000000)
    t = time.monotonic()
    out = await sim.document(HERE / "web" / name, caption)
    for text in followups:
        out += await sim.text(text)
    series = await user_series(sim.user.id)
    final = [x for n, _, x, _ in out if n == "SendMessage" and x and not x.startswith("⏳")]
    print(f"\n» {name[:40]} подпись={caption!r} ответы={list(followups)} → {len(series)} событий за {time.monotonic()-t:.0f}s")
    for line in final:
        print("   бот:", line.replace("\n", " ⏎ ")[:400])
    for item in series:
        print(f"      {DAYS[item.weekday]} {item.start_time:%H:%M}-{item.end_time:%H:%M} {item.week_pattern:5} {item.valid_from}..{item.valid_until} {item.title} | {item.description[:60]}")
    return series

CASES = {
    "mirea": ("0niel_test_schedule.xlsx", "КМБО-01-19", ["с 1 сентября по 31 декабря"]),
    "mirea_nocap": ("0niel_test_schedule.xlsx", None, ["КМБО-03-19", "с 1 сентября по 31 декабря"]),
    "mag": ("MinusD_III_mag_1kurs_osen_2022_2023.xlsx", "КММО-01-22", ["с 1 сентября по 31 декабря"]),
    "chech": ("Magan-Z_raspisanie.xlsx", "ИБ-26", ["1-2 пара 8:30–9:50, 3-4 пара 10:00–11:20, 5-6 пара 11:30–12:50, 7-8 пара 13:20–14:40", "с 1 сентября по 31 декабря"]),
    "chech_sub": ("Magan-Z_raspisanie.xlsx", None, ["БИ-26-1", "1-2 пара 8:30–9:50, 3-4 пара 10:00–11:20, 5-6 пара 11:30–12:50, 7-8 пара 13:20–14:40", "с 1 сентября по 31 декабря"]),
    "xls": ("MinusD_4k.xls", None, ["БББО-05-19", "с 1 сентября по 31 декабря"]),
}

async def main():
    await init_db()
    for key in sys.argv[1:] or CASES:
        await run(*CASES[key])
asyncio.run(main())
