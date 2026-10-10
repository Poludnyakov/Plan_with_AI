"""A private iCalendar link for Apple, Google and Yandex calendars.

Each account gets a secret URL. Calendar apps subscribe to it and download the
confirmed events and imported classes of all linked messengers (Telegram and
MAX). The link is read-only: changes are made in the bot or the Mini App.
"""

import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event as IcsEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_models import AccountIdentity, CalendarFeedToken
from account_service import account_id_for
from schedule_service import list_schedule_occurrences
from unified_calendar import aware, list_linked_events


FEED_PAST_DAYS = 60
FEED_FUTURE_DAYS = 370
ZONE = ZoneInfo("Europe/Moscow")


async def feed_token(
    db: AsyncSession, platform: str, external_id: int, reset: bool = False
) -> str:
    """The account's secret token; `reset` makes the old link stop working."""
    account_id = await account_id_for(db, platform, external_id)
    row = await db.get(CalendarFeedToken, account_id)
    if row and not reset:
        return row.token
    token = secrets.token_urlsafe(24)
    if row:
        row.token, row.created_at = token, datetime.now(timezone.utc)
    else:
        db.add(CalendarFeedToken(account_id=account_id, token=token))
    await db.commit()
    return token


def feed_links(token: str, base_url: str) -> dict[str, str]:
    host_path = f"{base_url.split('://', 1)[-1].rstrip('/')}/calendar/feed/{token}.ics"
    https = f"https://{host_path}"
    webcal = f"webcal://{host_path}"
    return {
        "https": https,
        "webcal": webcal,
        # Google opens «Add calendar from URL» with the link filled in.
        "google": "https://calendar.google.com/calendar/r?cid=" + quote(webcal, safe=""),
        "connect": f"{base_url.rstrip('/')}/calendar/connect/{token}",
    }


def connect_text(links: dict[str, str]) -> str:
    """The same instructions for Telegram and MAX."""
    return (
        "📆 Подключение к Apple, Google и Яндекс Календарю\n\n"
        "Откройте страницу подключения и выберите свой календарь:\n"
        f"{links['connect']}\n\n"
        "Или добавьте ссылку подписки вручную:\n"
        f"{links['https']}\n\n"
        "• Apple (iPhone, Mac): Календарь → Добавить календарь → Подписка.\n"
        "• Google: Другие календари → «+» → Добавить по URL.\n"
        "• Яндекс: Календарь → «Добавить в мои календари» или «Подписаться» → "
        "вставить ссылку. На странице подключения ссылка копируется сама.\n\n"
        "В календаре появятся подтверждённые события и занятия из расписания. "
        "Изменения делайте в боте: Apple обновляет подписку от 5 минут, Яндекс — "
        "примерно раз в час, Google — раз в несколько часов.\n"
        "Ссылка личная. Если поделились ею случайно, отправьте /calendars new — "
        "старая перестанет работать."
    )


async def _owner(db: AsyncSession, token: str) -> tuple[str, int] | None:
    if not token or len(token) > 64:
        return None
    row = (await db.execute(
        select(CalendarFeedToken).filter(CalendarFeedToken.token == token)
    )).scalar_one_or_none()
    if not row:
        return None
    identity = (await db.execute(
        select(AccountIdentity)
        .filter(AccountIdentity.account_id == row.account_id)
        .order_by(AccountIdentity.id)
    )).scalars().first()
    return (identity.platform, identity.external_id) if identity else None


def _add_event(calendar: Calendar, uid: str, title: str, description: str,
               start: datetime, end: datetime, all_day: bool, stamp: datetime) -> None:
    item = IcsEvent()
    item.add("uid", uid)
    item.add("dtstamp", stamp)
    item.add("summary", title)
    if description:
        item.add("description", description)
    if all_day:
        first = start.astimezone(ZONE).date()
        last = end.astimezone(ZONE).date()
        item.add("dtstart", first)
        item.add("dtend", last if last > first else first + timedelta(days=1))
    else:
        item.add("dtstart", start.astimezone(ZONE))
        item.add("dtend", end.astimezone(ZONE))
    calendar.add_component(item)


async def build_feed(db: AsyncSession, token: str, now: datetime | None = None) -> bytes | None:
    """The .ics file for a token, or None for an unknown or reset link."""
    owner = await _owner(db, token)
    if owner is None:
        return None
    platform, external_id = owner
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=FEED_PAST_DAYS)
    calendar = Calendar()
    calendar.add("prodid", "-//planwithai.ru//planiruy//RU")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "планиИруй!")
    calendar.add("x-wr-timezone", "Europe/Moscow")
    # Apple and Outlook honour these; Google refreshes on its own schedule.
    calendar.add("refresh-interval", timedelta(minutes=15), parameters={"VALUE": "DURATION"})
    calendar.add("x-published-ttl", "PT15M")
    for entry in await list_linked_events(db, platform, external_id):
        if aware(entry.timing.end_at) < since:
            continue
        _add_event(
            calendar, f"{entry.ref.replace(':', '-')}@planwithai.ru",
            entry.event.title, entry.event.description or "",
            aware(entry.timing.start_at), aware(entry.timing.end_at),
            bool(getattr(entry.timing, "all_day", False)), now,
        )
    today = now.astimezone(ZONE).date()
    for item in await list_schedule_occurrences(
        db, platform, external_id,
        today - timedelta(days=FEED_PAST_DAYS), today + timedelta(days=FEED_FUTURE_DAYS),
    ):
        _add_event(
            calendar, f"{item['id'].replace(':', '-')}@planwithai.ru",
            item["title"], item.get("description") or "",
            datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]),
            False, now,
        )
    return calendar.to_ical()
