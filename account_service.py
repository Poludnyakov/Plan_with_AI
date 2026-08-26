import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from account_models import (
    AccountIdentity,
    AccountLinkCode,
    AccountPreference,
    UnifiedAccount,
    WebLoginTicket,
)
from schedule_models import ScheduleImportDraft, ScheduleSeries
from reminder_models import ReminderDeliveryState, ReminderPreference


PLATFORMS = {"telegram", "max"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def new_short_code(length: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


async def ensure_identity(
    db: AsyncSession, platform: str, external_id: int
) -> AccountIdentity:
    if platform not in PLATFORMS:
        raise ValueError("Unsupported account platform")
    result = await db.execute(
        select(AccountIdentity).filter(
            AccountIdentity.platform == platform,
            AccountIdentity.external_id == int(external_id),
        )
    )
    identity = result.scalar_one_or_none()
    if isinstance(identity, AccountIdentity):
        return identity
    account = UnifiedAccount()
    db.add(account)
    await db.flush()
    identity = AccountIdentity(
        account_id=account.id, platform=platform, external_id=int(external_id)
    )
    db.add_all([identity, AccountPreference(account_id=account.id, intelligent_reminders=True)])
    await db.flush()
    return identity


async def account_id_for(db: AsyncSession, platform: str, external_id: int) -> int:
    return (await ensure_identity(db, platform, external_id)).account_id


async def linked_identities(
    db: AsyncSession, platform: str, external_id: int
) -> dict[str, int]:
    account_id = await account_id_for(db, platform, external_id)
    result = await db.execute(
        select(AccountIdentity).filter(AccountIdentity.account_id == account_id)
    )
    return {item.platform: item.external_id for item in result.scalars().all()}


async def create_link_code(db: AsyncSession, platform: str, external_id: int) -> str:
    identity = await ensure_identity(db, platform, external_id)
    for _ in range(10):
        code = new_short_code()
        if await db.get(AccountLinkCode, code) is None:
            break
    else:
        raise RuntimeError("Could not allocate an account link code")
    db.add(AccountLinkCode(
        code=code,
        source_identity_id=identity.id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await db.commit()
    return code


async def consume_link_code(
    db: AsyncSession, platform: str, external_id: int, code: str
) -> dict[str, int]:
    link = await db.get(AccountLinkCode, code.strip().upper())
    now = datetime.now(timezone.utc)
    if not link or link.used_at is not None or aware(link.expires_at) < now:
        raise ValueError("Код не найден или срок его действия истёк.")
    source = await db.get(AccountIdentity, link.source_identity_id)
    target = await ensure_identity(db, platform, external_id)
    if source.platform == target.platform and source.external_id != target.external_id:
        raise ValueError("Нельзя объединить две учётные записи одной платформы.")
    if source.account_id != target.account_id:
        source_platforms = {
            item.platform for item in (await db.execute(
                select(AccountIdentity).filter(AccountIdentity.account_id == source.account_id)
            )).scalars().all()
        }
        target_identities = (await db.execute(
            select(AccountIdentity).filter(AccountIdentity.account_id == target.account_id)
        )).scalars().all()
        if any(item.platform in source_platforms for item in target_identities):
            raise ValueError("В этих аккаунтах уже подключена одинаковая платформа.")
        old_account_id = target.account_id
        source_reminders = await db.get(ReminderPreference, source.account_id)
        target_reminders = await db.get(ReminderPreference, old_account_id)
        if target_reminders and not source_reminders:
            db.add(ReminderPreference(
                account_id=source.account_id,
                enabled=target_reminders.enabled,
                frequency=target_reminders.frequency,
                use_ai=target_reminders.use_ai,
                daily_summary=target_reminders.daily_summary,
                summary_hour=target_reminders.summary_hour,
                snooze_minutes=target_reminders.snooze_minutes,
                notification_platform=target_reminders.notification_platform,
                last_summary_date=target_reminders.last_summary_date,
            ))
            await db.flush()
        await db.execute(
            update(AccountIdentity)
            .where(AccountIdentity.account_id == old_account_id)
            .values(account_id=source.account_id)
        )
        # Recurring schedules belong to the unified account itself (ordinary
        # events remain attached to their platform users). Preserve both when
        # two existing accounts are linked.
        await db.execute(
            update(ScheduleSeries)
            .where(ScheduleSeries.account_id == old_account_id)
            .values(account_id=source.account_id)
        )
        await db.execute(
            update(ScheduleImportDraft)
            .where(ScheduleImportDraft.account_id == old_account_id)
            .values(account_id=source.account_id)
        )
        await db.execute(
            update(ReminderDeliveryState)
            .where(ReminderDeliveryState.account_id == old_account_id)
            .values(account_id=source.account_id)
        )
    link.used_at = now
    await db.commit()
    return await linked_identities(db, platform, external_id)


async def intelligent_reminders_enabled(
    db: AsyncSession, platform: str, external_id: int
) -> bool:
    account_id = await account_id_for(db, platform, external_id)
    preference = await db.get(AccountPreference, account_id)
    if not preference:
        preference = AccountPreference(account_id=account_id, intelligent_reminders=True)
        db.add(preference)
        await db.flush()
    return preference.intelligent_reminders


async def set_intelligent_reminders(
    db: AsyncSession, platform: str, external_id: int, enabled: bool
) -> bool:
    account_id = await account_id_for(db, platform, external_id)
    preference = await db.get(AccountPreference, account_id)
    if not preference:
        preference = AccountPreference(account_id=account_id)
        db.add(preference)
    preference.intelligent_reminders = enabled
    await db.commit()
    return enabled


async def create_web_login_ticket(db: AsyncSession, platform: str) -> WebLoginTicket:
    if platform not in PLATFORMS:
        raise ValueError("Unsupported login platform")
    ticket = WebLoginTicket(
        token=secrets.token_urlsafe(32),
        short_code=new_short_code(),
        platform=platform,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(ticket)
    await db.commit()
    return ticket


async def complete_web_login(
    db: AsyncSession, platform: str, external_id: int, short_code: str
) -> WebLoginTicket:
    result = await db.execute(
        select(WebLoginTicket).filter(
            WebLoginTicket.short_code == short_code.strip().upper(),
            WebLoginTicket.platform == platform,
        )
    )
    ticket = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not ticket or ticket.completed_at or aware(ticket.expires_at) < now:
        raise ValueError("Код входа не найден или срок его действия истёк.")
    identity = await ensure_identity(db, platform, external_id)
    ticket.account_id = identity.account_id
    ticket.completed_at = now
    await db.commit()
    return ticket
