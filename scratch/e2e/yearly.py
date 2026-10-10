import asyncio, glob
from real_files import run, ROOT
from database import init_db
async def main():
    await init_db()
    course = sorted(glob.glob(str(ROOT / "расписание _ годовой*.xlsx")))[0]
    await run(course, None, [])
asyncio.run(main())
