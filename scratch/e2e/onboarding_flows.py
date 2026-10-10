"""Tour and summaries through the real Telegram dispatcher and MAX handler."""
import asyncio, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import harness  # env, DB = planiruy_e2e
from harness import TgSim
import database
from database import init_db
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService
from max_flows import FakeMaxClient, MaxSim, joined
from unified_calendar import create_linked_event

checks = []
ZONE = ZoneInfo("Europe/Moscow")


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"   {'✔' if cond else '✘'} {name}{(' — ' + str(detail)[:400]) if detail and not cond else ''}")


def text_of(entries):
    return "\n".join(text or "" for _, _, text, _ in entries)


async def seed(platform, user_id):
    day = datetime.now(ZONE).date() + timedelta(days=1)
    at = lambda hour: datetime(day.year, day.month, day.day, hour, tzinfo=ZONE)
    async with database.async_session_maker() as db:
        await create_linked_event(db, platform, user_id, "Защита_проекта <финал>", "", at(10), at(11))


async def telegram(user_id):
    print("\n=== Telegram ===")
    sim = TgSim(user_id)
    out = await sim.text("/start")
    first = text_of(out)
    check("TG: первый запуск — список команд", all(c in first for c in ("/tour", "/today", "/week", "/history", "/help")), first)
    tour = [(mid, text) for _, mid, text, _ in out if text and "Знакомство за минуту" in text]
    check("TG: первый запуск — обучение начинается само", bool(tour), first)
    check("TG: у первого шага есть «Открыть календарь»", tour and any(b.web_app for b in sim.buttons(tour[0][0])))
    again = await sim.text("/start")
    check("TG: повторный /start — без списка и без автообучения", "/tour" not in text_of(again) and "Знакомство" not in text_of(again), text_of(again))
    start_id = [mid for _, mid, text, markup in again if hasattr(markup, "inline_keyboard") and any(b.callback_data == "tour:start" for row in markup.inline_keyboard for b in row)][-1]
    out, _ = await sim.click("tour:start", start_id)
    check("TG: кнопка обучения у вернувшегося — новым сообщением", any(name == "SendMessage" and "Знакомство" in (text or "") for name, _, text, _ in out), out)
    tour_id = tour[0][0]
    seen = []
    for step in range(1, 6):
        out, _ = await sim.click(f"tour:{step}", tour_id)
        edits = [(name, mid, text) for name, mid, text, _ in out if name == "EditMessageText" and mid == tour_id]
        seen.append(bool(edits) and f"Шаг {step} из 5" in edits[0][2])
        if step == 1:
            copy = [b for b in sim.buttons(tour_id) if b.copy_text]
            check("TG: шаг 1 — кнопка копирования примера", copy and "встреча с куратором" in copy[0].copy_text.text)
        if step == 3:
            check("TG: шаг 3 — кнопка открывает мини-приложение", any(b.web_app for b in sim.buttons(tour_id)))
    check("TG: все 5 шагов листаются в том же сообщении", all(seen), seen)
    out, _ = await sim.click("tour:2", tour_id)
    check("TG: «Назад» возвращает шаг", "Шаг 2 из 5" in text_of(out))
    out, _ = await sim.click("tour_open:reminders", tour_id)
    check("TG: из обучения открываются настройки напоминаний", "напомин" in text_of(out).lower(), text_of(out))
    out, _ = await sim.click("tour:end", tour_id)
    check("TG: финал с шпаргалкой", "Готово" in text_of(out) and "/week" in text_of(out), text_of(out))

    await seed("telegram", user_id)
    out = await sim.text("/week")
    week = text_of(out)
    check("TG: /week — план на 7 дней с событием", "План на 7 дней" in week and "Защита_проекта &lt;финал&gt;" in week and "Свободные дни" in week, week)
    week_id = [mid for _, mid, text, _ in out if text and "План на 7 дней" in text][0]
    out, _ = await sim.click("summary_switch:today", week_id)
    check("TG: переключение на «сегодня» правит то же сообщение", any(name == "EditMessageText" and mid == week_id for name, mid, _, _ in out), out)
    out = await sim.text("/today")
    check("TG: /today отвечает", "сегодня" in text_of(out).lower(), text_of(out))
    out = await sim.text("/help")
    check("TG: в справке есть /tour, /today, /week", all(c in text_of(out) for c in ("/tour", "/today", "/week")), text_of(out))
    out = await sim.text("/tour")
    check("TG: /tour запускает обучение", "Знакомство за минуту" in text_of(out))


async def max_bot(user_id):
    print("\n=== MAX ===")
    client = FakeMaxClient()
    sim = MaxSim(FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings()), client, user_id)
    out = await sim.text("/start")
    check("MAX: первый запуск — список команд и обучение", "/week" in joined(out) and "Знакомство за минуту" in joined(out), joined(out))
    out = await sim.press("Начать")
    check("MAX: шаг 1 заменяет сообщение (ответ на нажатие)", any(kind == "callback" and "Шаг 1 из 5" in (text or "") for kind, text, _ in out), out)
    clip = [b for _, _, buttons in out for row in buttons or [] for b in row if b.get("type") == "clipboard"]
    check("MAX: кнопка копирования примера", clip and "встреча с куратором" in clip[0]["payload"], clip)
    for label in ("Дальше", "Дальше", "Дальше", "Дальше"):
        out = await sim.press(label)
    check("MAX: дошли до шага 5", "Шаг 5 из 5" in joined(out), joined(out))
    out = await sim.press("Готово")
    check("MAX: финал", "Готово" in joined(out))
    await seed("max", user_id)
    out = await sim.text("/week")
    check("MAX: /week", "План на 7 дней" in joined(out) and "Защита_проекта" in joined(out), joined(out))
    out = await sim.press("План на сегодня")
    check("MAX: переключение на сегодня правит сообщение", any(kind == "callback" and "сегодня" in (text or "").lower() for kind, text, _ in out), out)
    out = await sim.text("/tour")
    check("MAX: /tour", "Знакомство за минуту" in joined(out))


async def main():
    await init_db()
    base = 780000000 + int(time.time()) % 100000 * 10
    await telegram(base + 1)
    await max_bot(base + 2)
    print(f"\n{sum(ok for _, ok in checks)}/{len(checks)} проверок обучения и сводок прошло")


asyncio.run(main())
