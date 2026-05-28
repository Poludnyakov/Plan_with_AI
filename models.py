import enum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import BigInteger, ForeignKey, String, Text, DateTime, Enum, func, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class EventStatus(str, enum.Enum):
    """Represents the status of an event."""
    DRAFT = "draft"
    CONFIRMED = "confirmed"


class ReminderStatus(str, enum.Enum):
    """Represents the status of a reminder notification."""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class User(Base):
    """
    Users table.
    Stores application users (Telegram students).
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Moscow", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )
    google_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    google_spreadsheet_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Relationships
    # One user can have many events. Deleting a user deletes all their events.
    events: Mapped[List["Event"]] = relationship(
        "Event", 
        back_populates="user", 
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, tg_id={self.tg_id}, timezone='{self.timezone}')>"


class Event(Base):
    """
    Events table.
    Stores scheduled student academic and personal events.
    """
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), 
        nullable=False,
        index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status"), 
        default=EventStatus.DRAFT, 
        nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    # Many events belong to a single user.
    user: Mapped["User"] = relationship("User", back_populates="events")
    
    # One event can have many reminders. Deleting an event deletes all its reminders.
    reminders: Mapped[List["Reminder"]] = relationship(
        "Reminder", 
        back_populates="event", 
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Event(id={self.id}, title='{self.title}', status='{self.status}')>"


class Reminder(Base):
    """
    Reminders table.
    Stores reminders linked to specific events.
    """
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), 
        nullable=False,
        index=True
    )
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus, name="reminder_status"), 
        default=ReminderStatus.PENDING, 
        nullable=False
    )

    # Relationships
    # Many reminders belong to a single event.
    event: Mapped["Event"] = relationship("Event", back_populates="reminders")

    def __repr__(self) -> str:
        return f"<Reminder(id={self.id}, event_id={self.event_id}, remind_at={self.remind_at}, status='{self.status}')>"
