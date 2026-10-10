import asyncio, sys, time
from pathlib import Path
import harness
from harness import TgSim, user_series
from database import init_db
import spreadsheet_schedule_parser as sp
from mirea_truth import truth
from sqlalchemy import select, func
import database
from schedule_models import ScheduleException
HERE = Path(__file__).resolve().parent

async def check(name, sheet_index, group, group_col, followups):
    data = (HERE / "web" / name).read_bytes()
    grid = (sp._read_xls if name.endswith(".xls") else sp._read_xlsx)(data)[sheet_index]
    expected = truth(grid.values, grid.merges, group_col)
    sim = TgSim(user_id=770000000 + int(time.time() * 1000) % 1000000)
    await sim.document(HERE / "web" / name, None)
    for text in followups:
        await sim.text(text)
    series = await user_series(sim.user.id)
    async with database.async_session_maker() as db:
        skipped = dict((await db.execute(select(ScheduleException.series_id, func.count()).filter(
            ScheduleException.series_id.in_([s.id for s in series])).group_by(ScheduleException.series_id))).all())
    got = {(s.weekday, s.start_time.strftime("%H:%M"), s.week_pattern) for s in series}
    want = {(d, t, w) for d, t, w, _ in expected}
    print(f"\n» {name} {group}: эталон {len(expected)} ячеек / {len(want)} слотов, импорт {len(series)} серий")
    for item in sorted(want - got):
        print("   ПРОПУЩЕНО:", item, [x[3][:50] for x in expected if x[:3] == item])
    for item in sorted(got - want):
        print("   ЛИШНЕЕ:", item, [s.title for s in series if (s.weekday, s.start_time.strftime('%H:%M'), s.week_pattern) == item])
    marks = [x for x in expected if re.match(r"\s*(кр\.?\s*)?\d", x[3])]
    print(f"   ячеек с пометкой недель: {len(marks)}, серий с пропусками недель: {len(skipped)}")
    for s in series:
        if s.id in skipped:
            print(f"     {s.title[:50]} {s.week_pattern}: пропущено дат {skipped[s.id]}")

import re
CASES = {
    "kmbo03": ("0niel_test_schedule.xlsx", 0, "КМБО-03-19", 11, ["КМБО-03-19", "с 1 сентября по 31 декабря"]),
    "bbbo05": ("MinusD_4k.xls", 4, "БББО-05-19", 6, ["БББО-05-19", "с 1 сентября по 31 декабря"]),
    "kmbo01": ("0niel_test_schedule.xlsx", 0, "КМБО-01-19", 6, ["КМБО-01-19", "с 1 сентября по 31 декабря"]),
}
async def main():
    await init_db()
    for key in sys.argv[1:] or CASES:
        await check(*CASES[key])
asyncio.run(main())
