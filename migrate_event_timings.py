"""Create interval storage and backfill legacy point-in-time events."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

import interval_models
from database import async_session_maker, init_db
from interval_models import EventTiming
from models import Event


async def migrate() -> None:
    await init_db()
    async with async_session_maker() as session:
        result = await session.execute(
            select(Event)
            .outerjoin(EventTiming, EventTiming.event_id == Event.id)
            .filter(EventTiming.event_id.is_(None))
        )
        events = result.scalars().all()
        for event in events:
            session.add(EventTiming(
                event_id=event.id,
                start_at=event.deadline - timedelta(minutes=30),
                end_at=event.deadline,
            ))
        await session.commit()
        print(f"Backfilled event timings: {len(events)}")


if __name__ == "__main__":
    asyncio.run(migrate())
