import asyncio
import logging
import json
import pytest
from sqlalchemy import select
from database import init_db, async_session_maker
from models import User, Event, Reminder
from services import ActionPipelineService

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger("RealPipelineTest")
pytestmark = pytest.mark.skip(reason="manual integration test requiring production services")

async def test_real():
    print("\n=== STARTING REAL UN-MOCKED END-TO-END PIPELINE TEST ===")
    
    # 1. Initialize DB
    logger.info("Initializing database tables...")
    await init_db()
    
    # 2. Setup query
    query = (
        "Я Алексей, мой тел 89991112233. Срочно запиши что в пятницу в 14:00 лек по матану "
        "в 402 аудитории у Козлова, а еще в субботу дедлайн по лабе ИИ"
    )
    
    test_tg_id = 777333111
    
    pipeline = ActionPipelineService()
    
    async with async_session_maker() as session:
        # Run pipeline
        logger.info("Running pipeline (calling real Natasha model and Yandex AI Studio API)...")
        events = await pipeline.process_text_input(
            user_tg_id=test_tg_id,
            raw_text=query,
            db_session=session
        )
        logger.info(f"Pipeline finished! Created {len(events)} events in draft state.")

    # 3. Read back from DB to verify that the data was COMMITTED successfully!
    logger.info("Querying Database from a fresh session to verify committed data...")
    async with async_session_maker() as session:
        # Fetch user
        user_result = await session.execute(select(User).filter(User.tg_id == test_tg_id))
        user = user_result.scalar_one_or_none()
        print(f"\nUser in DB: {user}")
        
        if user:
            # Fetch events
            events_result = await session.execute(
                select(Event).filter(Event.user_id == user.id)
            )
            db_events = events_result.scalars().all()
            for idx, ev in enumerate(db_events, 1):
                print(f"\nEvent #{idx}: ID={ev.id}, Title='{ev.title}', Deadline={ev.deadline}, Status={ev.status.value}, Description='{ev.description}'")
                
                # Fetch reminders for this event
                rem_result = await session.execute(
                    select(Reminder).filter(Reminder.event_id == ev.id)
                )
                db_rems = rem_result.scalars().all()
                print(f"  Reminders ({len(db_rems)} items):")
                for rem in db_rems:
                    print(f"    - Reminder ID={rem.id}, Remind At={rem.remind_at}, Status={rem.status.value}")
                    
    print("\n=== REAL UN-MOCKED END-TO-END PIPELINE TEST COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(test_real())
