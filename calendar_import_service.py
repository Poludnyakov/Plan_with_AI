"""Import from other calendars: .ics files, iCal links (webcal/https) and CalDAV.

Shared by Telegram, MAX and the Mini App. Repeating events are unfolded for a
year ahead, past ones are skipped. Each import is one entry in the history:
deleting it removes every event it created and nothing else; importing the
same calendar again adds only what is new.
"""

import asyncio
import io
import ipaddress
import re
import socket
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import recurring_ical_events
from icalendar import Calendar
from icalendar.cal import Component
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import account_id_for
from calendar_import_models import CalendarImport, CalendarImportItem
from intelligent_reminders import contextual_fallback
from interval_models import EventTiming
from max_bot.models import MaxEvent, MaxEventTiming, MaxReminder
from models import Event, EventStatus, Reminder, ReminderStatus
from reminder_service import FREQUENCY_LIMITS, all_day_reminder, reminder_preference
from unified_calendar import aware, ensure_platform_user


MAX_ICS_BYTES = 5 * 1024 * 1024
MAX_EVENTS = 1000
HORIZON = timedelta(days=366)
KIND_LABELS = {"file": "из файла", "link": "по ссылке", "caldav": "по CalDAV"}
ICS_LINK_RE = re.compile(
    r"(?:webcals?://\S+|https?://\S+?(?:\.ics|\.ical|/ical/\S*|/export/ics\S*|[?&]format=ics\S*)(?=[\s)>\]]|$))",
    re.IGNORECASE,
)
# Address book shortcuts: people type the service, not the server.
CALDAV_SERVERS = {
    "yandex": "https://caldav.yandex.ru",
    "яндекс": "https://caldav.yandex.ru",
    "icloud": "https://caldav.icloud.com",
    "apple": "https://caldav.icloud.com",
    "mail.ru": "https://calendar.mail.ru",
}
OWN_UID_SUFFIX = "@planwithai.ru"


class CalendarImportError(ValueError):
    """A message for the user: what is wrong and how to fix it."""


@dataclass
class Occurrence:
    key: str
    title: str
    description: str
    start: datetime
    end: datetime
    all_day: bool = False
    alarms: list[datetime] = field(default_factory=list)


@dataclass
class ParsedCalendar:
    name: str | None
    occurrences: list[Occurrence]
    skipped_past: int = 0
    truncated: bool = False
    broken: int = 0  # events with errors in the file, skipped


@dataclass
class ImportResult:
    import_id: int | None
    source_name: str
    created: int
    duplicates: int
    skipped_past: int = 0
    truncated: bool = False
    first: datetime | None = None
    last: datetime | None = None
    broken: int = 0


def find_ics_link(text: str) -> str | None:
    match = ICS_LINK_RE.search(text or "")
    return match.group(0).rstrip(".,;") if match else None


def is_ics_filename(name: str | None, mime: str | None = None) -> bool:
    """Calendar files; .zip too: Google exports calendars as a zip of .ics files."""
    return (name or "").casefold().endswith((".ics", ".ical", ".ifb", ".icalendar", ".vcs", ".zip")) or (
        (mime or "").casefold() in {"text/calendar", "application/ics", "text/x-vcalendar"}
    )


def calendar_files(content: bytes, filename: str) -> list[tuple[str, bytes]]:
    """(name, data) of each calendar: the file itself or the .ics files of a zip."""
    if not content.startswith(b"PK"):
        return [(filename, content)]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = [
                item for item in archive.infolist()
                if not item.is_dir() and item.filename.casefold().endswith((".ics", ".ical", ".vcs"))
                and not item.filename.startswith("__MACOSX/")
            ][:20]
            if sum(item.file_size for item in members) > 4 * MAX_ICS_BYTES:
                raise CalendarImportError("Архив слишком большой: максимум 20 МБ календарей.")
            files = [(item.filename.rsplit("/", 1)[-1], archive.read(item)) for item in members]
    except zipfile.BadZipFile as error:
        raise CalendarImportError("Архив повреждён.") from error
    if not files:
        raise CalendarImportError("В архиве нет файлов календаря .ics.")
    return files


