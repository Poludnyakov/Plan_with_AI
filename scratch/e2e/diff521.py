import asyncio
import harness
from spreadsheet_schedule_parser import parse_spreadsheet_schedule
async def main():
    data = (harness.ROOT / "ОСЕНЬ 26_27_тест.xlsx").read_bytes()
    runs = await asyncio.gather(*(parse_spreadsheet_schedule(data, ".xlsx", "521") for _ in range(3)))
    sets = [{(s["weekday"], s["start_time"], s["end_time"], s["title"], s["source_quote"][:24]) for s in r["slots"]} for r in runs]
    common = set.intersection(*sets)
    for i, s in enumerate(sets):
        print(i, len(s), "extra:", sorted(s - common))
asyncio.run(main())
