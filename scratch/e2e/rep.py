import asyncio, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import harness, interval_ai_service
from text_cases import FrozenDateTime
interval_ai_service.datetime = FrozenDateTime
async def main():
    text = sys.argv[1]
    res = await asyncio.gather(*(interval_ai_service.extract_intervals(text) for _ in range(int(sys.argv[2]))))
    for r in res:
        print([(e["title"], e["start_at"].strftime("%d.%m %H:%M")) for e in r])
asyncio.run(main())