def _decoded(data: bytes) -> bytes:
    """UTF-8 for the parser: drop a BOM, accept Windows-1251 exports."""
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace").encode("utf-8")


def _text(component, name: str) -> str:
    value = component.get(name)
    return str(value).strip() if value is not None else ""


def _moment(value, zone: ZoneInfo) -> tuple[datetime, bool]:
    """A start or end as an aware datetime and whether it is a whole day."""
    if isinstance(value, datetime):
        return (value.replace(tzinfo=zone) if value.tzinfo is None else value), False
    if isinstance(value, date):
        return datetime.combine(value, time(0), zone), True
    raise CalendarImportError("В календаре есть событие без даты.")


def _alarms(component, start: datetime, end: datetime, recurring: bool) -> list[datetime]:
    times = []
    for alarm in component.walk("VALARM"):
        trigger = alarm.get("trigger")
        if trigger is None:
            continue
        value = trigger.dt
        if isinstance(value, timedelta):
            related = str(trigger.params.get("RELATED", "START")).upper()
            times.append((end if related == "END" else start) + value)
        elif isinstance(value, datetime) and not recurring:
            times.append(value if value.tzinfo else value.replace(tzinfo=start.tzinfo))
    return times


def _expand(calendar: Calendar, start: datetime, end: datetime) -> tuple[list, int]:
    """Occurrences in the window and the number of event series that could not be read.

    One broken event must not stop the rest: when the whole calendar fails, each
    series (an UID with its changed occurrences) is unfolded on its own.
    """
    try:
        return list(recurring_ical_events.of(calendar, skip_bad_series=True).between(start, end)), 0
    except Exception:
        pass
    timezones = [item for item in calendar.subcomponents if item.name == "VTIMEZONE"]
    series: dict[str, list[Component]] = {}
    for index, event in enumerate(item for item in calendar.subcomponents if item.name == "VEVENT"):
        series.setdefault(_text(event, "UID") or f"#{index}", []).append(event)
    items, broken = [], 0
    for events in series.values():
        single = Calendar()
        for component in timezones + events:
            single.add_component(component)
        try:
            items.extend(recurring_ical_events.of(single, skip_bad_series=True).between(start, end))
        except Exception:
            plain = _plain_event(events)
            if plain is None:
                broken += 1
            else:
                items.append(plain)  # parse_ics keeps it only inside the window
    return items, broken


def _plain_event(events: list[Component]) -> Component | None:
    """A one-off event read without the recurrence library (it may choke on a
    broken time zone of the file); a repeated start keeps its first value."""
    if len(events) != 1 or any(events[0].get(name) is not None for name in ("RRULE", "RDATE")):
        return None
    event = events[0]
    for name in ("DTSTART", "DTEND"):
        value = event.get(name)
        if isinstance(value, list):
            event.pop(name)
            event.add(name, value[0].dt)
    try:
        event["DTSTART"].dt
    except Exception:
        return None
    return event


