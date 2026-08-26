from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from account_models import AccountIdentity, UnifiedAccount
from max_bot.models import MaxEvent, MaxUser
from models import Event, User
from schedule_models import ScheduleSeries
from statistics_models import StatisticsBaseline


BASELINE_ID = 1
HISTORICAL_USERS = 527
HISTORICAL_EVENTS = 7451
HISTORICAL_WEEK3_RETENTION = 35.0


@dataclass(frozen=True)
class ProductStatistics:
    users: int
    events: int
    week3_retention: float
    actual_users: int
    actual_events: int
    eligible_new_users: int
    retained_new_users: int
    baseline_captured_at: datetime


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _subjects_and_activity(
    db: AsyncSession,
) -> tuple[dict[tuple[str, int], datetime], dict[tuple[str, int], list[datetime]]]:
    accounts = {
        account_id: aware(created_at)
        for account_id, created_at in (await db.execute(
            select(UnifiedAccount.id, UnifiedAccount.created_at)
        )).all()
    }
    identities = (await db.execute(select(
        AccountIdentity.platform,
        AccountIdentity.external_id,
        AccountIdentity.account_id,
        AccountIdentity.created_at,
    ))).all()
    identity_accounts = {
        (platform, int(external_id)): int(account_id)
        for platform, external_id, account_id, _ in identities
    }

    subjects: dict[tuple[str, int], datetime] = {}
    activity: dict[tuple[str, int], list[datetime]] = {}
    for platform, external_id, account_id, created_at in identities:
        key = ("account", int(account_id))
        registered_at = min(
            accounts.get(int(account_id), aware(created_at)), aware(created_at)
        )
        subjects[key] = min(subjects.get(key, registered_at), registered_at)

    telegram_users = (await db.execute(
        select(User.id, User.tg_id, User.created_at)
    )).all()
    telegram_subjects: dict[int, tuple[str, int]] = {}
    for user_id, tg_id, created_at in telegram_users:
        account_id = identity_accounts.get(("telegram", int(tg_id)))
        key = ("account", account_id) if account_id is not None else ("telegram", int(tg_id))
        registered_at = aware(created_at)
        subjects[key] = min(subjects.get(key, registered_at), registered_at)
        telegram_subjects[int(user_id)] = key

    max_users = (await db.execute(
        select(MaxUser.id, MaxUser.max_user_id, MaxUser.created_at)
    )).all()
    max_subjects: dict[int, tuple[str, int]] = {}
    for user_id, max_user_id, created_at in max_users:
        account_id = identity_accounts.get(("max", int(max_user_id)))
        key = ("account", account_id) if account_id is not None else ("max", int(max_user_id))
        registered_at = aware(created_at)
        subjects[key] = min(subjects.get(key, registered_at), registered_at)
        max_subjects[int(user_id)] = key

    for user_id, created_at in (await db.execute(
        select(Event.user_id, Event.created_at)
    )).all():
        key = telegram_subjects.get(int(user_id))
        if key is not None:
            activity.setdefault(key, []).append(aware(created_at))

    for user_id, created_at in (await db.execute(
        select(MaxEvent.user_id, MaxEvent.created_at)
    )).all():
        key = max_subjects.get(int(user_id))
        if key is not None:
            activity.setdefault(key, []).append(aware(created_at))

    for account_id, created_at in (await db.execute(
        select(ScheduleSeries.account_id, ScheduleSeries.created_at)
    )).all():
        key = ("account", int(account_id))
        activity.setdefault(key, []).append(aware(created_at))

    return subjects, activity


async def ensure_statistics_baseline(
    db: AsyncSession,
    now: datetime | None = None,
    actual_users: int | None = None,
    actual_events: int | None = None,
) -> StatisticsBaseline:
    baseline = await db.get(StatisticsBaseline, BASELINE_ID)
    if baseline is not None:
        return baseline
    if actual_users is None or actual_events is None:
        subjects, activity = await _subjects_and_activity(db)
        actual_users = len(subjects)
        actual_events = sum(len(values) for values in activity.values())
    baseline = StatisticsBaseline(
        id=BASELINE_ID,
        captured_at=aware(now or datetime.now(timezone.utc)),
        actual_users_at_start=actual_users,
        actual_events_at_start=actual_events,
        historical_users=HISTORICAL_USERS,
        historical_events=HISTORICAL_EVENTS,
        historical_week3_retention=HISTORICAL_WEEK3_RETENTION,
    )
    db.add(baseline)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        baseline = await db.get(StatisticsBaseline, BASELINE_ID)
        if baseline is None:
            raise
    return baseline


async def product_statistics(
    db: AsyncSession, now: datetime | None = None
) -> ProductStatistics:
    now = aware(now or datetime.now(timezone.utc))
    subjects, activity = await _subjects_and_activity(db)
    actual_users = len(subjects)
    actual_events = sum(len(values) for values in activity.values())
    baseline = await ensure_statistics_baseline(
        db, now, actual_users=actual_users, actual_events=actual_events
    )

    captured_at = aware(baseline.captured_at)
    eligible_new_users = 0
    retained_new_users = 0
    for subject, registered_at in subjects.items():
        registered_at = aware(registered_at)
        if registered_at <= captured_at or registered_at + timedelta(days=21) > now:
            continue
        eligible_new_users += 1
        week3_start = registered_at + timedelta(days=14)
        week3_end = registered_at + timedelta(days=21)
        if any(
            week3_start <= activity_at < week3_end
            for activity_at in activity.get(subject, [])
        ):
            retained_new_users += 1

    historical_retained = (
        baseline.historical_users * baseline.historical_week3_retention / 100
    )
    retention_denominator = baseline.historical_users + eligible_new_users
    retention = (
        (historical_retained + retained_new_users) / retention_denominator * 100
        if retention_denominator else 0.0
    )

    return ProductStatistics(
        users=baseline.historical_users
        + max(0, actual_users - baseline.actual_users_at_start),
        events=baseline.historical_events
        + max(0, actual_events - baseline.actual_events_at_start),
        week3_retention=round(retention, 1),
        actual_users=actual_users,
        actual_events=actual_events,
        eligible_new_users=eligible_new_users,
        retained_new_users=retained_new_users,
        baseline_captured_at=captured_at,
    )
