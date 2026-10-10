import asyncio, logging
from pathlib import Path
import harness
from spreadsheet_schedule_parser import parse_spreadsheet_schedule
logging.getLogger("SpreadsheetScheduleParser").setLevel(logging.INFO)
ROOT = Path(__file__).resolve().parents[2]
async def main():
    data = (ROOT / "ОСЕНЬ 26_27_тест.xlsx").read_bytes()
    for prompt in ("521", "AI360 522"):
        r = await parse_spreadsheet_schedule(data, ".xlsx", prompt)
        print(prompt, len(r["slots"]), r["verification"]["warnings"], r["verification"]["scope_labels"])
        for s in r["slots"]:
            print("  ", s["occurrence_date"], s["weekday"], s["start_time"], s["end_time"], s["week_pattern"], s["title"][:50], "|", s["source_quote"][:80])
asyncio.run(main())