def parse_ics(
    data: bytes, timezone_name: str = "Europe/Moscow", now: datetime | None = None
) -> ParsedCalendar:
    """Future occurrences of the events in an iCalendar file, for a year ahead."""
    if len(data) > MAX_ICS_BYTES:
        raise CalendarImportError("Календарь слишком большой: максимум 5 МБ.")
    data = _decoded(data)
    if b"BEGIN:VCALENDAR" not in data.upper() and b"BEGIN:VEVENT" in data.upper():
        # A bare event, as some mail clients save it: wrap it into a calendar.
        data = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//planiruy//import//RU\r\n" + data.strip() + b"\r\nEND:VCALENDAR\r\n"
    if b"BEGIN:VCALENDAR" not in data.upper():
        raise CalendarImportError(
            "Не похоже на календарь iCalendar (.ics). Проверьте файл или ссылку."
        )
    try:
        calendars = Calendar.from_ical(data, multiple=True)
    except Exception:
        # A stray line that is not «NAME:value» (or a folded continuation) breaks the
        # strict parser; drop such lines and try once more.
        lines = [
            line for line in re.split(rb"\r?\n", data)
            if line[:1] in (b" ", b"\t") or b":" in line
        ]
        try:
            calendars = Calendar.from_ical(b"\r\n".join(lines), multiple=True)
        except Exception as error:
            raise CalendarImportError(
                "Не похоже на календарь iCalendar (.ics). Проверьте файл или ссылку."
            ) from error
    if not calendars:
        raise CalendarImportError("В файле нет календаря.")
    if not any(True for calendar in calendars for _ in calendar.walk("VEVENT")):
        raise CalendarImportError("В файле нет событий — только задачи или настройки. Задачи не загружаются.")
    zone = ZoneInfo(timezone_name or "Europe/Moscow")
    current = aware(now or datetime.now(timezone.utc))
    window_end = current + HORIZON
    name, occurrences, skipped, broken, seen = None, [], 0, 0, set()
    # From the start of the local day: today's earlier events are counted as past.
    window_start = datetime.combine(current.astimezone(zone).date(), time(0), zone)
    for calendar in calendars:
        name = name or _text(calendar, "X-WR-CALNAME") or None
        if not any(True for _ in calendar.walk("VEVENT")):
            continue
        items, failed = _expand(calendar, window_start, window_end)
        broken += failed
        for component in items:
            if component.name != "VEVENT" or _text(component, "STATUS").upper() == "CANCELLED":
                continue
            uid = _text(component, "UID")
            if uid.endswith(OWN_UID_SUFFIX):
                continue  # our own subscription link: these events are already here
            try:
                start, all_day = _moment(component.get("dtstart").dt, zone)
                if component.get("dtend") is not None:
                    end, _ = _moment(component.get("dtend").dt, zone)
                elif component.get("duration") is not None:
                    end = start + component.get("duration").dt
                else:
                    end = start + (timedelta(days=1) if all_day else timedelta(0))
            except Exception:
                broken += 1
                continue
            if start >= window_end:
                continue
            if end <= start:
                end = start + (timedelta(days=1) if all_day else timedelta(minutes=30))
            if end <= current:
                skipped += 1
                continue
            key = f"{uid or _text(component, 'SUMMARY')}|{start.astimezone(timezone.utc).isoformat()}"
            if key in seen:
                continue
            seen.add(key)
            description = _text(component, "DESCRIPTION")
            location = _text(component, "LOCATION")
            if location:
                description = (description + "\n" if description else "") + f"📍 {location}"
            occurrences.append(Occurrence(
                key=key[:600],
                title=(_text(component, "SUMMARY") or "Без названия")[:255],
                description=description[:4000],
                start=start, end=end, all_day=all_day,
                alarms=_alarms(component, start, end, bool(component.get("rrule"))),
            ))
    occurrences.sort(key=lambda item: item.start)
    if broken and not occurrences:
        raise CalendarImportError("Не удалось прочитать события: файл календаря повреждён.")
    return ParsedCalendar(name, occurrences[:MAX_EVENTS], skipped, len(occurrences) > MAX_EVENTS, broken)


