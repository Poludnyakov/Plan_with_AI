import asyncio
from harness import TgSim, show
from database import init_db

async def main():
    await init_db()
    sim = TgSim()
    show(await sim.text("/start"))
    out = await sim.text("завтра в 15 встреча с врачом")
    show(out)
asyncio.run(main())
