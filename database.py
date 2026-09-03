import os
from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

# Database configuration URL. We support reading from environment variables for production/development
# and fall back to a default PostgreSQL container URL for local MVP development.
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+asyncpg://postgres:postgres@localhost:5432/planiruy"
)

# Create an asynchronous engine. 
# echo=True is helpful during development/debugging to see SQL queries in the console.
engine = create_async_engine(DATABASE_URL, echo=True, future=True)

# Create an async session factory. 
# expire_on_commit=False prevents SQLAlchemy from trying to re-fetch models after commit
# in an async context (which raises DetachedInstanceError).
async_session_maker = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

# Modern declarative base for SQLAlchemy 2.0 type mapping
class Base(DeclarativeBase):
    pass

# Dependency for FastAPI to get a database session per request.
# Automatically handles opening and closing the session using an async generator context manager.
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()

# Database initialization function to create all tables.
# For rapid MVP development, we can trigger this on application startup.
async def init_db() -> None:
    async with engine.begin() as conn:
        # Import models here to register them with Base.metadata before creation
        import models  # noqa: F401
        import account_models  # noqa: F401
        import interval_models  # noqa: F401
        import max_bot.models  # noqa: F401
        import schedule_models  # noqa: F401
        import reminder_models  # noqa: F401
        import conversation_models  # noqa: F401
        import statistics_models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        # create_all() does not add columns to already deployed PostgreSQL tables.
        # This migration is additive and keeps every existing timed event unchanged.
        if conn.dialect.name == "postgresql":
            await conn.execute(text(
                "ALTER TABLE event_timings "
                "ADD COLUMN IF NOT EXISTS all_day BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            await conn.execute(text(
                "ALTER TABLE max_event_timings "
                "ADD COLUMN IF NOT EXISTS all_day BOOLEAN NOT NULL DEFAULT FALSE"
            ))
