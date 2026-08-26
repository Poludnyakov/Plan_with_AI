from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class EventTiming(Base):
    """Start/end interval attached to a legacy Event row."""

    __tablename__ = "event_timings"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # A date-only event occupies the whole local day (end_at is exclusive).
    # It intentionally does not participate in timed-event conflict detection.
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
