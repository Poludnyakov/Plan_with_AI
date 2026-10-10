import asyncio
import harness
from interval_ai_service import extract_intervals
ctx = [
 "user: завтра с 14:30 до 15:30 обед",
 "assistant: Событие: Обед; начало 2026-10-09T11:30:00+00:00; окончание 2026-10-09T12:30:00+00:00; по времени.",
 "user: завтра весь день командировка в Тверь",
 "assistant: Событие: Командировка в Тверь; начало 2026-10-08T21:00:00+00:00; окончание 2026-10-09T21:00:00+00:00; весь день.",
 "user: завтра в 20 ужин с коллегами",
 "assistant: Событие: Ужин с коллегами; начало 2026-10-09T17:00:00+00:00; окончание 2026-10-09T18:00:00+00:00; по времени.",
]
async def one():
    try:
        return await extract_intervals("перенеси спортзал на 15:30", context=ctx)
    except Exception as e:
        return repr(e)
async def main():
    for r in await asyncio.gather(*(one() for _ in range(12))):
        print(r if isinstance(r, str) else [(e["title"], e["start_at"].isoformat()) for e in r])
asyncio.run(main())
