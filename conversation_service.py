"""Short cross-messenger context for natural-language calendar commands."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import account_id_for
from conversation_models import ConversationTurn


CONTEXT_LIMIT = 6


def _clean(value: str) -> str:
    return " ".join((value or "").split())[:1200]


async def recent_dialogue_context(
    db: AsyncSession,
    platform: str,
    external_id: int,
    limit: int = CONTEXT_LIMIT,
) -> list[str]:
    """Return a compact oldest-to-newest context shared by linked accounts."""
    account_id = await account_id_for(db, platform, external_id)
    rows = (await db.execute(
        select(ConversationTurn)
        .filter(ConversationTurn.account_id == account_id)
        .order_by(ConversationTurn.id.desc())
        .limit(max(1, min(limit, CONTEXT_LIMIT)))
    )).scalars().all()
    return [f"{row.role}: {row.content}" for row in reversed(rows) if row.content]


async def remember_dialogue_turn(
    db: AsyncSession,
    platform: str,
    external_id: int,
    content: str,
    *,
    role: str = "user",
    event_ref: str | None = None,
    commit: bool = False,
) -> ConversationTurn | None:
    """Persist a compact turn; callers decide whether it belongs in a transaction."""
    value = _clean(content)
    if not value:
        return None
    account_id = await account_id_for(db, platform, external_id)
    turn = ConversationTurn(
        account_id=account_id,
        platform=platform,
        role=role[:20],
        content=value,
        event_ref=event_ref,
    )
    db.add(turn)
    await db.flush()
    if commit:
        await db.commit()
    return turn


async def latest_event_ref(
    db: AsyncSession, platform: str, external_id: int
) -> str | None:
    account_id = await account_id_for(db, platform, external_id)
    return (await db.execute(
        select(ConversationTurn.event_ref)
        .filter(
            ConversationTurn.account_id == account_id,
            ConversationTurn.event_ref.is_not(None),
        )
        .order_by(ConversationTurn.id.desc())
        .limit(1)
    )).scalar_one_or_none()


def event_context_text(
    title: str,
    start_at: datetime,
    end_at: datetime,
    *,
    all_day: bool = False,
) -> str:
    kind = "весь день" if all_day else "по времени"
    return (
        f"Событие: {title}; начало {start_at.isoformat()}; "
        f"окончание {end_at.isoformat()}; {kind}."
    )
