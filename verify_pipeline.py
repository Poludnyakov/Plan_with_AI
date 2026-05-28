import asyncio
import logging
import json
from database import init_db, async_session_maker
from services import ActionPipelineService

# Configure logging to console
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger("PipelineVerifier")

async def main():
    logger.info("--- STARTING END-TO-END PIPELINE VERIFICATION ---")
    
    # 1. Initialize database tables
    logger.info("Initializing database tables (if they do not exist)...")
    await init_db()
    
    # 2. Hardcoded test student query containing clutter and PII (personal data)
    test_query = (
        "Я Алексей, мой тел 89991112233. Срочно запиши что в пятницу в 14:00 лек по матану "
        "в 402 аудитории у Козлова, а еще в субботу дедлайн по лабе ИИ"
    )
    
    logger.info("==================================================")
    logger.info("[STAGE 0] Raw Student Query:")
    logger.info(f"'{test_query}'")
    logger.info("==================================================")

    # 3. Initialize ActionPipelineService and run process_text_input
    pipeline = ActionPipelineService()
    
    # Open an async database session
    async with async_session_maker() as session:
        try:
            # We use a mock Telegram user ID
            test_tg_id = 998877665
            logger.info(f"Processing input for Telegram User ID: {test_tg_id}")
            
            # Execute pipeline
            created_events = await pipeline.process_text_input(
                user_tg_id=test_tg_id,
                raw_text=test_query,
                db_session=session
            )
            
            logger.info("==================================================")
            logger.info("Pipeline executed successfully. Reviewing steps:")
            
            # [Транскрипт]
            logger.info(f"\n[STEP 1 - Transcript / Raw Input]:\n{test_query}")
            
            # [После анонимизации]
            anonymized = pipeline.anonymizer.anonymize_text(test_query)
            logger.info(f"\n[STEP 2 - After Anonymization (PII Masked)]:\n{anonymized}")
            
            # [Полученный JSON]
            extracted_json = await pipeline.gpt_service.extract_schedule(anonymized)
            logger.info(f"\n[STEP 3 - YandexGPT Extracted JSON Output]:\n{json.dumps(extracted_json, indent=2, ensure_ascii=False)}")
            
            # [ID созданных draft-событий в БД]
            logger.info("\n[STEP 4 - Database Persistence / Created Draft Events]:")
            for idx, event in enumerate(created_events, 1):
                logger.info(
                    f"  Event #{idx}: ID={event.id}, Title='{event.title}', "
                    f"Deadline={event.deadline}, Status={event.status.value}, "
                    f"Description='{event.description}'"
                )
                logger.info(f"  Reminders scheduled ({len(event.reminders)} items):")
                for rem in event.reminders:
                    logger.info(f"    - ID={rem.id}, Remind At={rem.remind_at}, Status={rem.status.value}")
                    
            logger.info("==================================================")
            logger.info("--- END-TO-END PIPELINE VERIFICATION SUCCESSFUL ---")
            
        except Exception as e:
            logger.error(f"Pipeline verification failed: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
