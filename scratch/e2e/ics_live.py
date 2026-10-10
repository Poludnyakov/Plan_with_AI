"""Real public calendars through fetch_ics + parse_ics (the same path as a link sent to the bot)."""
import asyncio, sys
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from calendar_import_service import CalendarImportError, fetch_ics, parse_ics  # noqa: E402

OUT = Path(__file__).resolve().parent / "ics" / "live"
URLS = {
    "Google: праздники России": "https://calendar.google.com/calendar/ical/ru.russian%23holiday%40group.v.calendar.google.com/public/basic.ics",
    "Google: праздники США": "https://calendar.google.com/calendar/ical/en.usa%23holiday%40group.v.calendar.google.com/public/basic.ics",
    "Google: фазы Луны": "https://calendar.google.com/calendar/ical/ht3jlfaac5lfd6263ulfh4tql8%40group.calendar.google.com/public/basic.ics",
    "Google: номера недель (webcal)": "webcal://calendar.google.com/calendar/ical/e_2_ru%23weeknum%40group.v.calendar.google.com/public/basic.ics",
    "Apple iCloud: праздники России": "https://calendars.icloud.com/holidays/ru_ru.ics",
    "Apple iCloud: праздники Германии": "https://calendars.icloud.com/holidays/de_de.ics",
    "Thunderbird: Россия": "https://www.thunderbird.net/media/caldata/autogen/RussianHolidays.ics",
    "Thunderbird: Германия": "https://www.thunderbird.net/media/caldata/autogen/GermanHolidays.ics",
    "officeholidays: Россия": "https://www.officeholidays.com/ics/russia",
    "calendarlabs: Россия": "https://www.calendarlabs.com/ical-calendar/ics/58/Russia_Holidays.ics",
    "Формула 1": "https://files-f1.motorsportcalendars.com/f1-calendar_p1_p2_p3_qualifying_sprint_gp.ics",
    "Футбол: Реал Мадрид (fixtur.es)": "https://ics.fixtur.es/v2/real-madrid.ics",
    "Футбол: Зенит (fixtur.es)": "https://ics.fixtur.es/v2/zenit-st-petersburg.ics",
    "Не календарь: обычная страница": "https://example.com/",
    "Нет такой страницы (404)": "https://calendar.google.com/calendar/ical/nonexistent-xyz%40group.calendar.google.com/public/basic.ics",
}


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for title, url in URLS.items():
        try:
            data, host = await fetch_ics(url)
            (OUT / (title.split(":")[0].replace(" ", "_") + "_" + str(abs(hash(url)) % 10000) + ".ics")).write_bytes(data)
            parsed = parse_ics(data, "Europe/Moscow")
            first = parsed.occurrences[0] if parsed.occurrences else None
            sample = f"{first.title} {first.start:%d.%m.%Y %H:%M}{' весь день' if first.all_day else ''}" if first else "—"
            bad = [o for o in parsed.occurrences if o.end <= o.start or o.start.tzinfo is None]
            print(f"✔ {title}: «{parsed.name or host}», {len(parsed.occurrences)} событий, "
                  f"весь день: {sum(o.all_day for o in parsed.occurrences)}, битых: {parsed.broken}, "
                  f"ошибок времени: {len(bad)} | первое: {sample}")
        except CalendarImportError as error:
            print(f"• {title}: понятный отказ — {error}")
        except Exception as error:
            print(f"✘ {title}: ПАДЕНИЕ {type(error).__name__}: {error}")


asyncio.run(main())
