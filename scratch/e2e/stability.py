import asyncio, glob, time
from pathlib import Path
import harness
from spreadsheet_schedule_parser import parse_spreadsheet_schedule
ROOT = harness.ROOT
async def one(data, prompt, ext=".xlsx"):
    t = time.monotonic()
    try:
        r = await parse_spreadsheet_schedule(data, ext, prompt)
        return prompt, len(r["slots"]), round(time.monotonic() - t), r["verification"]["warnings"]
    except Exception as e:
        return prompt, type(e).__name__ + ": " + str(e)[:80], round(time.monotonic() - t), []
async def main():
    autumn = (ROOT / "ОСЕНЬ 26_27_тест.xlsx").read_bytes()
    course = Path(sorted(glob.glob(str(ROOT / "расписание _ годовой*.xlsx")))[0]).read_bytes()
    jobs = [one(autumn, "AI360 522") for _ in range(4)] + [one(autumn, "521") for _ in range(3)] + [one(course, "") for _ in range(2)]
    for r in await asyncio.gather(*jobs):
        print(r)
asyncio.run(main())