def _public_host(url: str) -> str:
    """Only internet addresses: a link must not reach the server's own network."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CalendarImportError("Нужна ссылка вида https://… или webcal://…")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except OSError as error:
        raise CalendarImportError("Сервер календаря не найден. Проверьте адрес.") from error
    if not addresses or not all(ipaddress.ip_address(item).is_global for item in addresses):
        raise CalendarImportError("Эта ссылка ведёт во внутреннюю сеть и не может быть загружена.")
    return parts.hostname


def normalize_link(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcals://"):
        return "https://" + url[10:]
    if url.lower().startswith("webcal://"):
        return "https://" + url[9:]
    return url


async def fetch_ics(url: str) -> tuple[bytes, str]:
    """Download an iCal link (webcal is https) with a size limit."""
    url = normalize_link(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(5):
            host = await asyncio.to_thread(_public_host, url)
            async with client.stream("GET", url, headers={"User-Agent": "planiruy-calendar-import"}) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers.get("location", "")))
                    continue
                if response.status_code in {401, 403}:
                    raise CalendarImportError("Календарь закрыт. Нужна публичная или секретная ссылка iCal.")
                if response.status_code == 404:
                    raise CalendarImportError("По ссылке ничего нет (404). Проверьте адрес.")
                if response.is_error:
                    raise CalendarImportError(f"Сервер календаря ответил ошибкой {response.status_code}.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ICS_BYTES:
                        raise CalendarImportError("Календарь слишком большой: максимум 5 МБ.")
                    chunks.append(chunk)
                return b"".join(chunks), host
    raise CalendarImportError("Слишком много перенаправлений по ссылке.")


def caldav_url(server: str) -> str:
    server = server.strip().rstrip("/")
    if server.casefold() in CALDAV_SERVERS:
        return CALDAV_SERVERS[server.casefold()]
    if "://" not in server:
        server = "https://" + server
    if server.lower().startswith("http://"):
        raise CalendarImportError("CalDAV подключается только по https.")
    return server


def parse_caldav_credentials(text: str) -> tuple[str, str, str] | None:
    """«server login password» in lines or separated by spaces."""
    parts = [part for part in re.split(r"\s+", (text or "").strip()) if part]
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _caldav_download(url: str, username: str, password: str, start: datetime, end: datetime):
    import caldav
    from caldav.lib import error as dav_error

    client = caldav.DAVClient(
        url=url, username=username, password=password, timeout=30, enable_rfc6764=False,
    )
    try:
        calendars = client.principal().calendars()
    except dav_error.AuthorizationError as error:
        raise CalendarImportError(
            "Сервер не принял логин или пароль. Для Яндекса и iCloud нужен пароль приложения."
        ) from error
    except dav_error.DAVError as error:
        raise CalendarImportError("Не удалось открыть календари на сервере CalDAV.") from error
    result = []
    for calendar in calendars:
        try:
            name = calendar.get_display_name() or "Календарь"
        except Exception:
            name = "Календарь"
        try:
            objects = calendar.search(event=True, start=start, end=end)
        except Exception:
            try:
                objects = calendar.events()
            except Exception:
                continue  # a task list or a calendar without events
        data = [obj.data.encode() if isinstance(obj.data, str) else obj.data for obj in objects if obj.data]
        if data:
            result.append((name, data))
    return result


async def fetch_caldav(
    server: str, username: str, password: str, now: datetime | None = None
) -> list[tuple[str, list[bytes]]]:
    """Every event calendar of the account: (name, iCalendar objects)."""
    url = caldav_url(server)
    if "google.com" in url:
        raise CalendarImportError(
            "Google не пускает по паролю. Скопируйте в настройках календаря "
            "«Секретный адрес в формате iCal» и пришлите ссылку."
        )
    await asyncio.to_thread(_public_host, url)
    current = aware(now or datetime.now(timezone.utc))
    try:
        return await asyncio.wait_for(asyncio.to_thread(
            _caldav_download, url, username, password, current - timedelta(days=1), current + HORIZON,
        ), timeout=90)
    except CalendarImportError:
        raise
    except asyncio.TimeoutError as error:
        raise CalendarImportError("Сервер CalDAV не ответил вовремя.") from error
    except Exception as error:
        raise CalendarImportError("Не удалось подключиться к серверу CalDAV. Проверьте адрес.") from error


def merge_parsed(parts: list[ParsedCalendar]) -> ParsedCalendar:
    seen, occurrences = set(), []
    for part in parts:
        for item in part.occurrences:
            if item.key not in seen:
                seen.add(item.key)
                occurrences.append(item)
    occurrences.sort(key=lambda item: item.start)
    return ParsedCalendar(
        next((part.name for part in parts if part.name), None), occurrences[:MAX_EVENTS],
        sum(part.skipped_past for part in parts),
        len(occurrences) > MAX_EVENTS or any(part.truncated for part in parts),
        sum(part.broken for part in parts),
    )


async def _active_keys(db: AsyncSession, account_id: int) -> set[str]:
    rows = await db.execute(
        select(CalendarImportItem.uid_key)
        .join(CalendarImport, CalendarImport.id == CalendarImportItem.import_id)
        .filter(CalendarImport.account_id == account_id, CalendarImport.status == "imported")
    )
    return set(rows.scalars().all())


def _reminders(preference, item: Occurrence, zone: str, now: datetime) -> list[datetime]:
    """The calendar's own alarms; otherwise the free local rules, never AI per event."""
    if not preference.enabled:
        return []
    limit = item.end if item.all_day else item.start
    if item.alarms:
        values = [value for value in item.alarms if now < value <= limit]
    elif item.all_day:
        values = all_day_reminder(item.start, item.end, zone, now)
    else:
        values = contextual_fallback(item.title, item.start, zone, now)[-FREQUENCY_LIMITS.get(preference.frequency, 1):]
    return sorted({aware(value) for value in values})


