from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class MaxUser(Base):
    __tablename__ = "max_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Moscow", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    events: Mapped[list["MaxEvent"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class MaxEvent(Base):
    __tablename__ = "max_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("max_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True, nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user: Mapped[MaxUser] = relationship(back_populates="events")
    timing: Mapped["MaxEventTiming"] = relationship(
        back_populates="event", cascade="all, delete-orphan", passive_deletes=True,
        uselist=False,
    )
    reminders: Mapped[list["MaxReminder"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", passive_deletes=True
    )


class MaxEventTiming(Base):
    __tablename__ = "max_event_timings"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("max_events.id", ondelete="CASCADE"), primary_key=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    event: Mapped[MaxEvent] = relationship(back_populates="timing")


class MaxReminder(Base):
    __tablename__ = "max_reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("max_events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    event: Mapped[MaxEvent] = relationship(back_populates="reminders")


class MaxInboxUpdate(Base):
    __tablename__ = "max_inbox_updates"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

