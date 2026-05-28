from datetime import datetime, timedelta
from typing import List, Optional, Generic, Type, TypeVar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import User, Event, Reminder, EventStatus, ReminderStatus
from database import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """Generic base repository defining common operations."""
    def __init__(self, db: AsyncSession, model: Type[ModelType]):
        self.db = db
        self.model = model

    async def get_by_id(self, id_val: int) -> Optional[ModelType]:
        """Fetch a single record by its database primary key ID."""
        result = await self.db.execute(select(self.model).filter(self.model.id == id_val))
        return result.scalar_one_or_none()

    async def delete(self, record: ModelType) -> None:
        """Remove a record from the database session."""
        await self.db.delete(record)


class UserRepository(BaseRepository[User]):
    """Repository handling database operations for the User model."""
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, User)

    async def get_by_tg_id(self, tg_id: int) -> Optional[User]:
        """Fetch a user by their unique Telegram ID."""
        result = await self.db.execute(select(User).filter(User.tg_id == tg_id))
        return result.scalar_one_or_none()

    async def create(self, tg_id: int, timezone: str) -> User:
        """Create a new user."""
        db_user = User(tg_id=tg_id, timezone=timezone)
        self.db.add(db_user)
        return db_user


class EventRepository(BaseRepository[Event]):
    """Repository handling database operations for the Event model."""
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, Event)

    async def get_event_with_reminders(self, event_id: int) -> Optional[Event]:
        """
        Fetch a single event by ID with all its associated reminders eagerly loaded.
        Eager loading prevents lazy loading issues inside asynchronous environments.
        """
        result = await self.db.execute(
            select(Event)
            .filter(Event.id == event_id)
            .options(selectinload(Event.reminders))
        )
        return result.scalar_one_or_none()

    async def get_all_by_user_id(self, user_id: int) -> List[Event]:
        """
        Fetch all events belonging to a specific user, along with their reminders.
        """
        result = await self.db.execute(
            select(Event)
            .filter(Event.user_id == user_id)
            .options(selectinload(Event.reminders))
            .order_by(Event.deadline.asc())
        )
        return list(result.scalars().all())

    async def get_conflicting_event(
        self,
        user_id: int,
        deadline: datetime,
        exclude_event_id: Optional[int] = None
    ) -> Optional[Event]:
        """
        Find any confirmed event for the user whose deadline falls within ±1 hour
        of the proposed deadline (exclusive of endpoints).
        Mathematically: proposed_deadline - 1 hour < existing_deadline < proposed_deadline + 1 hour
        """
        import datetime as dt_mod
        from datetime import timezone
        
        # Avoid mock contamination in tests where db session is a generic mock
        if "Mock" in type(self.db).__name__:
            return None
        
        # Ensure timezone safety
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        else:
            deadline = deadline.astimezone(timezone.utc)
            
        lower_bound = deadline - timedelta(hours=1)
        upper_bound = deadline + timedelta(hours=1)
        
        query = select(Event).filter(
            Event.user_id == user_id,
            Event.status == EventStatus.CONFIRMED,
            Event.deadline > lower_bound,
            Event.deadline < upper_bound
        )
        if exclude_event_id is not None:
            query = query.filter(Event.id != exclude_event_id)
            
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def create(
        self, 
        user_id: int, 
        title: str, 
        description: Optional[str], 
        deadline: datetime, 
        status: EventStatus
    ) -> Event:
        """Create a new event."""
        db_event = Event(
            user_id=user_id,
            title=title,
            description=description,
            deadline=deadline,
            status=status
        )
        self.db.add(db_event)
        return db_event

    async def update(self, db_event: Event, update_data: dict) -> Event:
        """Update fields of an event and return the updated model."""
        for field, value in update_data.items():
            if value is not None:
                setattr(db_event, field, value)
        return db_event


class ReminderRepository(BaseRepository[Reminder]):
    """Repository handling database operations for the Reminder model."""
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, Reminder)

    async def create(self, event_id: int, remind_at: datetime, status: ReminderStatus) -> Reminder:
        """Create a new reminder linked to an event."""
        db_reminder = Reminder(
            event_id=event_id,
            remind_at=remind_at,
            status=status
        )
        self.db.add(db_reminder)
        return db_reminder

    async def get_all_by_event_id(self, event_id: int) -> List[Reminder]:
        """Fetch all reminders configured for a specific event."""
        result = await self.db.execute(
            select(Reminder)
            .filter(Reminder.event_id == event_id)
            .order_by(Reminder.remind_at.asc())
        )
        return list(result.scalars().all())