async def import_calendar(
    db: AsyncSession,
    platform: str,
    external_id: int,
    kind: str,
    source_name: str,
    parsed: ParsedCalendar,
    now: datetime | None = None,
) -> ImportResult:
    """Create the events of one calendar as a single history entry."""
    current = aware(now or datetime.now(timezone.utc))
    user = await ensure_platform_user(db, platform, external_id)
    account_id = await account_id_for(db, platform, external_id)
    known = await _active_keys(db, account_id)
    fresh = [item for item in parsed.occurrences if item.key not in known]
    name = (source_name or parsed.name or "Календарь").strip()[:255]
    result = ImportResult(
        None, name, len(fresh), len(parsed.occurrences) - len(fresh),
        parsed.skipped_past, parsed.truncated,
        fresh[0].start if fresh else None, max((item.end for item in fresh), default=None),
        parsed.broken,
    )
    if not fresh:
        return result
    record = CalendarImport(
        account_id=account_id, platform=platform, external_id=external_id,
        kind=kind, source_name=name,
    )
    db.add(record)
    await db.flush()
    preference = await reminder_preference(db, platform, external_id)
    zone = user.timezone or "Europe/Moscow"
    for item in fresh:
        if platform == "telegram":
            event = Event(
                user_id=user.id, title=item.title, description=item.description,
                deadline=item.end, status=EventStatus.CONFIRMED,
            )
        else:
            event = MaxEvent(
                user_id=user.id, title=item.title, description=item.description,
                deadline=item.end, status="confirmed",
            )
        db.add(event)
        await db.flush()
        timing_model = EventTiming if platform == "telegram" else MaxEventTiming
        db.add(timing_model(event_id=event.id, start_at=item.start, end_at=item.end, all_day=item.all_day))
        for remind_at in _reminders(preference, item, zone, current):
            if platform == "telegram":
                db.add(Reminder(event_id=event.id, remind_at=remind_at, status=ReminderStatus.PENDING))
            else:
                db.add(MaxReminder(event_id=event.id, remind_at=remind_at, status="pending"))
        db.add(CalendarImportItem(
            import_id=record.id, source=platform, event_id=event.id, uid_key=item.key,
        ))
    await db.commit()
    result.import_id = record.id
    return result


async def owned_import(
    db: AsyncSession, platform: str, external_id: int, import_id: int
) -> CalendarImport | None:
    account_id = await account_id_for(db, platform, external_id)
    record = await db.get(CalendarImport, import_id)
    return record if record and record.account_id == account_id else None


