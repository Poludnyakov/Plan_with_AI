from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_models import AccountIdentity
from account_service import account_id_for
from intelligent_reminders import contextual_fallback, recommend_reminders, valid_times
from reminder_models import ReminderDeliveryState, ReminderPreference


FREQUENCIES = ("minimal", "balanced", "frequent")
FREQUENCY_LIMITS = {"minimal": 1, "balanced": 2, "frequent": 3}
SUMMARY_HOURS = (7, 8, 9, 10)
SNOOZE_MINUTES = (15, 30, 60, 180)


async def reminder_preference(
    db: AsyncSession, platform: str, external_id: int,
) -> ReminderPreference:
    account_id = await account_id_for(db, platform, external_id)
    preference = await db.get(ReminderPreference, account_id)
    if preference:
        return preference
    identities = (await db.execute(
        select(AccountIdentity.platform).filter(AccountIdentity.account_id == account_id)
    )).scalars().all()
    notification_platform = "telegram" if "telegram" in identities else platform
    preference = ReminderPreference(
        account_id=account_id,
        enabled=True,
        frequency="minimal",
        use_ai=False,
        daily_summary=True,
        summary_hour=8,
        snooze_minutes=30,
        notification_platform=notification_platform,
    )
    db.add(preference)
    await db.flush()
    return preference


async def update_reminder_preference(
    db: AsyncSession, platform: str, external_id: int, action: str,
) -> ReminderPreference:
    preference = await reminder_preference(db, platform, external_id)
    if action == "enabled":
        preference.enabled = not preference.enabled
    elif action == "frequency":
        current = preference.frequency if preference.frequency in FREQUENCIES else "minimal"
        preference.frequency = FREQUENCIES[(FREQUENCIES.index(current) + 1) % len(FREQUENCIES)]
    elif action == "ai":
        preference.use_ai = not preference.use_ai
    elif action == "summary":
        preference.daily_summary = not preference.daily_summary
    elif action == "summary_hour":
        current = preference.summary_hour if preference.summary_hour in SUMMARY_HOURS else 8
        preference.summary_hour = SUMMARY_HOURS[(SUMMARY_HOURS.index(current) + 1) % len(SUMMARY_HOURS)]
    elif action == "snooze":
        current = preference.snooze_minutes if preference.snooze_minutes in SNOOZE_MINUTES else 30
        preference.snooze_minutes = SNOOZE_MINUTES[(SNOOZE_MINUTES.index(current) + 1) % len(SNOOZE_MINUTES)]
    elif action == "platform":
        preference.notification_platform = platform
    else:
        raise ValueError("Неизвестная настройка напоминаний")
    await db.commit()
    return preference


async def build_user_reminders(
    db: AsyncSession,
    platform: str,
    external_id: int,
    title: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    timezone_name: str,
    now: datetime | None = None,
    all_day: bool = False,
) -> list[datetime]:
    preference = await reminder_preference(db, platform, external_id)
    if not preference.enabled:
        return []
    limit = FREQUENCY_LIMITS.get(preference.frequency, 1)
    if all_day:
        # A date-only event should always have one useful, predictable reminder.
        day_before = valid_times((start_at - timedelta(days=1),), start_at, now)
        if not preference.use_ai:
            return day_before
        suggestions = await recommend_reminders(
            title, description, start_at, end_at, timezone_name, now
        )
        extras = [value for value in sorted(set(suggestions)) if value not in day_before]
        return sorted(day_before + extras[-limit:])
    if preference.use_ai:
        values = await recommend_reminders(
            title, description, start_at, end_at, timezone_name, now
        )
    else:
        values = contextual_fallback(title, start_at, timezone_name, now)
    return sorted(values)[-limit:]


async def delivery_state(
    db: AsyncSession, platform: str, event_id: int, external_id: int,
) -> ReminderDeliveryState:
    state = await db.get(ReminderDeliveryState, (platform, event_id))
    if state:
        return state
    state = ReminderDeliveryState(
        platform=platform,
        event_id=event_id,
        account_id=await account_id_for(db, platform, external_id),
    )
    db.add(state)
    await db.flush()
    return state


async def acknowledge_delivery(
    db: AsyncSession, platform: str, event_id: int, external_id: int,
) -> ReminderDeliveryState:
    from max_bot.models import MaxReminder
    from models import Reminder, ReminderStatus

    state = await delivery_state(db, platform, event_id, external_id)
    model = Reminder if platform == "telegram" else MaxReminder
    pending_status = ReminderStatus.PENDING if platform == "telegram" else "pending"
    sent_status = ReminderStatus.SENT if platform == "telegram" else "sent"
    overdue = (await db.execute(select(model).filter(
        model.event_id == event_id,
        model.status == pending_status,
        model.remind_at <= datetime.now(timezone.utc),
    ))).scalars().all()
    for reminder in overdue:
        reminder.status = sent_status
    state.awaiting_action = False
    state.acknowledged_at = datetime.now(timezone.utc)
    await db.commit()
    return state


async def snooze_delivery(
    db: AsyncSession,
    platform: str,
    event_id: int,
    external_id: int,
    event_start: datetime,
) -> tuple[datetime, int]:
    from max_bot.models import MaxReminder
    from models import Reminder, ReminderStatus

    preference = await reminder_preference(db, platform, external_id)
    now = datetime.now(timezone.utc)
    start = event_start.replace(tzinfo=timezone.utc) if event_start.tzinfo is None else event_start
    snooze_at = now + timedelta(minutes=preference.snooze_minutes)
    if snooze_at >= start:
        raise ValueError("Событие начнётся раньше выбранного времени повтора.")
    model = Reminder if platform == "telegram" else MaxReminder
    pending_status = ReminderStatus.PENDING if platform == "telegram" else "pending"
    sent_status = ReminderStatus.SENT if platform == "telegram" else "sent"
    pending = (await db.execute(
        select(model).filter(
            model.event_id == event_id,
            model.status == pending_status,
            model.remind_at <= snooze_at,
        )
    )).scalars().all()
    for reminder in pending:
        reminder.status = sent_status
    if platform == "telegram":
        db.add(Reminder(
            event_id=event_id, remind_at=snooze_at,
            status=ReminderStatus.PENDING,
        ))
    else:
        db.add(MaxReminder(event_id=event_id, remind_at=snooze_at, status="pending"))
    state = await delivery_state(db, platform, event_id, external_id)
    state.awaiting_action = False
    state.acknowledged_at = now
    await db.commit()
    return snooze_at, preference.snooze_minutes


def preference_text(preference: ReminderPreference) -> str:
    frequency = {
        "minimal": "тихо · 1",
        "balanced": "баланс · до 2",
        "frequent": "часто · до 3",
    }.get(preference.frequency, "тихо · 1")
    return (
        "🔔 **Настройки напоминаний**\n\n"
        f"Уведомления: {'включены' if preference.enabled else 'выключены'}\n"
        f"Частота на событие: {frequency}\n"
        f"Утренняя сводка: {'включена' if preference.daily_summary else 'выключена'}\n"
        f"Время сводки: {preference.summary_hour:02d}:00\n"
        f"«Напомнить позже»: через {preference.snooze_minutes} мин.\n"
        f"Платный AI-анализ времени: {'включён' if preference.use_ai else 'выключен'}\n\n"
        "Настройки применяются к вашему связанному аккаунту, а сводка приходит "
        "только в выбранный здесь мессенджер."
    )
