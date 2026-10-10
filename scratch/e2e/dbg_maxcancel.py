import asyncio, time
import harness
from database import init_db
from max_flows import FakeMaxClient, MaxSim
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService
async def main():
    await init_db()
    c = FakeMaxClient(); h = FeatureMaxUpdateHandler(c, MaxEventService(), MaxSettings())
    sim = MaxSim(h, c, 795000000 + int(time.time()) % 100000)
    await sim.text("завтра в 15 встреча с научным руководителем")
    await sim.text("Отмени встречу с научным руководителем.")
    await sim.text("завтра в 17 встреча с деканом")
    await sim.press("Подтвердить")
    await sim.text("Отмени встречу с деканом.")
asyncio.run(main())