async def undo_calendar_import(
    db: AsyncSession, platform: str, external_id: int, import_id: int
) -> tuple[str, int]:
    """Delete every event the import created; other events stay."""
    record = await owned_import(db, platform, external_id, import_id)
    if not record:
        return "missing", 0
    if record.status != "imported":
        return "already_undone", 0
    items = (await db.execute(
        select(CalendarImportItem).filter(CalendarImportItem.import_id == record.id)
    )).scalars().all()
    removed = 0
    for source in ("telegram", "max"):
        ids = [item.event_id for item in items if item.source == source]
        if not ids:
            continue
        model = Event if source == "telegram" else MaxEvent
        timing = EventTiming if source == "telegram" else MaxEventTiming
        events = (await db.execute(select(model).filter(model.id.in_(ids)))).scalars().all()
        # The database cascades this too; explicit so that no orphan time remains anywhere.
        await db.execute(delete(timing).where(timing.event_id.in_([event.id for event in events])))
        for event in events:
            await db.delete(event)
            removed += 1
    record.status = "undone"
    await db.commit()
    return "undone", removed


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def events_word(count: int) -> str:
    return f"{count} {_plural(count, 'событие', 'события', 'событий')}"


def result_text(
    results: list[ImportResult], timezone_name: str = "Europe/Moscow", in_app: bool = False
) -> str:
    """The same summary for Telegram, MAX and the Mini App (in_app: no chat buttons there)."""
    zone = ZoneInfo(timezone_name or "Europe/Moscow")
    lines = []
    for result in results:
        if result.created:
            span = ""
            if result.first and result.last:
                span = f" ({result.first.astimezone(zone):%d.%m.%Y}–{(result.last - timedelta(seconds=1)).astimezone(zone):%d.%m.%Y})"
            line = f"📆 «{result.source_name}»: добавлено {events_word(result.created)}{span}."
        elif result.duplicates:
            line = f"📆 «{result.source_name}»: новых событий нет."
        else:
            line = f"📆 «{result.source_name}»: будущих событий на ближайший год нет — прошедшие не загружаются."
        if result.duplicates:
            line += f" Уже были в календаре: {result.duplicates}."
        if result.truncated:
            line += f" Взяты первые {MAX_EVENTS}."
        if result.broken:
            line += f" ⚠️ Пропущено событий с ошибками в файле: {result.broken}."
        lines.append(line)
    if any(result.created for result in results):
        lines.append(
            "Повторяющиеся события добавлены на год вперёд, прошедшие пропущены. "
            + ("Удалить импорт целиком можно на вкладке «История»." if in_app
               else "Удалить импорт целиком можно кнопкой ниже или в /history.")
        )
    return "\n".join(lines)


async def import_from_file(
    db: AsyncSession, platform: str, external_id: int, content: bytes, filename: str,
    now: datetime | None = None,
) -> list[ImportResult]:
    """One file, or each calendar of a zip (Google exports one .ics per calendar)."""
    user = await ensure_platform_user(db, platform, external_id)
    results = []
    for name, data in calendar_files(content, filename):
        parsed = parse_ics(data, user.timezone, now)
        name = parsed.name or re.sub(r"\.(ics|ical|ifb|icalendar|vcs)$", "", name or "", flags=re.I) or "Календарь"
        results.append(await import_calendar(db, platform, external_id, "file", name, parsed, now))
    return results


def link_name(url: str, host: str) -> str:
    """A readable name from the link when the calendar has none: GermanHolidays.ics → GermanHolidays."""
    stem = urlsplit(normalize_link(url)).path.rstrip("/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(ics|ical|xml)$", "", stem, flags=re.I)
    if 3 <= len(stem) <= 60 and stem.casefold() not in {"basic", "calendar", "ics", "export", "feed", "public", "private"}:
        return stem
    return host


async def import_from_link(
    db: AsyncSession, platform: str, external_id: int, url: str, now: datetime | None = None,
) -> list[ImportResult]:
    user = await ensure_platform_user(db, platform, external_id)
    content, host = await fetch_ics(url)
    parsed = parse_ics(content, user.timezone, now)
    return [await import_calendar(db, platform, external_id, "link", parsed.name or link_name(url, host), parsed, now)]


