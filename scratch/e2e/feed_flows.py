import asyncio, re, time, urllib.request
import harness
from harness import TgSim, show
from database import init_db
from icalendar import Calendar
from max_flows import FakeMaxClient, MaxSim, joined
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService

def fetch(url):
    local = url.replace("https://planwithai.ru", "http://127.0.0.1:8077")
    with urllib.request.urlopen(local, timeout=10) as r:
        return r.status, r.headers.get("content-type"), r.read()

async def main():
    await init_db()
    sim = TgSim(user_id=797000000 + int(time.time()) % 100000)
    await sim.text("завтра с 15 до 17 встреча с инвестором"); await sim.press("Подтверд")
    await sim.text("послезавтра весь день конференция"); await sim.press("Подтверд")
    out = await sim.text("/calendars")
    show(out)
    text = out[-1][2]
    feed_url = re.search(r"https://\S+\.ics", text).group(0)
    status, ctype, body = fetch(feed_url)
    events = [str(e.get("summary")) for e in Calendar.from_ical(body).walk("VEVENT")]
    print("TG лента:", status, ctype, events)
    out2, answers = await sim.press("Новая ссылка")
    new_url = re.search(r"https://\S+\.ics", out2[-1][2]).group(0)
    try:
        fetch(feed_url); print("✘ старая ссылка всё ещё работает")
    except Exception as error:
        print("старая ссылка после сброса:", error)
    print("новая ссылка:", fetch(new_url)[0])

    client = FakeMaxClient()
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
    msim = MaxSim(handler, client, 798000000 + int(time.time()) % 100000)
    await msim.text("завтра в 10 стоматолог"); await msim.press("Подтвердить")
    out = await msim.text("/calendars")
    url = re.search(r"https://\S+\.ics", joined(out)).group(0)
    status, ctype, body = fetch(url)
    print("MAX лента:", status, [str(e.get("summary")) for e in Calendar.from_ical(body).walk("VEVENT")])
asyncio.run(main())
