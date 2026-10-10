"""Calendar import (.ics file, iCal link, CalDAV) through the real Telegram dispatcher and MAX handler."""
import asyncio, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import harness  # env, DB = planiruy_e2e
from harness import TgSim
import database
from database import init_db
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService
from max_flows import FakeMaxClient, MaxSim, joined
from unified_calendar import list_linked_events

HERE = Path(__file__).resolve().parent
GOOGLE_HOLIDAYS = "https://calendar.google.com/calendar/ical/ru.russian%23holiday%40group.v.calendar.google.com/public/basic.ics"
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"   {'✔' if cond else '✘'} {name}{(' — ' + str(detail)[:400]) if detail and not cond else ''}")


def make_ics() -> Path:
    zone = ZoneInfo("Europe/Moscow")
    day = datetime.now(zone).date() + timedelta(days=3)
    stamp = f"{day:%Y%m%d}"
    path = HERE / "ics" / "Работа.ics"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//e2e//RU
X-WR-CALNAME:Работа
BEGIN:VEVENT
UID:sync-{stamp}@e2e
SUMMARY:Синк команды
DTSTART;TZID=Europe/Moscow:{stamp}T110000
DTEND;TZID=Europe/Moscow:{stamp}T113000
RRULE:FREQ=WEEKLY;COUNT=5
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:trip-{stamp}@e2e
SUMMARY:Конференция
DTSTART;VALUE=DATE:{stamp}
DTEND;VALUE=DATE:{(day + timedelta(days=2)):%Y%m%d}
LOCATION:Казань
END:VEVENT
END:VCALENDAR
""", encoding="utf-8")
    return path


def tg_text(entries):
    return "\n".join(text or "" for _, _, text, _ in entries)


async def telegram(ics: Path, user_id: int):
    print("\n=== Telegram ===")
    sim = TgSim(user_id)
    await sim.text("/start")
    out = await sim.text("/import")
    check("TG: /import объясняет три способа", all(word in tg_text(out) for word in ("Файл .ics", "Ссылка iCal", "/caldav")), tg_text(out))
    out = await sim.document(ics)
    text = tg_text(out)
    check("TG: файл .ics импортирован (5 повторов + 1 на весь день)", "добавлено 6 событий" in text, text)
    check("TG: кнопка «Отменить импорт»", any("Отменить импорт" in b.text for b in sim.buttons()), [b.text for b in sim.buttons()])
    events = await list_linked_events_for("telegram", user_id)
    sync = [e for e in events if e.event.title == "Синк команды"]
    check("TG: повторы развёрнуты, напоминание из календаря за 15 минут",
          len(sync) == 5 and all([r - e.timing.start_at.astimezone(r.tzinfo) for r in e.reminders] == [timedelta(minutes=-15)] for e in sync),
          [(e.timing.start_at, e.reminders) for e in sync[:2]])
    conf = next((e for e in events if e.event.title == "Конференция"), None)
    check("TG: событие на весь день и место в описании", conf and conf.timing.all_day and "📍 Казань" in (conf.event.description or ""), conf and conf.event.description)
    out = await sim.document(ics)
    check("TG: повторная загрузка не дублирует", "новых событий нет" in tg_text(out) and "Уже были в календаре: 6" in tg_text(out), tg_text(out))
    out = await sim.text("/history")
    check("TG: импорт одной строкой в истории", "📆 Работа" in tg_text(out) and "Синк команды" not in tg_text(out), tg_text(out))
    out, answers = await sim.press("Отменить импорт", message_id=[mid for _, mid, text, _ in sim.log if text and "добавлено 6 событий" in text][0])
    check("TG: отмена удаляет весь импорт", "удалено 6 событий" in tg_text(out) and not await list_linked_events_for("telegram", user_id), tg_text(out))

    out = await sim.text(f"вот мой календарь праздников {GOOGLE_HOLIDAYS}")
    text = tg_text(out)
    check("TG: импорт по ссылке (праздники Google)", "добавлено" in text and "событ" in text, text)
    out = await sim.text("http://127.0.0.1:8000/calendar/feed/x.ics")
    check("TG: ссылка во внутреннюю сеть отклонена", "внутреннюю сеть" in tg_text(out), tg_text(out))

    out = await sim.text("/caldav")
    check("TG: /caldav просит сервер, логин и пароль приложения", "пароль приложения" in tg_text(out), tg_text(out))
    before = len(sim.requests)
    out = await sim.text("https://example.com\nuser@example.com\nsecret-password")
    deleted = any(type(r).__name__ == "DeleteMessage" for r in sim.requests[before:])
    check("TG: сообщение с паролем удалено из чата", deleted)
    check("TG: понятная ошибка CalDAV", "❌" in tg_text(out) and "CalDAV" in tg_text(out), tg_text(out))
    out = await sim.text("/caldav")
    out = await sim.text("просто текст")
    check("TG: неверный формат — подсказка", "три строки" in tg_text(out), tg_text(out))


async def list_linked_events_for(platform, user_id):
    async with database.async_session_maker() as db:
        return await list_linked_events(db, platform, user_id)


async def max_bot(ics: Path, user_id: int):
    print("\n=== MAX ===")
    client = FakeMaxClient()
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
    sim = MaxSim(handler, client, user_id)
    await sim.text("/start")
    out = await sim.text("/import")
    check("MAX: /import объясняет три способа", "/caldav" in joined(out), joined(out))
    out = await sim.file(ics)
    check("MAX: файл .ics импортирован", "добавлено 6 событий" in joined(out), joined(out))
    events = await list_linked_events_for("max", user_id)
    check("MAX: события в календаре", len(events) == 6, len(events))
    out = await sim.file(ics)
    check("MAX: повторная загрузка не дублирует", "новых событий нет" in joined(out), joined(out))
    out = await sim.text("/history")
    check("MAX: импорт в истории", "Работа" in joined(out), joined(out))
    out = await sim.press("Отменить импорт")
    check("MAX: отмена удаляет весь импорт", "удалено 6 событий" in joined(out) and not await list_linked_events_for("max", user_id), joined(out))
    out = await sim.text(f"webcal://{GOOGLE_HOLIDAYS.split('://', 1)[1]}")
    check("MAX: импорт по ссылке webcal://", "добавлено" in joined(out), joined(out))
    out = await sim.text("/caldav")
    out = await sim.text("https://example.com user@example.com secret-password")
    check("MAX: ошибка CalDAV и просьба удалить пароль, если удалить не вышло",
          "CalDAV" in joined(out) and ("Удалите" in joined(out) or "❌" in joined(out)), joined(out))


def make_formats() -> dict:
    import io, zipfile
    zone = ZoneInfo("Europe/Moscow")
    day = datetime.now(zone).date() + timedelta(days=5)
    d = f"{day:%Y%m%d}"
    folder = HERE / "ics" / "formats"
    folder.mkdir(parents=True, exist_ok=True)
    def cal(body, head=""):
        return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//e2e//RU\r\n" + head + body + "END:VCALENDAR\r\n")
    def ev(uid, title, start, end, extra=""):
        return f"BEGIN:VEVENT\r\nUID:{uid}-{d}\r\nSUMMARY:{title}\r\nDTSTART{start}\r\nDTEND{end}\r\n{extra}END:VEVENT\r\n"
    files = {}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("work@gmail.com.ics", cal(ev("gz1", "Обзор задач", f";TZID=Europe/Moscow:{d}T100000", f";TZID=Europe/Moscow:{d}T103000", "RRULE:FREQ=DAILY;COUNT=3\r\n"), "X-WR-CALNAME:Рабочий Google\r\n"))
        z.writestr("birthdays.ics", cal(ev("gz2", "ДР коллеги", f";VALUE=DATE:{d}", f";VALUE=DATE:{(day + timedelta(days=1)):%Y%m%d}"), "X-WR-CALNAME:Дни рождения\r\n"))
    files["google_export.zip"] = archive.getvalue()
    files["outlook.ics"] = cal(ev("ol", "Совещание Outlook", f';TZID="Russian Standard Time":{d}T150000', f';TZID="Russian Standard Time":{d}T160000')).encode()
    files["windows1251.ics"] = cal(ev("cp", "Пара по истории", f":{d}T090000Z", f":{d}T103000Z")).encode("cp1251")
    files["old_phone.vcs"] = (f"BEGIN:VCALENDAR\r\nVERSION:1.0\r\nBEGIN:VEVENT\r\nSUMMARY:Встреча из старого телефона\r\nDTSTART:{d}T120000Z\r\nDTEND:{d}T130000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode()
    files["tasks.ics"] = cal("BEGIN:VTODO\r\nUID:t\r\nSUMMARY:Купить хлеб\r\nEND:VTODO\r\n").encode()
    files["fake.ics"] = b"<html><body>Login required</body></html>"
    with zipfile.ZipFile(io.BytesIO(), "w") as z:
        pass
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("notes.txt", "hello")
    files["photos.zip"] = empty.getvalue()
    paths = {}
    for name, data in files.items():
        (folder / name).write_bytes(data)
        paths[name] = folder / name
    return paths


async def formats_tg(paths, user_id):
    print("\n=== Telegram: форматы ===")
    sim = TgSim(user_id)
    await sim.text("/start")
    out = tg_text(await sim.document(paths["google_export.zip"]))
    check("TG: ZIP-выгрузка Google — каждый календарь отдельно", "«Рабочий Google»: добавлено 3 события" in out and "«Дни рождения»: добавлено 1 событие" in out, out)
    out = tg_text(await sim.document(paths["outlook.ics"]))
    check("TG: Outlook (виндовый часовой пояс)", "добавлено 1 событие" in out, out)
    out = tg_text(await sim.document(paths["windows1251.ics"]))
    check("TG: файл в Windows-1251", "добавлено 1 событие" in out, out)
    out = tg_text(await sim.document(paths["old_phone.vcs"]))
    check("TG: vCalendar 1.0 (.vcs)", "добавлено 1 событие" in out, out)
    titles = {e.event.title: e for e in await list_linked_events_for("telegram", user_id)}
    check("TG: русские названия и время Outlook верны",
          "Пара по истории" in titles and "Встреча из старого телефона" in titles
          and titles["Совещание Outlook"].timing.start_at.astimezone(ZoneInfo("Europe/Moscow")).hour == 15, list(titles))
    out = tg_text(await sim.document(paths["tasks.ics"]))
    check("TG: файл только с задачами — понятный ответ", "только задачи" in out, out)
    out = tg_text(await sim.document(paths["fake.ics"]))
    check("TG: «календарь», который на деле HTML", "Не похоже на календарь" in out, out)
    out = tg_text(await sim.document(paths["photos.zip"]))
    check("TG: ZIP без календарей", "нет файлов календаря" in out, out)
    out = tg_text(await sim.text("https://www.thunderbird.net/media/caldata/autogen/GermanHolidays.ics"))
    check("TG: имя календаря из ссылки", "«GermanHolidays»" in out, out)


async def formats_max(paths, user_id):
    print("\n=== MAX: форматы ===")
    client = FakeMaxClient()
    sim = MaxSim(FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings()), client, user_id)
    await sim.text("/start")
    out = joined(await sim.file(paths["google_export.zip"]))
    check("MAX: ZIP-выгрузка Google", "«Рабочий Google»: добавлено 3 события" in out and "«Дни рождения»" in out, out)
    out = joined(await sim.file(paths["windows1251.ics"]))
    check("MAX: Windows-1251", "добавлено 1 событие" in out, out)
    out = joined(await sim.file(paths["tasks.ics"]))
    check("MAX: только задачи", "только задачи" in out, out)
    out = joined(await sim.file(paths["photos.zip"]))
    check("MAX: ZIP без календарей", "нет файлов календаря" in out, out)


async def main():
    await init_db()
    ics = make_ics()
    base = 760000000 + int(time.time()) % 100000 * 10
    await telegram(ics, base + 1)
    await max_bot(ics, base + 2)
    paths = make_formats()
    await formats_tg(paths, base + 3)
    await formats_max(paths, base + 4)
    print(f"\n{sum(ok for _, ok in checks)}/{len(checks)} проверок импорта прошло")


asyncio.run(main())