async def import_from_caldav(
    db: AsyncSession, platform: str, external_id: int, server: str, username: str, password: str,
    now: datetime | None = None,
) -> list[ImportResult]:
    """Each calendar of the account is a separate history entry."""
    user = await ensure_platform_user(db, platform, external_id)
    calendars = await fetch_caldav(server, username, password, now)
    if not calendars:
        raise CalendarImportError("На сервере не нашлось календарей с событиями.")
    results = []
    for name, objects in calendars:
        parts = []
        for data in objects:
            try:
                parts.append(parse_ics(data, user.timezone, now))
            except CalendarImportError:
                continue  # one broken event must not stop the rest
        parsed = merge_parsed(parts)
        if parsed.occurrences:
            results.append(await import_calendar(db, platform, external_id, "caldav", name, parsed, now))
    if not results:
        raise CalendarImportError("В календарях нет будущих событий на ближайший год.")
    return results


IMPORT_HELP = (
    "📆 Импорт из другого календаря\n\n"
    "• Файл .ics — просто пришлите его сюда. Выгрузить можно в Google "
    "(Настройки → Импорт и экспорт → Экспорт), в Яндексе (настройки календаря → Экспорт), "
    "в Outlook и других календарях.\n"
    "• Ссылка iCal — пришлите ссылку .ics или webcal://. В Google это «Закрытый адрес "
    "в формате iCal» в настройках календаря, в Яндексе — ссылка из «Экспорт».\n"
    "• CalDAV (Яндекс, iCloud, Nextcloud, Mail.ru) — команда /caldav.\n\n"
    "Повторяющиеся события добавятся на год вперёд, прошедшие — нет. Если загрузить "
    "тот же календарь ещё раз, добавятся только новые события. Удалить импорт целиком "
    "можно в /history или в календаре на вкладке «История»."
)
CALDAV_HELP = (
    "🔗 Подключение по CalDAV\n\n"
    "Пришлите одним сообщением три строки:\n"
    "сервер\nлогин\nпароль приложения\n\n"
    "Например:\nyandex\nname@yandex.ru\nabcdabcdabcdabcd\n\n"
    "Сервер: yandex, icloud или адрес вида https://cloud.example.com/remote.php/dav.\n"
    "Нужен именно пароль приложения: Яндекс ID → Безопасность → Пароли приложений; "
    "Apple ID → Вход и безопасность → Пароли приложений.\n"
    "Пароль используется один раз для загрузки и не сохраняется. "
    "Google по паролю не подключается — для него пришлите закрытую ссылку iCal."
)
CALDAV_WAIT = timedelta(minutes=10)
_caldav_waiting: dict[tuple[str, int], datetime] = {}


def wait_for_caldav(platform: str, external_id: int) -> None:
    """The next message of this user is CalDAV access (kept in memory only)."""
    _caldav_waiting[(platform, external_id)] = datetime.now(timezone.utc) + CALDAV_WAIT


def expects_caldav(platform: str, external_id: int) -> bool:
    deadline = _caldav_waiting.get((platform, external_id))
    if deadline and deadline < datetime.now(timezone.utc):
        _caldav_waiting.pop((platform, external_id), None)
        return False
    return deadline is not None


def stop_waiting_caldav(platform: str, external_id: int) -> None:
    _caldav_waiting.pop((platform, external_id), None)


def undo_payload(results: list[ImportResult]) -> str | None:
    """Callback data «calimp_undo:3,4» for the imports that added something."""
    ids = [str(result.import_id) for result in results if result.import_id]
    if not ids:
        return None
    payload = "calimp_undo:" + ",".join(ids)
    return payload if len(payload) <= 64 else None


async def undo_imports(
    db: AsyncSession, platform: str, external_id: int, payload: str
) -> str:
    removed, found = 0, False
    for raw in payload.split(":", 1)[1].split(","):
        try:
            state, count = await undo_calendar_import(db, platform, external_id, int(raw))
        except ValueError:
            continue
        found = found or state != "missing"
        removed += count
    if not found:
        return "Импорт не найден."
    if not removed:
        return "Этот импорт уже удалён."
    return f"↩️ Импорт отменён: удалено {events_word(removed)}. Остальные события не затронуты."
