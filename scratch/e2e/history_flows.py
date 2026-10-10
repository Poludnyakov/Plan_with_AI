import asyncio, time
import harness
from harness import TgSim, show, user_series, user_events
from database import init_db
from max_flows import FakeMaxClient, MaxSim, joined
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService
import database
from sqlalchemy import select
from account_service import account_id_for
from schedule_models import ScheduleSeries

HERE = harness.Path(__file__).resolve().parent
checks = []
def check(name, ok, detail=""):
    checks.append(ok); print(f"   {'✔' if ok else '✘'} {name}" + (f" — {detail[:300]}" if detail and not ok else ""))

async def series_count(platform, uid):
    async with database.async_session_maker() as db:
        acc = await account_id_for(db, platform, uid)
        return len((await db.execute(select(ScheduleSeries).filter(ScheduleSeries.account_id == acc))).scalars().all())

async def tg():
    sim = TgSim(user_id=799000000 + int(time.time()) % 100000)
    await sim.document(HERE / "xlsx/3_two_groups.xlsx", "ИВТ-22")
    await sim.text("завтра в 18 бассейн"); await sim.press("Подтверд")
    before = await series_count("telegram", sim.user.id)
    out = await sim.text("/history"); show(out)
    text = out[-1][2]
    check("TG: в истории импорт и событие", "3_two_groups.xlsx" in text and "Бассейн" in text, text)
    labels = [b.text for b in sim.buttons()]
    number = next(line.split(".")[0] for line in text.splitlines() if "3_two_groups.xlsx" in line)
    out, _ = await sim.press(f"🗑 {number}"); show(out)
    check("TG: подтверждение перед удалением импорта", "Все занятия этого импорта" in (out[-1][2] or ""), str(out))
    out, _ = await sim.press("Да, удалить"); show(out)
    after = await series_count("telegram", sim.user.id)
    check("TG: импорт удалён целиком", before > 0 and after == 0, f"{before}→{after}")
    events = await user_events(sim.user.id)
    check("TG: личное событие осталось", any(t == "Бассейн" for t, *_ in events), str(events))
    out, _ = await sim.press("К истории"); show(out)
    text = out[-1][2]
    number = next(line.split(".")[0] for line in text.splitlines() if "Бассейн" in line)
    await sim.press(f"🗑 {number}")
    out, _ = await sim.press("Да, удалить"); show(out)
    check("TG: событие удалено из истории", not [e for e in await user_events(sim.user.id) if e[1] == "confirmed"], out[-1][2])

async def mx():
    client = FakeMaxClient()
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
    sim = MaxSim(handler, client, 799500000 + int(time.time()) % 100000)
    await sim.file(HERE / "xlsx/3_two_groups.xlsx", "ИВТ-22")
    before = await series_count("max", sim.user_id)
    out = await sim.text("/history")
    text = joined(out)
    check("MAX: импорт в истории", "3_two_groups.xlsx" in text, text)
    await sim.press("🗑 1")
    out = await sim.press("Да, удалить")
    check("MAX: импорт удалён целиком", before > 0 and await series_count("max", sim.user_id) == 0, joined(out))

async def main():
    await init_db()
    await tg(); await mx()
    print(f"\n{sum(checks)}/{len(checks)} проверок истории прошло")
asyncio.run(main())
